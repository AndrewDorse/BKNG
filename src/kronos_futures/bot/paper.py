from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from .binance import BinanceGateway
from .domain import AccountContext, OrderRequest, OrderResult, PositionSnapshot


class PaperGateway:
    """Uses public Binance market data while keeping all orders local."""

    def __init__(self, market_gateway: BinanceGateway, starting_equity: Decimal = Decimal("1000")):
        self.market = market_gateway
        self._account = AccountContext(
            equity=starting_equity,
            available_balance=starting_equity,
            peak_equity=starting_equity,
            daily_realized_pnl=Decimal(0),
            consecutive_losses=0,
        )
        self._positions: dict[str, PositionSnapshot] = {}
        self._orders: dict[str, OrderResult] = {}
        self._next_order_id = 1

    def __getattr__(self, name):
        return getattr(self.market, name)

    async def account(self) -> AccountContext:
        return self._account

    async def position_mode_is_one_way(self) -> bool:
        return True

    async def account_is_single_asset(self) -> bool:
        return True

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        del symbol, leverage

    async def symbol_is_isolated(self, symbol: str) -> bool:
        del symbol
        return True

    async def positions(self) -> list[PositionSnapshot]:
        return list(self._positions.values())

    async def open_orders(self, symbol: str | None = None) -> list[OrderResult]:
        values = [order for order in self._orders.values() if order.status == "NEW"]
        return [order for order in values if symbol is None or order.symbol == symbol]

    async def submit_order(self, request: OrderRequest) -> OrderResult:
        existing = self._orders.get(request.client_order_id)
        if existing:
            return existing
        bid, ask = await self.market.book_ticker(request.symbol)
        price = ask if request.side == "BUY" else bid
        status = "NEW" if request.order_type == "STOP_MARKET" else "FILLED"
        result = OrderResult(
            symbol=request.symbol,
            client_order_id=request.client_order_id,
            order_id=self._next_order_id,
            status=status,
            executed_quantity=Decimal(0) if status == "NEW" else request.quantity,
            average_price=Decimal(0) if status == "NEW" else price,
            order_type=request.order_type,
        )
        self._next_order_id += 1
        self._orders[request.client_order_id] = result
        if request.order_type == "MARKET":
            if request.reduce_only:
                self._positions.pop(request.symbol, None)
            else:
                signed = request.quantity if request.side == "BUY" else -request.quantity
                self._positions[request.symbol] = PositionSnapshot(
                    request.symbol, signed, price, True, 50
                )
        return result

    async def query_order(self, symbol: str, client_order_id: str) -> OrderResult | None:
        order = self._orders.get(client_order_id)
        return order if order and order.symbol == symbol else None

    async def cancel_all_orders(self, symbol: str) -> None:
        for client_id, order in list(self._orders.items()):
            if order.symbol == symbol and order.status == "NEW":
                self._orders[client_id] = replace(order, status="CANCELED")
