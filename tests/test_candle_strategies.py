from datetime import datetime, timedelta, timezone
from decimal import Decimal

from kronos_futures.bot.domain import (
    AccountContext,
    Candle,
    ForecastContext,
    MarketContext,
    PositionContext,
    Side,
)
from kronos_futures.bot.strategies import CompositeCandleStrategy, _rsi


def make_candles(
    closes: list[Decimal],
    *,
    interval_minutes: int = 60,
    last_open: Decimal | None = None,
    last_high: Decimal | None = None,
    last_low: Decimal | None = None,
) -> tuple[Candle, ...]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    candles = []
    for index, close in enumerate(closes):
        open_time = start + timedelta(minutes=interval_minutes * index)
        open_ = close if index != len(closes) - 1 or last_open is None else last_open
        high = close if index != len(closes) - 1 or last_high is None else last_high
        low = close if index != len(closes) - 1 or last_low is None else last_low
        candles.append(
            Candle(
                open_time=open_time,
                close_time=open_time + timedelta(minutes=interval_minutes) - timedelta(milliseconds=1),
                open=open_,
                high=high,
                low=low,
                close=close,
                volume=Decimal("1"),
                amount=Decimal("1"),
            )
        )
    return tuple(candles)


def market(candles: tuple[Candle, ...], interval: str = "1h") -> MarketContext:
    return MarketContext(
        symbol="TESTUSDT",
        interval=interval,
        candles=candles,
        bid=candles[-1].close,
        ask=candles[-1].close,
        observed_at=datetime.now(timezone.utc),
        multi_timeframe={interval: candles},
    )


def forecast() -> ForecastContext:
    return ForecastContext(datetime.now(timezone.utc), (Decimal("1"),), 0, 0)


def account() -> AccountContext:
    return AccountContext(Decimal("1000"), Decimal("1000"), Decimal("1000"), Decimal(0), 0)


def evaluate(strategy: CompositeCandleStrategy, candles: tuple[Candle, ...], interval: str = "1h"):
    return strategy.evaluate(market(candles, interval), forecast(), PositionContext("TESTUSDT"), account())


def test_range_fade_short_rule_fires_with_signal_overrides():
    strategy = CompositeCandleStrategy(
        rules=[
            {
                "name": "range_short",
                "family": "range_fade",
                "interval": "1h",
                "side": "SHORT",
                "target_pct": "0.005",
                "stop_pct": "0.02",
                "hold_candles": 4,
                "parameters": {"range": "0.02", "move": "0.003"},
            }
        ]
    )
    closes = [Decimal("100")] * 200 + [Decimal("100"), Decimal("100"), Decimal("100"), Decimal("100"), Decimal("101")]
    candles = make_candles(
        closes,
        last_open=Decimal("100"),
        last_high=Decimal("103"),
        last_low=Decimal("99"),
    )

    intent = evaluate(strategy, candles)

    assert intent.side is Side.SHORT
    assert intent.target_pct == Decimal("0.005")
    assert intent.stop_pct == Decimal("0.02")
    assert intent.max_hold_minutes == 240


def test_range_fade_long_rule_fires():
    strategy = CompositeCandleStrategy(
        rules=[
            {
                "name": "range_long",
                "family": "range_fade",
                "interval": "1h",
                "side": "LONG",
                "target_pct": "0.0075",
                "stop_pct": "0.02",
                "hold_candles": 4,
                "parameters": {"range": "0.02", "move": "0.003"},
            }
        ]
    )
    closes = [Decimal("100")] * 200 + [Decimal("100"), Decimal("100"), Decimal("100"), Decimal("100"), Decimal("99")]
    candles = make_candles(
        closes,
        last_open=Decimal("100"),
        last_high=Decimal("102"),
        last_low=Decimal("98"),
    )

    assert evaluate(strategy, candles).side is Side.LONG


def test_pullback_trend_rule_fires():
    strategy = CompositeCandleStrategy(
        rules=[
            {
                "name": "pullback",
                "family": "pullback_trend",
                "interval": "15m",
                "side": "LONG",
                "target_pct": "0.003",
                "stop_pct": "0.03",
                "hold_candles": 4,
                "parameters": {"fast": 20, "slow": 100, "rsi_period": 14, "rsi": 30},
            }
        ]
    )
    closes = [Decimal(100 + index) for index in range(185)]
    closes += [Decimal(305 - 2 * index) for index in range(20)]
    candles = make_candles(closes, interval_minutes=15)

    assert evaluate(strategy, candles, "15m").side is Side.LONG


def test_rsi2_short_rule_fires_below_ema200():
    strategy = CompositeCandleStrategy(
        rules=[
            {
                "name": "rsi_short",
                "family": "rsi2_reversion",
                "interval": "1d",
                "side": "SHORT",
                "target_pct": "0.01",
                "stop_pct": "0.10",
                "hold_candles": 20,
                "parameters": {"rsi": 20, "slow": 200},
            }
        ]
    )
    closes = [Decimal("200")] * 199 + [
        Decimal("90"), Decimal("110"), Decimal("130"),
        Decimal("150"), Decimal("170"), Decimal("190"),
    ]
    candles = make_candles(closes, interval_minutes=24 * 60)

    assert evaluate(strategy, candles, "1d").side is Side.SHORT


def test_bollinger_short_rule_fires_below_ema200():
    strategy = CompositeCandleStrategy(
        rules=[
            {
                "name": "bollinger_short",
                "family": "bollinger_reversion",
                "interval": "4h",
                "side": "SHORT",
                "target_pct": "0.015",
                "stop_pct": "0.10",
                "hold_candles": 12,
                "parameters": {"lookback": 20, "z": 1.5, "slow": 200},
            }
        ]
    )
    closes = [Decimal("200")] * 185 + [Decimal("80")] * 19 + [Decimal("100")]
    candles = make_candles(closes, interval_minutes=4 * 60)

    assert evaluate(strategy, candles, "4h").side is Side.SHORT


def test_rule_does_not_fire_before_enough_history():
    strategy = CompositeCandleStrategy(
        rules=[
            {
                "name": "range_short",
                "family": "range_fade",
                "interval": "1h",
                "side": "SHORT",
                "target_pct": "0.005",
                "stop_pct": "0.02",
                "hold_candles": 4,
                "parameters": {"range": "0.02", "move": "0.003"},
            }
        ]
    )
    candles = make_candles([Decimal("100")] * 10)

    assert evaluate(strategy, candles).side is None


def test_priority_uses_first_matching_rule():
    strategy = CompositeCandleStrategy(
        rules=[
            {
                "name": "first",
                "family": "range_fade",
                "interval": "1h",
                "side": "SHORT",
                "target_pct": "0.005",
                "stop_pct": "0.02",
                "hold_candles": 4,
                "parameters": {"range": "0.02", "move": "0.003"},
            },
            {
                "name": "second",
                "family": "range_fade",
                "interval": "1h",
                "side": "SHORT",
                "target_pct": "0.015",
                "stop_pct": "0.10",
                "hold_candles": 6,
                "parameters": {"range": "0.02", "move": "0.003"},
            },
        ]
    )
    closes = [Decimal("100")] * 200 + [Decimal("100"), Decimal("100"), Decimal("100"), Decimal("100"), Decimal("101")]
    candles = make_candles(closes, last_open=Decimal("100"), last_high=Decimal("103"), last_low=Decimal("99"))

    assert evaluate(strategy, candles).reason == "first"


def test_rsi_uses_wilder_smoothing_across_history():
    assert _rsi(
        [Decimal("100"), Decimal("101"), Decimal("100"), Decimal("101")],
        2,
    ) == Decimal("75")
