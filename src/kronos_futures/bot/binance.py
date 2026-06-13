from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, AsyncIterator
from urllib.parse import urlencode

import httpx
import websockets

from .domain import (
    AccountContext,
    Candle,
    OrderRequest,
    OrderResult,
    PositionSnapshot,
    SymbolRules,
    TradingMode,
)


class BinanceError(RuntimeError):
    def __init__(self, status: int, code: int | None, message: str):
        super().__init__(f"Binance error status={status} code={code}: {message}")
        self.status = status
        self.code = code


class BinanceGateway:
    LIVE_REST = "https://fapi.binance.com"
    TESTNET_REST = "https://testnet.binancefuture.com"
    LIVE_WS = "wss://fstream.binance.com"
    TESTNET_WS = "wss://stream.binancefuture.com"

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        mode: TradingMode,
        timeout_seconds: float = 5.0,
    ):
        self.api_key = api_key
        self.api_secret = api_secret.encode()
        self.mode = mode
        self.rest_base = self.TESTNET_REST if mode is TradingMode.TESTNET else self.LIVE_REST
        self.ws_base = self.TESTNET_WS if mode is TradingMode.TESTNET else self.LIVE_WS
        self.client = httpx.AsyncClient(timeout=timeout_seconds)
        self.time_offset_ms = 0
        self._rules: dict[str, SymbolRules] = {}

    async def close(self) -> None:
        await self.client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        signed: bool = False,
        retries: int = 3,
    ) -> Any:
        values = {key: str(value) for key, value in (params or {}).items() if value is not None}
        headers = {"X-MBX-APIKEY": self.api_key} if self.api_key else {}
        if signed:
            values["timestamp"] = str(int(time.time() * 1000) + self.time_offset_ms)
            values["recvWindow"] = "5000"
            query = urlencode(values)
            values["signature"] = hmac.new(
                self.api_secret, query.encode(), hashlib.sha256
            ).hexdigest()
        for attempt in range(retries):
            response = await self.client.request(
                method, self.rest_base + path, params=values, headers=headers
            )
            if response.status_code < 400:
                return response.json()
            body = response.json()
            code = body.get("code")
            if code == -1021 and attempt == 0:
                await self.synchronize_time()
                values["timestamp"] = str(int(time.time() * 1000) + self.time_offset_ms)
                unsigned = {key: value for key, value in values.items() if key != "signature"}
                values["signature"] = hmac.new(
                    self.api_secret, urlencode(unsigned).encode(), hashlib.sha256
                ).hexdigest()
                continue
            if response.status_code in {418, 429} or response.status_code >= 500:
                await asyncio.sleep(min(2**attempt, 8))
                continue
            raise BinanceError(response.status_code, code, body.get("msg", response.text))
        raise BinanceError(response.status_code, body.get("code"), body.get("msg", response.text))

    async def synchronize_time(self) -> None:
        started = int(time.time() * 1000)
        payload = await self._request("GET", "/fapi/v1/time")
        completed = int(time.time() * 1000)
        midpoint = (started + completed) // 2
        self.time_offset_ms = int(payload["serverTime"]) - midpoint

    async def symbol_rules(self, symbol: str) -> SymbolRules:
        if symbol in self._rules:
            return self._rules[symbol]
        payload = await self._request("GET", "/fapi/v1/exchangeInfo")
        match = next(item for item in payload["symbols"] if item["symbol"] == symbol)
        filters = {item["filterType"]: item for item in match["filters"]}
        lot = filters["LOT_SIZE"]
        price = filters["PRICE_FILTER"]
        notional = filters.get("MIN_NOTIONAL", {"notional": "0"})
        if self.api_key:
            brackets = await self._request(
                "GET", "/fapi/v1/leverageBracket", {"symbol": symbol}, signed=True
            )
            maximum_leverage = max(
                int(row["initialLeverage"]) for row in brackets[0]["brackets"]
            )
        else:
            maximum_leverage = 125
        rules = SymbolRules(
            symbol=symbol,
            price_tick=Decimal(price["tickSize"]),
            quantity_step=Decimal(lot["stepSize"]),
            minimum_quantity=Decimal(lot["minQty"]),
            minimum_notional=Decimal(notional.get("notional", "0")),
            maximum_quantity=Decimal(lot["maxQty"]),
            maximum_leverage=maximum_leverage,
        )
        self._rules[symbol] = rules
        return rules

    async def account(self) -> AccountContext:
        payload = await self._request("GET", "/fapi/v3/account", signed=True)
        equity = Decimal(payload["totalMarginBalance"])
        available = Decimal(payload["availableBalance"])
        return AccountContext(
            equity=equity,
            available_balance=available,
            peak_equity=equity,
            daily_realized_pnl=Decimal(0),
            consecutive_losses=0,
        )

    async def positions(self) -> list[PositionSnapshot]:
        payload = await self._request("GET", "/fapi/v3/positionRisk", signed=True)
        return [
            PositionSnapshot(
                symbol=item["symbol"],
                quantity=Decimal(item["positionAmt"]),
                entry_price=Decimal(item["entryPrice"]),
                isolated=item["marginType"] == "isolated",
                leverage=int(item["leverage"]),
            )
            for item in payload
            if Decimal(item["positionAmt"]) != 0
        ]

    async def symbol_is_isolated(self, symbol: str) -> bool:
        try:
            await self._request(
                "POST",
                "/fapi/v1/marginType",
                {"symbol": symbol, "marginType": "ISOLATED"},
                signed=True,
            )
        except BinanceError as exc:
            # Binance returns -4046 when the requested margin type is already active.
            if exc.code != -4046:
                raise
        return True

    @staticmethod
    def _order_result(payload: dict[str, Any]) -> OrderResult:
        return OrderResult(
            symbol=payload["symbol"],
            client_order_id=payload["clientOrderId"],
            order_id=int(payload["orderId"]),
            status=payload["status"],
            executed_quantity=Decimal(payload.get("executedQty", "0")),
            average_price=Decimal(payload.get("avgPrice", "0")),
            order_type=payload.get("type", ""),
        )

    async def open_orders(self, symbol: str | None = None) -> list[OrderResult]:
        params = {"symbol": symbol} if symbol else {}
        payload = await self._request("GET", "/fapi/v1/openOrders", params, signed=True)
        return [self._order_result(item) for item in payload]

    async def submit_order(self, request: OrderRequest) -> OrderResult:
        params = {
            "symbol": request.symbol,
            "side": request.side,
            "type": request.order_type,
            "quantity": None if request.close_position else request.quantity,
            "newClientOrderId": request.client_order_id,
            "reduceOnly": str(request.reduce_only).lower() if request.reduce_only else None,
            "stopPrice": request.stop_price,
            "workingType": request.working_type,
            "closePosition": str(request.close_position).lower() if request.close_position else None,
            "newOrderRespType": "RESULT" if request.order_type == "MARKET" else "ACK",
        }
        try:
            payload = await self._request("POST", "/fapi/v1/order", params, signed=True, retries=1)
        except (httpx.TimeoutException, httpx.NetworkError):
            existing = await self.query_order(request.symbol, request.client_order_id)
            if existing:
                return existing
            payload = await self._request("POST", "/fapi/v1/order", params, signed=True, retries=1)
        return self._order_result(payload)

    async def query_order(self, symbol: str, client_order_id: str) -> OrderResult | None:
        try:
            payload = await self._request(
                "GET",
                "/fapi/v1/order",
                {"symbol": symbol, "origClientOrderId": client_order_id},
                signed=True,
            )
        except BinanceError as exc:
            if exc.code == -2013:
                return None
            raise
        return self._order_result(payload)

    async def cancel_all_orders(self, symbol: str) -> None:
        await self._request(
            "DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol}, signed=True
        )

    async def start_user_stream(self) -> str:
        payload = await self._request("POST", "/fapi/v1/listenKey")
        return payload["listenKey"]

    async def keepalive_user_stream(self, listen_key: str) -> None:
        await self._request("PUT", "/fapi/v1/listenKey", {"listenKey": listen_key})

    async def user_stream(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            listen_key = await self.start_user_stream()

            async def keepalive():
                while True:
                    await asyncio.sleep(30 * 60)
                    await self.keepalive_user_stream(listen_key)

            task = asyncio.create_task(keepalive())
            try:
                async with websockets.connect(
                    f"{self.ws_base}/ws/{listen_key}", ping_interval=20
                ) as ws:
                    async for raw in ws:
                        event = json.loads(raw)
                        if event.get("e") == "listenKeyExpired":
                            break
                        yield event
            except (OSError, websockets.ConnectionClosed):
                await asyncio.sleep(2)
            finally:
                task.cancel()

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        await self._request(
            "POST",
            "/fapi/v1/leverage",
            {"symbol": symbol, "leverage": leverage},
            signed=True,
        )

    async def position_mode_is_one_way(self) -> bool:
        payload = await self._request("GET", "/fapi/v1/positionSide/dual", signed=True)
        return not bool(payload["dualSidePosition"])

    async def account_is_single_asset(self) -> bool:
        payload = await self._request("GET", "/fapi/v1/multiAssetsMargin", signed=True)
        return not bool(payload["multiAssetsMargin"])

    async def klines(self, symbol: str, interval: str, limit: int = 512) -> tuple[Candle, ...]:
        payload = await self._request(
            "GET", "/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": limit}
        )
        now_ms = int(time.time() * 1000) + self.time_offset_ms
        return tuple(
            Candle(
                open_time=datetime.fromtimestamp(item[0] / 1000, timezone.utc),
                close_time=datetime.fromtimestamp(item[6] / 1000, timezone.utc),
                open=Decimal(item[1]),
                high=Decimal(item[2]),
                low=Decimal(item[3]),
                close=Decimal(item[4]),
                volume=Decimal(item[5]),
                amount=Decimal(item[7]),
                closed=item[6] < now_ms,
            )
            for item in payload
            if item[6] < now_ms
        )

    async def book_ticker(self, symbol: str) -> tuple[Decimal, Decimal]:
        payload = await self._request("GET", "/fapi/v1/ticker/bookTicker", {"symbol": symbol})
        return Decimal(payload["bidPrice"]), Decimal(payload["askPrice"])

    async def closed_kline_stream(
        self, symbol: str, interval: str
    ) -> AsyncIterator[Candle]:
        stream = f"{symbol.lower()}@kline_{interval}"
        while True:
            try:
                async with websockets.connect(f"{self.ws_base}/ws/{stream}", ping_interval=20) as ws:
                    async for raw in ws:
                        event = json.loads(raw)
                        kline = event["k"]
                        if not kline["x"]:
                            continue
                        yield Candle(
                            open_time=datetime.fromtimestamp(kline["t"] / 1000, timezone.utc),
                            close_time=datetime.fromtimestamp(kline["T"] / 1000, timezone.utc),
                            open=Decimal(kline["o"]),
                            high=Decimal(kline["h"]),
                            low=Decimal(kline["l"]),
                            close=Decimal(kline["c"]),
                            volume=Decimal(kline["v"]),
                            amount=Decimal(kline["q"]),
                            closed=True,
                        )
            except (OSError, websockets.ConnectionClosed):
                await asyncio.sleep(2)
