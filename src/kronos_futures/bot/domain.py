from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Mapping


class Side(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"

    @property
    def sign(self) -> int:
        return 1 if self is Side.LONG else -1

    @property
    def entry_order_side(self) -> str:
        return "BUY" if self is Side.LONG else "SELL"

    @property
    def exit_order_side(self) -> str:
        return "SELL" if self is Side.LONG else "BUY"


class TradingMode(str, Enum):
    PAPER = "paper"
    TESTNET = "testnet"
    LIVE = "live"


@dataclass(frozen=True)
class Candle:
    open_time: datetime
    close_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    amount: Decimal
    closed: bool = True


@dataclass(frozen=True)
class MarketContext:
    symbol: str
    interval: str
    candles: tuple[Candle, ...]
    bid: Decimal
    ask: Decimal
    observed_at: datetime

    @property
    def last(self) -> Candle:
        return self.candles[-1]


@dataclass(frozen=True)
class ForecastContext:
    generated_at: datetime
    close_paths: tuple[Decimal, ...]
    seed: int
    latency_ms: int

    @property
    def median_close(self) -> Decimal:
        ordered = sorted(self.close_paths)
        size = len(ordered)
        middle = size // 2
        if size % 2:
            return ordered[middle]
        return (ordered[middle - 1] + ordered[middle]) / Decimal(2)


@dataclass(frozen=True)
class PositionContext:
    symbol: str
    side: Side | None = None
    quantity: Decimal = Decimal(0)
    entry_price: Decimal = Decimal(0)
    opened_at: datetime | None = None
    protected: bool = False

    @property
    def is_open(self) -> bool:
        return self.side is not None and self.quantity > 0


@dataclass(frozen=True)
class AccountContext:
    equity: Decimal
    available_balance: Decimal
    peak_equity: Decimal
    daily_realized_pnl: Decimal
    consecutive_losses: int
    halted_until: datetime | None = None


@dataclass(frozen=True)
class SignalIntent:
    symbol: str
    candle_close_time: datetime
    side: Side | None
    reason: str
    confidence: Decimal = Decimal(0)
    metadata: Mapping[str, str] = MappingProxyType({})


@dataclass(frozen=True)
class SymbolRules:
    symbol: str
    price_tick: Decimal
    quantity_step: Decimal
    minimum_quantity: Decimal
    minimum_notional: Decimal
    maximum_quantity: Decimal
    maximum_leverage: int


@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    side: str
    order_type: str
    quantity: Decimal
    client_order_id: str
    reduce_only: bool = False
    stop_price: Decimal | None = None
    working_type: str | None = None
    close_position: bool = False


@dataclass(frozen=True)
class OrderResult:
    symbol: str
    client_order_id: str
    order_id: int
    status: str
    executed_quantity: Decimal
    average_price: Decimal
    order_type: str = ""


@dataclass(frozen=True)
class PositionSnapshot:
    symbol: str
    quantity: Decimal
    entry_price: Decimal
    isolated: bool
    leverage: int
