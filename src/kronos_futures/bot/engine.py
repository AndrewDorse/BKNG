from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import replace
from datetime import datetime, timezone

from prometheus_client import Counter, Gauge, Histogram

from .domain import MarketContext, OrderRequest, PositionContext, Side
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
        for left, right in zip(selected, selected[1:])
    )


class TradingEngine:
    def __init__(self, binding, strategy, risk, exchange, inference, poll_seconds: int = 30):
        self.binding: BindingSettings = binding
        self.strategy = strategy
        self.risk: GuardedRiskEngine = risk
        self.exchange = exchange
        self.inference = inference
        self.poll_seconds = poll_seconds
        self.rules = None
        self.running = True
        self.ready = False
        self.halted_reason: str | None = None
        self.last_analyzed_candle: datetime | None = None
        self.peak_equity = None
        self.last_position: PositionContext | None = None

    async def preflight(self) -> None:
        attempts = 0
        while self.running and not await self.inference.ready():
            attempts += 1
            if attempts == 1 or attempts % 6 == 0:
                LOG.info(
                    "waiting_for_inference",
                    extra={"symbol": self.binding.symbol, "attempt": attempts},
                )
            await asyncio.sleep(10)
        if not self.running:
            raise RuntimeError("Trader stopped while waiting for inference")
        await self.exchange.synchronize_time()
        if not await self.exchange.position_mode_is_one_way():
            raise RuntimeError("Binance account must use one-way position mode")
        if not await self.exchange.account_is_single_asset():
            raise RuntimeError("Binance account must use single-asset margin mode")
        if not await self.exchange.symbol_is_isolated(self.binding.symbol):
            raise RuntimeError(f"{self.binding.symbol} must already use isolated margin")
        self.rules = await self.exchange.symbol_rules(self.binding.symbol)
        if self.binding.risk.leverage > self.rules.maximum_leverage:
            raise RuntimeError("Configured leverage exceeds symbol limit")
        await self.exchange.set_leverage(self.binding.symbol, self.binding.risk.leverage)
        candles = await self.exchange.klines(
            self.binding.symbol, self.binding.interval, limit=513
        )
        candles = candles[-512:]
        if not contiguous(candles, 60):
            raise RuntimeError("Initial candle history is not contiguous")
        await self.reconcile()
        self.ready = True
        READY.labels(self.binding.name).set(1)
        LOG.info(
            "binding_ready",
            extra={
                "symbol": self.binding.symbol,
                "mode": self.exchange.mode.value,
            },
        )

    async def run(self) -> None:
        try:
            await self.preflight()
            while self.running:
                started = asyncio.get_running_loop().time()
                try:
                    await self.cycle()
                except Exception:
                    LOG.exception("analysis_cycle_failed", extra={"symbol": self.binding.symbol})
                elapsed = asyncio.get_running_loop().time() - started
                await asyncio.sleep(max(1, self.poll_seconds - elapsed))
        finally:
            self.ready = False
            READY.labels(self.binding.name).set(0)

    async def cycle(self) -> None:
        position = await self.reconcile()
        candles = await self.exchange.klines(
            self.binding.symbol, self.binding.interval, limit=513
        )
        candles = candles[-512:]
        if not contiguous(candles, 60):
            raise RuntimeError("Binance returned non-contiguous candle history")
        latest = candles[-1]
        if self.last_analyzed_candle == latest.open_time:
            return
        self.last_analyzed_candle = latest.open_time
        if position.is_open or self.halted_reason:
            return

        bid, ask = await self.exchange.book_ticker(self.binding.symbol)
        account = await self.exchange.account()
        self.peak_equity = max(self.peak_equity or account.equity, account.equity)
        account = replace(account, peak_equity=self.peak_equity)
        market = MarketContext(
            self.binding.symbol,
            self.binding.interval,
            candles,
            bid,
            ask,
            datetime.now(timezone.utc),
        )
        started = asyncio.get_running_loop().time()
        forecast = await self.inference.forecast(market)
        INFERENCE.labels(self.binding.name).observe(
            asyncio.get_running_loop().time() - started
        )
        intent = self.strategy.evaluate(market, forecast, position, account)
        approved, reason = self.risk.approve_entry(
            intent, market, forecast, account, self.rules
        )
        SIGNALS.labels(self.binding.name, "approved" if approved else reason).inc()
        if approved:
            await self.enter(intent, market, account)

    async def reconcile(self) -> PositionContext:
        positions = [
            item for item in await self.exchange.positions()
            if item.symbol == self.binding.symbol
        ]
        orders = await self.exchange.open_orders(self.binding.symbol)
        if not positions:
            if self.last_position and self.last_position.is_open:
                LOG.info(
                    "deal_closed",
                    extra={
                        "symbol": self.last_position.symbol,
                        "side": self.last_position.side.value,
                        "quantity": str(self.last_position.quantity),
                        "price": str(self.last_position.entry_price),
                        "reason": "exchange_position_closed",
                    },
                )
            self.last_position = None
            bot_orders = [
                order for order in orders if order.client_order_id.startswith("kr_")
            ]
            if bot_orders:
                await self.exchange.cancel_all_orders(self.binding.symbol)
            return PositionContext(self.binding.symbol)

        snapshot = positions[0]
        side = Side.LONG if snapshot.quantity > 0 else Side.SHORT
        position = PositionContext(
            symbol=snapshot.symbol,
            side=side,
            quantity=abs(snapshot.quantity),
            entry_price=snapshot.entry_price,
            protected=False,
        )
        stop_exists = any(
            order.client_order_id.startswith("kr_stop_")
            or order.order_type == "STOP_MARKET"
            for order in orders
        )
        target_exists = any(
            order.client_order_id.startswith("kr_take_")
            or order.order_type == "TAKE_PROFIT_MARKET"
            for order in orders
        )
        if not stop_exists or not target_exists:
            await self.exchange.cancel_all_orders(self.binding.symbol)
            try:
                await self.place_protection(position, datetime.now(timezone.utc))
            except Exception:
                LOG.exception(
                    "position_protection_failed", extra={"symbol": self.binding.symbol}
                )
                await self.flatten(position, "protection_failed")
                self.halted_reason = "protection_failed"
                raise
        protected_position = replace(position, protected=True)
        self.last_position = protected_position
        return protected_position

    async def enter(self, intent, market, account) -> None:
        assert intent.side and self.rules
        quantity = self.risk.entry_quantity(account, market, self.rules)
        request = OrderRequest(
            symbol=self.binding.symbol,
            side=intent.side.entry_order_side,
            order_type="MARKET",
            quantity=quantity,
            client_order_id=client_order_id(
                self.binding.name, intent.candle_close_time, "entry"
            ),
        )
        result = await self.exchange.submit_order(request)
        ORDERS.labels(self.binding.name, "entry", result.status).inc()
        if result.status not in {"FILLED", "PARTIALLY_FILLED"}:
            return
        position = PositionContext(
            self.binding.symbol,
            intent.side,
            result.executed_quantity,
            result.average_price,
        )
        try:
            await self.place_protection(position, intent.candle_close_time)
        except Exception:
            await self.flatten(position, "protection_failed")
            self.halted_reason = "protection_failed"
            raise
        self.last_position = replace(position, protected=True)
        LOG.info(
            "deal_opened",
            extra={
                "symbol": position.symbol,
                "side": position.side.value,
                "quantity": str(position.quantity),
                "price": str(position.entry_price),
            },
        )

    async def place_protection(
        self, position: PositionContext, reference_time: datetime
    ) -> None:
        assert position.side and self.rules
        stop = OrderRequest(
            symbol=position.symbol,
            side=position.side.exit_order_side,
            order_type="STOP_MARKET",
            quantity=position.quantity,
            client_order_id=client_order_id(self.binding.name, reference_time, "stop"),
            stop_price=self.risk.stop_price(
                position.entry_price, position.side.sign, self.rules.price_tick
            ),
            working_type="MARK_PRICE",
            close_position=True,
        )
        target = OrderRequest(
            symbol=position.symbol,
            side=position.side.exit_order_side,
            order_type="TAKE_PROFIT_MARKET",
            quantity=position.quantity,
            client_order_id=client_order_id(self.binding.name, reference_time, "take"),
            stop_price=self.risk.target_price(
                position.entry_price, position.side.sign, self.rules.price_tick
            ),
            working_type="MARK_PRICE",
            close_position=True,
        )
        stop_result = await self.exchange.submit_order(stop)
        ORDERS.labels(self.binding.name, "stop", stop_result.status).inc()
        if stop_result.status not in {"NEW", "PARTIALLY_FILLED", "FILLED"}:
            raise RuntimeError(f"Stop rejected with status {stop_result.status}")
        target_result = await self.exchange.submit_order(target)
        ORDERS.labels(self.binding.name, "target", target_result.status).inc()
        if target_result.status not in {"NEW", "PARTIALLY_FILLED", "FILLED"}:
            raise RuntimeError(f"Target rejected with status {target_result.status}")

    async def flatten(self, position: PositionContext, reason: str) -> None:
        if not position.is_open or not position.side:
            return
        await self.exchange.cancel_all_orders(position.symbol)
        request = OrderRequest(
            symbol=position.symbol,
            side=position.side.exit_order_side,
            order_type="MARKET",
            quantity=position.quantity,
            client_order_id=client_order_id(
                self.binding.name, datetime.now(timezone.utc), "exit"
            ),
            reduce_only=True,
        )
        result = await self.exchange.submit_order(request)
        ORDERS.labels(self.binding.name, "exit", result.status).inc()
        if result.status in {"FILLED", "PARTIALLY_FILLED"}:
            LOG.info(
                "deal_closed",
                extra={
                    "symbol": position.symbol,
                    "side": position.side.value,
                    "quantity": str(result.executed_quantity),
                    "price": str(result.average_price),
                    "reason": reason,
                },
            )
            self.last_position = None
