import sys
import types
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from kronos_futures.bot.domain import Candle


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

from kronos_futures.bot.engine import contiguous


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
