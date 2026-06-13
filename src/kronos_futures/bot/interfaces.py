from __future__ import annotations

from typing import Protocol
from decimal import Decimal

from .domain import (
    AccountContext,
    ForecastContext,
    MarketContext,
    OrderRequest,
    OrderResult,
    PositionContext,
    PositionSnapshot,
    SignalIntent,
    SymbolRules,
)


class Strategy(Protocol):
    name: str

    def evaluate(
        self,
        market: MarketContext,
        forecast: ForecastContext,
        position: PositionContext,
        account: AccountContext,
    ) -> SignalIntent: ...


class RiskEngine(Protocol):
    def approve_entry(
        self,
        intent: SignalIntent,
        market: MarketContext,
        forecast: ForecastContext,
        account: AccountContext,
        rules: SymbolRules,
    ) -> tuple[bool, str]: ...

    def entry_quantity(
        self,
        account: AccountContext,
        market: MarketContext,
        rules: SymbolRules,
    ) -> Decimal: ...


class ExchangeGateway(Protocol):
    async def synchronize_time(self) -> None: ...

    async def symbol_rules(self, symbol: str) -> SymbolRules: ...

    async def account(self) -> AccountContext: ...

    async def positions(self) -> list[PositionSnapshot]: ...

    async def open_orders(self, symbol: str | None = None) -> list[OrderResult]: ...

    async def submit_order(self, request: OrderRequest) -> OrderResult: ...

    async def query_order(self, symbol: str, client_order_id: str) -> OrderResult | None: ...

    async def cancel_all_orders(self, symbol: str) -> None: ...


class InferenceClient(Protocol):
    async def forecast(self, market: MarketContext) -> ForecastContext: ...


class PositionManager(Protocol):
    async def reconcile(self) -> None: ...

    async def flatten(self, symbol: str, reason: str) -> None: ...
