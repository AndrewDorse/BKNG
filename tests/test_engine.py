import sys
import types
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from kronos_futures.bot.binance import BinanceError
from kronos_futures.bot.domain import (
    AccountContext,
    Candle,
    MarketContext,
    Side,
    SignalIntent,
    SymbolRules,
)
from kronos_futures.bot.risk import GuardedRiskEngine
from kronos_futures.bot.settings import BindingSettings, RiskSettings


class Metric:
    def __init__(self, *args, **kwargs):
        pass

    def labels(self, *args, **kwargs):
        return self

    def set(self, *args, **kwargs):
        pass

    def observe(self, *args, **kwargs):
        pass


sys.modules.setdefault(
    "prometheus_client",
    types.SimpleNamespace(Counter=Metric, Gauge=Metric, Histogram=Metric),
)

from kronos_futures.bot.engine import TradingEngine, contiguous, interval_seconds  # noqa: E402


def candle(open_time: datetime, *, closed: bool = True) -> Candle:
    return Candle(
        open_time=open_time,
        close_time=open_time + timedelta(minutes=1) - timedelta(milliseconds=1),
        open=Decimal("1"),
        high=Decimal("1"),
        low=Decimal("1"),
        close=Decimal("1"),
        volume=Decimal("1"),
        amount=Decimal("1"),
        closed=closed,
    )


def test_contiguous_accepts_complete_minute_history():
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    candles = tuple(candle(start + timedelta(minutes=index)) for index in range(512))

    assert contiguous(candles, 60) is True


def test_contiguous_rejects_missing_minute():
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    candles = tuple(
        candle(start + timedelta(minutes=index + (1 if index >= 256 else 0)))
        for index in range(512)
    )

    assert contiguous(candles, 60) is False


def test_interval_seconds_supports_strategy_timeframes():
    assert interval_seconds("15m") == 15 * 60
    assert interval_seconds("1h") == 60 * 60
    assert interval_seconds("4h") == 4 * 60 * 60
    assert interval_seconds("1d") == 24 * 60 * 60


def test_tradfi_agreement_rejection_halts_only_binding():
    now = datetime.now(timezone.utc)
    market = MarketContext(
        symbol="COINUSDT",
        interval="15m",
        candles=(candle(now),),
        bid=Decimal("100"),
        ask=Decimal("100"),
        observed_at=now,
    )
    account = AccountContext(
        equity=Decimal("1000"),
        available_balance=Decimal("1000"),
        peak_equity=Decimal("1000"),
        daily_realized_pnl=Decimal(0),
        consecutive_losses=0,
    )
    binding = BindingSettings(
        name="coin",
        strategy="unused",
        symbol="COINUSDT",
        interval="15m",
        risk=RiskSettings(leverage=10, margin_fraction=0.05),
    )

    class Exchange:
        async def submit_order(self, request):
            raise BinanceError(400, -4411, "TradFi agreement required")

    engine = TradingEngine(
        binding,
        strategy=None,
        risk=GuardedRiskEngine(binding.risk),
        exchange=Exchange(),
        inference=None,
    )
    engine.rules = SymbolRules(
        symbol="COINUSDT",
        price_tick=Decimal("0.01"),
        quantity_step=Decimal("0.01"),
        minimum_quantity=Decimal("0.01"),
        minimum_notional=Decimal("5"),
        maximum_quantity=Decimal("1000"),
        maximum_leverage=10,
    )
    intent = SignalIntent("COINUSDT", now, Side.LONG, "test")

    import asyncio

    asyncio.run(engine.enter(intent, market, account))

    assert engine.halted_reason == "tradfi_perps_agreement_required"
