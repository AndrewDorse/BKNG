from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx

from .domain import OrderRequest, PositionSnapshot, Side, SymbolRules
from .engine import contiguous
from .risk import ceil_to_step, floor_to_step
from .settings import PortfolioSettings


LOG = logging.getLogger(__name__)
FOUR_HOURS = 4 * 60 * 60


def portfolio_order_id(
    portfolio: str, symbol: str, rebalance_open: datetime, purpose: str
) -> str:
    raw = f"{portfolio}|{symbol}|{rebalance_open.isoformat()}|{purpose}".encode()
    return f"kp_{purpose[:3]}_{hashlib.sha256(raw).hexdigest()[:22]}"


def rebalance_due(next_open: datetime, rebalance_bars: int) -> bool:
    return int(next_open.timestamp() // FOUR_HOURS) % rebalance_bars == 0


def rank_targets(
    closes: dict[str, tuple[Decimal, ...]], lookback: int, count: int
) -> dict[str, Side]:
    momentum = {
        symbol: values[-1] / values[-1 - lookback] - Decimal(1)
        for symbol, values in closes.items()
        if len(values) > lookback and values[-1 - lookback] > 0
    }
    if len(momentum) < count * 2:
        raise RuntimeError("Insufficient synchronized symbols for portfolio ranking")
    ordered = sorted(momentum, key=lambda symbol: (momentum[symbol], symbol))
    result = {symbol: Side.SHORT for symbol in ordered[:count]}
    result.update({symbol: Side.LONG for symbol in ordered[-count:]})
    return result


@dataclass
class PortfolioState:
    strategy_id: str
    started_at: str | None = None
    peak_equity: str = "0"
    max_drawdown_pct: float = 0.0
    closed_trades: int = 0
    unprotected_positions: int = 0
    reconciliation_errors: int = 0
    last_rebalance_open: str | None = None
    halted_reason: str | None = None
    positions: dict[str, dict] | None = None

    def __post_init__(self) -> None:
        if self.positions is None:
            self.positions = {}
        if self.started_at is None:
            self.started_at = datetime.now(timezone.utc).isoformat()


class PortfolioTradingEngine:
    def __init__(self, settings: PortfolioSettings, exchange, state_path: str, poll_seconds=30):
        self.settings = settings
        self.exchange = exchange
        self.state_path = Path(state_path)
        self.poll_seconds = poll_seconds
        self.running = True
        self.ready = False
        self.last_analysis_error: str | None = None
        self.last_analyzed_candle: datetime | None = None
        self.rules: dict[str, SymbolRules] = {}
        self.state = self._load_state()

    def _load_state(self) -> PortfolioState:
        if not self.state_path.exists():
            return PortfolioState(self.settings.strategy_id)
        payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        if payload.get("strategy_id") != self.settings.strategy_id:
            raise RuntimeError("Persisted portfolio state belongs to another strategy")
        return PortfolioState(**payload)

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "strategy_id": self.state.strategy_id,
                    "started_at": self.state.started_at,
                    "peak_equity": self.state.peak_equity,
                    "max_drawdown_pct": self.state.max_drawdown_pct,
                    "closed_trades": self.state.closed_trades,
                    "unprotected_positions": self.state.unprotected_positions,
                    "reconciliation_errors": self.state.reconciliation_errors,
                    "last_rebalance_open": self.state.last_rebalance_open,
                    "halted_reason": self.state.halted_reason,
                    "positions": self.state.positions,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        os.replace(temporary, self.state_path)

    async def preflight(self) -> None:
        await self.exchange.synchronize_time()
        if not await self.exchange.position_mode_is_one_way():
            raise RuntimeError("Binance account must use one-way position mode")
        if not await self.exchange.account_is_single_asset():
            raise RuntimeError("Binance account must use single-asset margin mode")
        await self.reconcile(restore_protection=False)
        for symbol in self.settings.symbols:
            if not await self.exchange.symbol_is_isolated(symbol):
                raise RuntimeError(f"{symbol} must use isolated margin")
            rules = await self.exchange.symbol_rules(symbol)
            if self.settings.leverage > rules.maximum_leverage:
                raise RuntimeError(f"{symbol} does not support configured leverage")
            self.rules[symbol] = rules
            await self.exchange.set_leverage(symbol, self.settings.leverage)
        candles_by_symbol = await asyncio.gather(
            *(
                self.exchange.klines(
                    symbol, self.settings.interval, self.settings.lookback_bars + 2
                )
                for symbol in self.settings.symbols
            )
        )
        for symbol, candles in zip(self.settings.symbols, candles_by_symbol):
            if not contiguous(candles, FOUR_HOURS, self.settings.lookback_bars + 1):
                raise RuntimeError(f"{symbol} 4h candle history is not contiguous")
        await self.reconcile(restore_protection=True)
        account = await self.exchange.account()
        peak = max(Decimal(self.state.peak_equity), account.equity)
        self.state.peak_equity = str(peak)
        drawdown_pct = float(max(Decimal(0), (Decimal(1) - account.equity / peak) * 100))
        self.state.max_drawdown_pct = max(self.state.max_drawdown_pct, drawdown_pct)
        self._save_state()
        self.ready = True
        LOG.info(
            "portfolio_ready",
            extra={
                "symbols": len(self.settings.symbols),
                "leverage": self.settings.leverage,
                "margin_fraction": self.settings.margin_fraction,
            },
        )

    async def run(self) -> None:
        try:
            await self.preflight()
            while self.running:
                started = asyncio.get_running_loop().time()
                try:
                    await self.cycle()
                    self.last_analysis_error = None
                except httpx.TimeoutException as exc:
                    self.last_analysis_error = type(exc).__name__
                    LOG.error("portfolio_cycle_timeout", extra={"reason": type(exc).__name__})
                except Exception:
                    self.last_analysis_error = "portfolio_cycle_failed"
                    LOG.exception("portfolio_cycle_failed")
                elapsed = asyncio.get_running_loop().time() - started
                await asyncio.sleep(max(1, self.poll_seconds - elapsed))
        finally:
            self.ready = False

    async def cycle(self) -> None:
        await self.reconcile(restore_protection=True)
        account = await self.exchange.account()
        peak = max(Decimal(self.state.peak_equity), account.equity)
        self.state.peak_equity = str(peak)
        if peak > 0 and account.equity / peak - Decimal(1) <= -Decimal(
            str(self.settings.max_portfolio_drawdown_pct)
        ):
            await self.flatten_all("portfolio_drawdown_kill")
            self.state.halted_reason = "portfolio_drawdown_kill"
            self._save_state()
        if self.state.halted_reason:
            return

        required = self.settings.lookback_bars + 1
        batches = await asyncio.gather(
            *(
                self.exchange.klines(symbol, self.settings.interval, required + 1)
                for symbol in self.settings.symbols
            )
        )
        synchronized: dict[str, tuple] = {}
        latest_times = set()
        for symbol, batch in zip(self.settings.symbols, batches):
            candles = tuple(batch[-required:])
            if not contiguous(candles, FOUR_HOURS, required):
                raise RuntimeError(f"Non-contiguous 4h candles for {symbol}")
            synchronized[symbol] = candles
            latest_times.add(candles[-1].open_time)
        if len(latest_times) != 1:
            raise RuntimeError("Portfolio candles are not synchronized")
        latest_open = latest_times.pop()
        self.last_analyzed_candle = latest_open
        next_open = latest_open + timedelta(hours=4)
        if not rebalance_due(next_open, self.settings.rebalance_bars):
            return
        if self.state.last_rebalance_open == next_open.isoformat():
            return
        latest_close = next(iter(synchronized.values()))[-1].close_time
        if datetime.now(timezone.utc) - latest_close > timedelta(minutes=5):
            raise RuntimeError("Refusing stale portfolio rebalance")

        targets = rank_targets(
            {
                symbol: tuple(candle.close for candle in candles)
                for symbol, candles in synchronized.items()
            },
            self.settings.lookback_bars,
            self.settings.positions_per_side,
        )
        await self.flatten_all("scheduled_rebalance")
        account = await self.exchange.account()
        opened: list[str] = []
        try:
            for symbol, side in targets.items():
                latest = synchronized[symbol][-1]
                await self._enter(symbol, side, latest.close, latest.close_time, next_open, account)
                opened.append(symbol)
        except Exception:
            LOG.exception("portfolio_rebalance_incomplete")
            await self.flatten_all("rebalance_incomplete")
            self.state.halted_reason = "rebalance_incomplete"
            self._save_state()
            raise
        self.state.last_rebalance_open = next_open.isoformat()
        self._save_state()

    async def reconcile(self, restore_protection: bool) -> dict[str, PositionSnapshot]:
        all_snapshots = {item.symbol: item for item in await self.exchange.positions()}
        all_orders = await self.exchange.open_orders()
        unmanaged_orders = [
            order.client_order_id
            for order in all_orders
            if not order.client_order_id.startswith("kp_")
        ]
        if unmanaged_orders:
            self.state.reconciliation_errors += 1
            self.state.halted_reason = "unmanaged_orders"
            self._save_state()
            raise RuntimeError(f"Unmanaged open orders: {unmanaged_orders}")
        outside = sorted(set(all_snapshots).difference(self.settings.symbols))
        if outside:
            self.state.reconciliation_errors += 1
            self.state.halted_reason = f"outside_positions:{','.join(outside)}"
            self._save_state()
            raise RuntimeError(f"Unmanaged positions outside portfolio: {outside}")
        snapshots = dict(all_snapshots)
        owned = self.state.positions or {}
        orphan_order_symbols = {
            order.symbol
            for order in all_orders
            if order.symbol not in owned and order.symbol not in all_snapshots
        }
        for symbol in sorted(orphan_order_symbols):
            await self.exchange.cancel_all_orders(symbol)
        unknown = sorted(set(snapshots).difference(owned))
        if unknown:
            self.state.reconciliation_errors += 1
            self.state.halted_reason = f"unknown_positions:{','.join(unknown)}"
            self._save_state()
            raise RuntimeError(f"Unknown portfolio positions: {unknown}")
        for symbol in list(owned):
            if symbol not in snapshots:
                await self.exchange.cancel_all_orders(symbol)
                previous = owned.pop(symbol, None) or {}
                if previous.get("status") != "entry_intent":
                    self.state.closed_trades += 1
                LOG.info("deal_closed", extra={"symbol": symbol, "reason": "exchange_position_closed"})
        for symbol, snapshot in snapshots.items():
            orders = await self.exchange.open_orders(symbol)
            stop_exists = any(
                order.client_order_id.startswith("kp_sto_")
                and order.order_type == "STOP_MARKET"
                for order in orders
            )
            if restore_protection and not stop_exists:
                self.state.unprotected_positions += 1
                self._save_state()
                await self._place_stop(snapshot, datetime.now(timezone.utc))
        self.state.positions = owned
        self._save_state()
        return snapshots

    async def _enter(
        self,
        symbol: str,
        side: Side,
        candle_close: Decimal,
        candle_close_time: datetime,
        rebalance_open: datetime,
        account,
    ) -> None:
        bid, ask = await self.exchange.book_ticker(symbol)
        midpoint = (bid + ask) / Decimal(2)
        if midpoint <= 0:
            raise RuntimeError(f"Invalid midpoint for {symbol}")
        if (ask - bid) / midpoint > Decimal(str(self.settings.maximum_spread_pct)):
            raise RuntimeError(f"Spread gate failed for {symbol}")
        if abs(midpoint / candle_close - Decimal(1)) > Decimal(
            str(self.settings.maximum_price_drift_pct)
        ):
            raise RuntimeError(f"Price drift gate failed for {symbol}")
        rules = self.rules[symbol]
        margin = account.equity * Decimal(str(self.settings.margin_fraction))
        notional = margin * Decimal(self.settings.leverage)
        entry_price = ask if side is Side.LONG else bid
        configured = floor_to_step(notional / entry_price, rules.quantity_step)
        minimum = max(
            rules.minimum_quantity,
            ceil_to_step(rules.minimum_notional / entry_price, rules.quantity_step),
        )
        quantity = max(configured, minimum)
        actual_margin = quantity * entry_price / Decimal(self.settings.leverage)
        if actual_margin > account.equity * Decimal("0.02"):
            raise RuntimeError(f"Minimum order exceeds 2% equity for {symbol}")
        request = OrderRequest(
            symbol=symbol,
            side=side.entry_order_side,
            order_type="MARKET",
            quantity=quantity,
            client_order_id=portfolio_order_id(
                self.settings.name, symbol, rebalance_open, "entry"
            ),
        )
        assert self.state.positions is not None
        self.state.positions[symbol] = {
            "side": side.value,
            "quantity": "0",
            "entry_price": "0",
            "opened_at": None,
            "signal_close_time": candle_close_time.isoformat(),
            "status": "entry_intent",
        }
        self._save_state()
        result = await self.exchange.submit_order(request)
        if result.status not in {"FILLED", "PARTIALLY_FILLED"} or result.executed_quantity <= 0:
            self.state.positions.pop(symbol, None)
            self._save_state()
            raise RuntimeError(f"Entry was not filled for {symbol}: {result.status}")
        snapshot = PositionSnapshot(
            symbol=symbol,
            quantity=result.executed_quantity * side.sign,
            entry_price=result.average_price,
            isolated=True,
            leverage=self.settings.leverage,
            opened_at=datetime.now(timezone.utc),
        )
        self.state.positions[symbol] = {
            "side": side.value,
            "quantity": str(result.executed_quantity),
            "entry_price": str(result.average_price),
            "opened_at": datetime.now(timezone.utc).isoformat(),
            "signal_close_time": candle_close_time.isoformat(),
            "status": "filled_unprotected",
        }
        self._save_state()
        try:
            await self._place_stop(snapshot, rebalance_open)
        except Exception:
            self.state.unprotected_positions += 1
            self._save_state()
            raise
        self.state.positions[symbol]["status"] = "protected"
        self._save_state()
        LOG.info(
            "deal_opened",
            extra={
                "symbol": symbol,
                "side": side.value,
                "quantity": str(result.executed_quantity),
                "price": str(result.average_price),
            },
        )

    async def _place_stop(self, snapshot: PositionSnapshot, reference: datetime) -> None:
        side = Side.LONG if snapshot.quantity > 0 else Side.SHORT
        rules = self.rules[snapshot.symbol]
        raw_stop = snapshot.entry_price * (
            Decimal(1) - Decimal(side.sign) * Decimal(str(self.settings.stop_pct))
        )
        stop_price = floor_to_step(raw_stop, rules.price_tick)
        result = await self.exchange.submit_order(
            OrderRequest(
                symbol=snapshot.symbol,
                side=side.exit_order_side,
                order_type="STOP_MARKET",
                quantity=abs(snapshot.quantity),
                client_order_id=portfolio_order_id(
                    self.settings.name, snapshot.symbol, reference, "stop"
                ),
                stop_price=stop_price,
                working_type="MARK_PRICE",
                close_position=True,
            )
        )
        if result.status not in {"NEW", "PARTIALLY_FILLED", "FILLED"}:
            raise RuntimeError(f"Protective stop rejected for {snapshot.symbol}")

    async def flatten_all(self, reason: str) -> None:
        snapshots = {
            item.symbol: item
            for item in await self.exchange.positions()
            if item.symbol in self.settings.symbols
        }
        for symbol, snapshot in snapshots.items():
            await self.exchange.cancel_all_orders(symbol)
            side = Side.LONG if snapshot.quantity > 0 else Side.SHORT
            result = None
            for attempt in range(3):
                current = next(
                    (
                        item
                        for item in await self.exchange.positions()
                        if item.symbol == symbol
                    ),
                    None,
                )
                if current is None:
                    break
                result = await self.exchange.submit_order(
                    OrderRequest(
                        symbol=symbol,
                        side=side.exit_order_side,
                        order_type="MARKET",
                        quantity=abs(current.quantity),
                        client_order_id=portfolio_order_id(
                            self.settings.name,
                            symbol,
                            datetime.now(timezone.utc),
                            f"exit{attempt}",
                        ),
                        reduce_only=True,
                    )
                )
                if result.status not in {"FILLED", "PARTIALLY_FILLED"}:
                    raise RuntimeError(f"Could not flatten {symbol}: {result.status}")
            residual = next(
                (item for item in await self.exchange.positions() if item.symbol == symbol),
                None,
            )
            if residual is not None:
                raise RuntimeError(f"Residual position remains after flatten: {symbol}")
            self.state.closed_trades += 1
            LOG.info(
                "deal_closed",
                extra={
                    "symbol": symbol,
                    "side": side.value,
                    "quantity": str(result.executed_quantity if result else snapshot.quantity),
                    "price": str(result.average_price if result else snapshot.entry_price),
                    "reason": reason,
                },
            )
        self.state.positions = {}
        self._save_state()
