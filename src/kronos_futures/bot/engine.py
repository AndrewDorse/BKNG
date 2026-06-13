from __future__ import annotations

import asyncio
import hashlib
import logging
from collections import deque
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from prometheus_client import Counter, Gauge, Histogram

from .domain import (
    AccountContext,
    MarketContext,
    OrderRequest,
    PositionContext,
    Side,
)
from .risk import GuardedRiskEngine
from .settings import BindingSettings

LOG = logging.getLogger(__name__)
SIGNALS = Counter("kronos_bot_signals_total", "Strategy signals", ["binding", "outcome"])
ORDERS = Counter("kronos_bot_orders_total", "Orders", ["binding", "purpose", "status"])
INFERENCE = Histogram("kronos_bot_inference_seconds", "Inference latency", ["binding"])
READY = Gauge("kronos_bot_binding_ready", "Binding readiness", ["binding"])


def client_order_id(binding: str, close_time: datetime, purpose: str) -> str:
    raw = f"{binding}|{close_time.isoformat()}|{purpose}".encode()
    digest = hashlib.sha256(raw).hexdigest()[:20]
    return f"kr_{purpose[:4]}_{digest}"


def contiguous(candles, interval_seconds: int, required: int = 512) -> bool:
    if len(candles) < required or not all(candle.closed for candle in candles[-required:]):
        return False
    selected = candles[-required:]
    return all(
        int((right.open_time - left.open_time).total_seconds()) == interval_seconds
        for left, right in zip(selected, selected[1:], strict=True)
    )


class TradingEngine:
    def __init__(self, binding, strategy, risk, exchange, inference, store):
        self.binding: BindingSettings = binding
        self.strategy = strategy
        self.risk: GuardedRiskEngine = risk
        self.exchange = exchange
        self.inference = inference
        self.store = store
        self.candles = deque(maxlen=600)
        self.rules = None
        self.position = PositionContext(binding.symbol)
        self.running = True
        self.halted_reason: str | None = None

    async def preflight(self) -> None:
        await self.exchange.synchronize_time()
        if not await self.inference.ready():
            raise RuntimeError("Inference readiness benchmark failed")
        if not await self.exchange.position_mode_is_one_way():
            raise RuntimeError("Binance account must use one-way position mode")
        if not await self.exchange.account_is_single_asset():
            raise RuntimeError("Binance account must use single-asset margin mode")
        if not await self.exchange.symbol_is_isolated(self.binding.symbol):
            raise RuntimeError(f"{self.binding.symbol} must already use isolated margin")
        self.rules = await self.exchange.symbol_rules(self.binding.symbol)
        if self.binding.risk.leverage > self.rules.maximum_leverage:
            raise RuntimeError("Configured leverage exceeds symbol limit")
        positions = [p for p in await self.exchange.positions() if p.symbol == self.binding.symbol]
        orders = await self.exchange.open_orders(self.binding.symbol)
        if positions:
            owned = await self.store.owns_symbol(self.binding.symbol)
            if not owned:
                raise RuntimeError(f"Unknown position exists for {self.binding.symbol}")
            position = positions[0]
            if not position.isolated:
                raise RuntimeError("Owned position is not isolated")
            stops = [order for order in orders if order.client_order_id.startswith("kr_stop_")]
            if not stops:
                await self._flatten_and_halt("owned_position_missing_stop")
                raise RuntimeError("Owned position had no protective stop")
            self.position = PositionContext(
                symbol=position.symbol,
                side=Side.LONG if position.quantity > 0 else Side.SHORT,
                quantity=abs(position.quantity),
                entry_price=position.entry_price,
                protected=True,
            )
        elif orders:
            unknown = [
                order for order in orders if not order.client_order_id.startswith("kr_")
            ]
            if unknown:
                raise RuntimeError(f"Unknown open orders exist for {self.binding.symbol}")
        await self.exchange.set_leverage(self.binding.symbol, self.binding.risk.leverage)
        initial = await self.exchange.klines(
            self.binding.symbol, self.binding.interval, limit=513
        )
        self.candles.extend(initial[-512:])
        if not contiguous(tuple(self.candles), 60):
            raise RuntimeError("Initial candle history is not contiguous")
        READY.labels(self.binding.name).set(1)

    async def run(self) -> None:
        await self.preflight()
        await self.run_after_preflight()

    async def run_after_preflight(self) -> None:
        reconcile_task = asyncio.create_task(self._reconcile_loop())
        try:
            async for candle in self.exchange.closed_kline_stream(
                self.binding.symbol, self.binding.interval
            ):
                if not self.running:
                    break
                await self.on_candle(candle)
        finally:
            reconcile_task.cancel()
            READY.labels(self.binding.name).set(0)

    async def on_candle(self, candle) -> None:
        if self.candles and candle.open_time <= self.candles[-1].open_time:
            return
        self.candles.append(candle)
        if not contiguous(tuple(self.candles), 60):
            READY.labels(self.binding.name).set(0)
            backfill = await self.exchange.klines(
                self.binding.symbol, self.binding.interval, limit=513
            )
            self.candles.clear()
            self.candles.extend(backfill[-512:])
            if not contiguous(tuple(self.candles), 60):
                LOG.error("candle_gap_unresolved", extra={"symbol": self.binding.symbol})
                return
            READY.labels(self.binding.name).set(1)
        if await self._exit_if_expired(candle.close_time):
            return
        state = await self.store.runtime_state("control")
        if state and state.get("paused"):
            return
        bid, ask = await self.exchange.book_ticker(self.binding.symbol)
        account = await self.store.risk_adjusted_account(await self.exchange.account())
        market = MarketContext(
            self.binding.symbol,
            self.binding.interval,
            tuple(self.candles)[-512:],
            bid,
            ask,
            datetime.now(timezone.utc),
        )
        started = asyncio.get_running_loop().time()
        forecast = await self.inference.forecast(market)
        INFERENCE.labels(self.binding.name).observe(
            asyncio.get_running_loop().time() - started
        )
        intent = self.strategy.evaluate(market, forecast, self.position, account)
        inserted = await self.store.record_signal(self.binding.name, intent)
        if not inserted:
            SIGNALS.labels(self.binding.name, "duplicate").inc()
            return
        approved, reason = self.risk.approve_entry(
            intent, market, forecast, account, self.rules
        )
        SIGNALS.labels(self.binding.name, "approved" if approved else reason).inc()
        if approved:
            await self._enter(intent, market, account)

    async def _enter(self, intent, market, account: AccountContext) -> None:
        assert intent.side and self.rules
        quantity = self.risk.entry_quantity(account, market, self.rules)
        entry_id = client_order_id(
            self.binding.name, intent.candle_close_time, "entry"
        )
        request = OrderRequest(
            symbol=self.binding.symbol,
            side=intent.side.entry_order_side,
            order_type="MARKET",
            quantity=quantity,
            client_order_id=entry_id,
        )
        await self.store.persist_order_intent(self.binding.name, request, "entry")
        result = await self.exchange.submit_order(request)
        await self.store.record_order(result)
        ORDERS.labels(self.binding.name, "entry", result.status).inc()
        if result.status not in {"FILLED", "PARTIALLY_FILLED"} or result.executed_quantity <= 0:
            return
        entry_price = result.average_price
        self.position = PositionContext(
            self.binding.symbol,
            intent.side,
            result.executed_quantity,
            entry_price,
            datetime.now(timezone.utc),
            False,
        )
        try:
            await self._place_stop(intent.candle_close_time)
        except Exception:
            LOG.exception("protective_stop_failed", extra={"symbol": self.binding.symbol})
            await self._flatten_and_halt("protective_stop_failed")

    async def _place_stop(self, candle_close_time: datetime) -> None:
        assert self.position.side and self.rules
        stop_price = self.risk.stop_price(
            self.position.entry_price, self.position.side.sign, self.rules.price_tick
        )
        request = OrderRequest(
            symbol=self.binding.symbol,
            side=self.position.side.exit_order_side,
            order_type="STOP_MARKET",
            quantity=self.position.quantity,
            client_order_id=client_order_id(self.binding.name, candle_close_time, "stop"),
            stop_price=stop_price,
            working_type="MARK_PRICE",
            close_position=True,
        )
        await self.store.persist_order_intent(self.binding.name, request, "stop")
        result = await self.exchange.submit_order(request)
        await self.store.record_order(result)
        ORDERS.labels(self.binding.name, "stop", result.status).inc()
        if result.status not in {"NEW", "PARTIALLY_FILLED", "FILLED"}:
            raise RuntimeError(f"Stop rejected with status {result.status}")
        self.position = replace(self.position, protected=True)

    async def _exit_if_expired(self, now: datetime) -> bool:
        if not self.position.is_open or not self.position.opened_at:
            return False
        hold = timedelta(minutes=getattr(self.strategy, "maximum_hold_minutes", 120))
        if now < self.position.opened_at + hold:
            return False
        await self.flatten("maximum_hold")
        return True

    async def flatten(self, reason: str) -> None:
        if not self.position.is_open or not self.position.side:
            return
        await self.exchange.cancel_all_orders(self.binding.symbol)
        close_time = datetime.now(timezone.utc)
        request = OrderRequest(
            symbol=self.binding.symbol,
            side=self.position.side.exit_order_side,
            order_type="MARKET",
            quantity=self.position.quantity,
            client_order_id=client_order_id(self.binding.name, close_time, "exit"),
            reduce_only=True,
        )
        await self.store.persist_order_intent(self.binding.name, request, "exit")
        result = await self.exchange.submit_order(request)
        await self.store.record_order(result)
        ORDERS.labels(self.binding.name, "exit", result.status).inc()
        self.position = PositionContext(self.binding.symbol)

    async def _flatten_and_halt(self, reason: str) -> None:
        try:
            await self.flatten(reason)
        finally:
            self.halted_reason = reason
            await self.store.set_runtime_state(
                "control", {"paused": True, "reason": reason}
            )

    async def _reconcile_loop(self) -> None:
        while self.running:
            await asyncio.sleep(15)
            try:
                positions = [
                    p for p in await self.exchange.positions()
                    if p.symbol == self.binding.symbol
                ]
                orders = await self.exchange.open_orders(self.binding.symbol)
                if self.position.is_open:
                    if not positions:
                        self.position = PositionContext(self.binding.symbol)
                    elif not any(
                        order.client_order_id.startswith("kr_stop_") for order in orders
                    ):
                        await self._flatten_and_halt("stop_missing_during_reconcile")
                elif positions:
                    if not await self.store.owns_symbol(self.binding.symbol):
                        self.halted_reason = "unknown_position"
                        await self.store.set_runtime_state(
                            "control", {"paused": True, "reason": "unknown_position"}
                        )
            except Exception:
                LOG.exception("reconciliation_failed", extra={"symbol": self.binding.symbol})

    async def process_user_events(self) -> None:
        async for event in self.exchange.user_stream():
            event_type = event.get("e")
            if event_type == "ORDER_TRADE_UPDATE":
                order = event["o"]
                client_id = order.get("c", "")
                if not client_id.startswith("kr_"):
                    continue
                await self.store.update_order_event(order)
                await self.store.record_trade_update(
                    order,
                    int(event["E"]),
                    self.binding.risk.consecutive_loss_limit,
                    self.binding.risk.loss_pause_hours,
                )
                if order.get("X") == "FILLED" and client_id.startswith("kr_stop_"):
                    self.position = PositionContext(self.binding.symbol)
            elif event_type == "ACCOUNT_UPDATE":
                await self.store.record_account_event(event)
                for balance in event.get("a", {}).get("B", []):
                    if balance.get("a") == "USDT":
                        LOG.info(
                            "account_update",
                            extra={"wallet_balance": balance.get("wb")},
                        )
