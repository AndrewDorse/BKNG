from __future__ import annotations

import asyncio
import hashlib
import logging
import json
import os
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from prometheus_client import Counter, Gauge, Histogram
import httpx

from .binance import BinanceError
from .domain import ForecastContext, MarketContext, OrderRequest, PositionContext, Side
from .risk import GuardedRiskEngine
from .settings import BindingSettings
from .strategies import INTERVAL_SECONDS

LOG = logging.getLogger(__name__)
SIGNALS = Counter("kronos_bot_signals_total", "Strategy signals", ["binding", "outcome"])
ORDERS = Counter("kronos_bot_orders_total", "Orders", ["binding", "purpose", "status"])
INFERENCE = Histogram("kronos_bot_inference_seconds", "Inference latency", ["binding"])
READY = Gauge("kronos_bot_binding_ready", "Binding readiness", ["binding"])
HEARTBEAT_SECONDS = 4 * 60 * 60


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


def interval_seconds(interval: str) -> int:
    try:
        return INTERVAL_SECONDS[interval]
    except KeyError as exc:
        raise ValueError(f"Unsupported interval: {interval}") from exc


class TradingEngine:
    @dataclass
    class EngineState:
        managed_position: bool = False

    def __init__(
        self,
        binding,
        strategy,
        risk,
        exchange,
        inference,
        poll_seconds: int = 30,
        state_path: str | None = None,
    ):
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
        self.last_analyzed_by_interval: dict[str, datetime] = {}
        self.peak_equity = None
        self.last_position: PositionContext | None = None
        self.last_successful_analysis: datetime | None = None
        self.last_analysis_error: str | None = None
        self.current_max_hold_minutes: int | None = None
        self.effective_leverage = self.binding.risk.leverage
        self.state_path = Path(state_path) if state_path else None
        self.state = self._load_state()
        self.maximum_hold_minutes = int(
            self.binding.parameters.get("maximum_hold_minutes", 60)
        )

    def _load_state(self) -> EngineState:
        if not self.state_path or not self.state_path.exists():
            return self.EngineState()
        payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        return self.EngineState(managed_position=bool(payload.get("managed_position", False)))

    def _save_state(self) -> None:
        if not self.state_path:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"managed_position": self.state.managed_position}, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temporary, self.state_path)

    def _bot_order_prefix(self) -> str:
        return "kr_"

    def _bot_orders(self, orders):
        return [order for order in orders if order.client_order_id.startswith(self._bot_order_prefix())]

    async def _cancel_bot_orders(self, symbol: str, orders=None) -> None:
        existing = orders if orders is not None else await self.exchange.open_orders(symbol)
        bot_orders = self._bot_orders(existing)
        if not bot_orders:
            return
        for order in bot_orders:
            if hasattr(self.exchange, "cancel_order"):
                await self.exchange.cancel_order(symbol, order.client_order_id, order.order_type)
            else:
                await self.exchange.cancel_all_orders(symbol)
                break

    @property
    def requires_inference(self) -> bool:
        return bool(getattr(self.strategy, "requires_inference", True))

    @property
    def required_intervals(self) -> tuple[str, ...]:
        intervals = getattr(self.strategy, "required_intervals", None)
        if intervals:
            return tuple(intervals)
        return (self.binding.interval,)

    @property
    def required_candles(self) -> int:
        return int(getattr(self.strategy, "required_candles", 512))

    async def preflight(self) -> None:
        if self.requires_inference:
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
        self.effective_leverage = min(self.binding.risk.leverage, self.rules.maximum_leverage)
        if self.effective_leverage != self.binding.risk.leverage:
            self.risk = GuardedRiskEngine(
                replace(self.binding.risk, leverage=self.effective_leverage)
            )
            LOG.info(
                "binding_leverage_adjusted",
                extra={
                    "symbol": self.binding.symbol,
                    "leverage": self.effective_leverage,
                    "reason": "binance_symbol_limit",
                },
            )
        await self.exchange.set_leverage(self.binding.symbol, self.effective_leverage)
        required = self.required_candles
        for interval in self.required_intervals:
            candles = await self.exchange.klines(
                self.binding.symbol, interval, limit=required + 1
            )
            candles = tuple(candles[-required:])
            if not contiguous(candles, interval_seconds(interval), required):
                raise RuntimeError(
                    f"Initial {interval} candle history is not contiguous for {self.binding.symbol}"
                )
        await self.reconcile()
        self.ready = True
        READY.labels(self.binding.name).set(1)
        LOG.info(
            "binding_ready",
            extra={
                "symbol": self.binding.symbol,
                "mode": self.exchange.mode.value,
                "leverage": self.effective_leverage,
                "margin_fraction": self.binding.risk.margin_fraction,
            },
        )

    async def run(self) -> None:
        try:
            await self.preflight()
            while self.running:
                started = asyncio.get_running_loop().time()
                try:
                    await self.cycle()
                except httpx.TimeoutException as exc:
                    self.last_analysis_error = type(exc).__name__
                    LOG.error(
                        "analysis_cycle_timeout",
                        extra={
                            "symbol": self.binding.symbol,
                            "reason": type(exc).__name__,
                        },
                    )
                except Exception:
                    self.last_analysis_error = "analysis_cycle_failed"
                    LOG.exception("analysis_cycle_failed", extra={"symbol": self.binding.symbol})
                elapsed = asyncio.get_running_loop().time() - started
                await asyncio.sleep(max(1, self.poll_seconds - elapsed))
        finally:
            self.ready = False
            READY.labels(self.binding.name).set(0)

    async def cycle(self) -> None:
        position = await self.reconcile()
        if (
            position.is_open
            and position.managed
            and position.opened_at is not None
            and datetime.now(timezone.utc) - position.opened_at
            >= timedelta(minutes=self.current_max_hold_minutes or self.maximum_hold_minutes)
        ):
            await self.flatten(position, "maximum_hold")
            return
        required = self.required_candles
        new_contexts: dict[str, tuple] = {}
        for interval in self.required_intervals:
            candles = tuple(
                (await self.exchange.klines(
                    self.binding.symbol, interval, limit=required + 1
                ))[-required:]
            )
            if not contiguous(candles, interval_seconds(interval), required):
                raise RuntimeError(
                    f"Binance returned non-contiguous {interval} candle history"
                )
            latest = candles[-1]
            if self.last_analyzed_by_interval.get(interval) != latest.open_time:
                new_contexts[interval] = candles
        if not new_contexts:
            return
        for interval, candles in new_contexts.items():
            self.last_analyzed_by_interval[interval] = candles[-1].open_time
        primary_interval = next(iter(new_contexts))
        candles = new_contexts[primary_interval]
        latest = candles[-1]
        self.last_analyzed_candle = latest.open_time
        if position.is_open or self.halted_reason:
            return

        bid, ask = await self.exchange.book_ticker(self.binding.symbol)
        account = await self.exchange.account()
        self.peak_equity = max(self.peak_equity or account.equity, account.equity)
        account = replace(account, peak_equity=self.peak_equity)
        market = MarketContext(
            self.binding.symbol,
            primary_interval,
            candles,
            bid,
            ask,
            datetime.now(timezone.utc),
            multi_timeframe=new_contexts,
        )
        if self.requires_inference:
            started = asyncio.get_running_loop().time()
            forecast = await self.inference.forecast(market)
            INFERENCE.labels(self.binding.name).observe(
                asyncio.get_running_loop().time() - started
            )
        else:
            forecast = ForecastContext(
                generated_at=datetime.now(timezone.utc),
                close_paths=(market.last.close,),
                seed=0,
                latency_ms=0,
            )
        self.last_successful_analysis = datetime.now(timezone.utc)
        self.last_analysis_error = None
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
        bot_orders = self._bot_orders(orders)
        if not positions:
            if self.last_position and self.last_position.is_open and self.last_position.managed:
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
            if bot_orders:
                await self._cancel_bot_orders(self.binding.symbol, orders)
            self.state.managed_position = False
            self._save_state()
            return PositionContext(self.binding.symbol)

        snapshot = positions[0]
        side = Side.LONG if snapshot.quantity > 0 else Side.SHORT
        owned_position = (
            self.state.managed_position
            or bool(bot_orders)
            or bool(self.last_position and self.last_position.managed)
        )
        if not owned_position:
            external = PositionContext(
                symbol=snapshot.symbol,
                side=side,
                quantity=abs(snapshot.quantity),
                entry_price=snapshot.entry_price,
                opened_at=snapshot.opened_at,
                protected=True,
                managed=False,
            )
            self.last_position = external
            return external
        position = PositionContext(
            symbol=snapshot.symbol,
            side=side,
            quantity=abs(snapshot.quantity),
            entry_price=snapshot.entry_price,
            opened_at=snapshot.opened_at,
            protected=False,
            managed=True,
        )
        stop_exists = any(
            order.client_order_id.startswith("kr_stop_")
            for order in bot_orders
        )
        target_exists = any(
            order.client_order_id.startswith("kr_take_")
            for order in bot_orders
        )
        if not stop_exists or not target_exists:
            await self._cancel_bot_orders(self.binding.symbol, bot_orders)
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
        self.state.managed_position = True
        self._save_state()
        self.last_position = protected_position
        return protected_position

    async def enter(self, intent, market, account) -> None:
        assert intent.side and self.rules
        quantity = self.risk.entry_quantity(account, market, self.rules, intent.stop_pct)
        required_margin = quantity * market.ask / Decimal(self.risk.settings.leverage)
        if required_margin > account.available_balance and account.available_balance > 0:
            affordable_notional = account.available_balance * Decimal(self.risk.settings.leverage)
            adjusted = affordable_notional / market.ask if market.ask > 0 else Decimal(0)
            quantity = min(
                self.rules.maximum_quantity,
                self.risk.entry_quantity(
                    replace(account, available_balance=account.available_balance),
                    market,
                    self.rules,
                    intent.stop_pct,
                ),
            )
            if adjusted > 0:
                from .risk import floor_to_step, ceil_to_step

                quantity = floor_to_step(adjusted, self.rules.quantity_step)
                minimum = max(
                    self.rules.minimum_quantity,
                    ceil_to_step(self.rules.minimum_notional / market.ask, self.rules.quantity_step),
                )
                if quantity < minimum:
                    self.last_analysis_error = "insufficient_available_margin"
                    return
        if quantity <= 0:
            self.last_analysis_error = "insufficient_available_margin"
            return
        request = OrderRequest(
            symbol=self.binding.symbol,
            side=intent.side.entry_order_side,
            order_type="MARKET",
            quantity=quantity,
            client_order_id=client_order_id(
                self.binding.name, intent.candle_close_time, "entry"
            ),
        )
        try:
            result = await self.exchange.submit_order(request)
        except BinanceError as exc:
            if exc.code == -4411:
                self.halted_reason = "tradfi_perps_agreement_required"
                LOG.error(
                    "binding_halted",
                    extra={
                        "symbol": self.binding.symbol,
                        "reason": self.halted_reason,
                    },
                )
                return
            raise
        ORDERS.labels(self.binding.name, "entry", result.status).inc()
        if result.status not in {"FILLED", "PARTIALLY_FILLED"}:
            return
        self.state.managed_position = True
        self._save_state()
        position = PositionContext(
            self.binding.symbol,
            intent.side,
            result.executed_quantity,
            result.average_price,
            datetime.now(timezone.utc),
            managed=True,
        )
        try:
            await self.place_protection(
                position,
                intent.candle_close_time,
                target_pct=intent.target_pct,
                stop_pct=intent.stop_pct,
            )
        except Exception:
            await self.flatten(position, "protection_failed")
            self.halted_reason = "protection_failed"
            raise
        self.last_position = replace(position, protected=True)
        self.current_max_hold_minutes = intent.max_hold_minutes
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
        self,
        position: PositionContext,
        reference_time: datetime,
        target_pct=None,
        stop_pct=None,
    ) -> None:
        assert position.side and self.rules
        stop = OrderRequest(
            symbol=position.symbol,
            side=position.side.exit_order_side,
            order_type="STOP_MARKET",
            quantity=position.quantity,
            client_order_id=client_order_id(self.binding.name, reference_time, "stop"),
            stop_price=self.risk.stop_price(
                position.entry_price,
                position.side.sign,
                self.rules.price_tick,
                stop_pct=stop_pct,
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
                position.entry_price,
                position.side.sign,
                self.rules.price_tick,
                target_pct=target_pct,
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
        if not position.is_open or not position.side or not position.managed:
            return
        await self._cancel_bot_orders(position.symbol)
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
            self.current_max_hold_minutes = None
            self.state.managed_position = False
            self._save_state()
