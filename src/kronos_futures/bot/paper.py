from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from decimal import Decimal
import json
import os
from pathlib import Path

from .binance import BinanceGateway
from .domain import AccountContext, OrderRequest, OrderResult, PositionSnapshot


class PaperGateway:
    """Uses public Binance market data while keeping all orders local."""

    def __init__(
        self,
        market_gateway: BinanceGateway,
        starting_equity: Decimal = Decimal("1000"),
        state_path: str | None = None,
    ):
        self._state_path = Path(state_path) if state_path else None
        self._cash = starting_equity
        self._fee_rate = Decimal("0.0005")
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
        self._requests: dict[str, OrderRequest] = {}
        self._leverages: dict[str, int] = {}
        self._next_order_id = 1
        self._load_state()

    def _load_state(self) -> None:
        if not self._state_path or not self._state_path.exists():
            return
        payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        self._cash = Decimal(payload["cash"])
        self._leverages = {key: int(value) for key, value in payload["leverages"].items()}
        self._next_order_id = int(payload["next_order_id"])
        self._positions = {
            symbol: PositionSnapshot(
                symbol,
                Decimal(row["quantity"]),
                Decimal(row["entry_price"]),
                bool(row["isolated"]),
                int(row["leverage"]),
                datetime.fromisoformat(row["opened_at"]) if row.get("opened_at") else None,
            )
            for symbol, row in payload.get("positions", {}).items()
        }
        self._orders = {
            key: OrderResult(
                row["symbol"], key, int(row["order_id"]), row["status"],
                Decimal(row["executed_quantity"]), Decimal(row["average_price"]),
                row["order_type"],
            )
            for key, row in payload.get("orders", {}).items()
        }
        self._requests = {
            key: OrderRequest(
                symbol=row["symbol"], side=row["side"], order_type=row["order_type"],
                quantity=Decimal(row["quantity"]), client_order_id=key,
                reduce_only=bool(row["reduce_only"]),
                stop_price=Decimal(row["stop_price"]) if row.get("stop_price") else None,
                working_type=row.get("working_type"), close_position=bool(row["close_position"]),
            )
            for key, row in payload.get("requests", {}).items()
        }

    def _save_state(self) -> None:
        if not self._state_path:
            return
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "cash": str(self._cash),
            "leverages": self._leverages,
            "next_order_id": self._next_order_id,
            "positions": {
                symbol: {
                    "quantity": str(row.quantity), "entry_price": str(row.entry_price),
                    "isolated": row.isolated, "leverage": row.leverage,
                    "opened_at": row.opened_at.isoformat() if row.opened_at else None,
                }
                for symbol, row in self._positions.items()
            },
            "orders": {
                key: {
                    "symbol": row.symbol, "order_id": row.order_id, "status": row.status,
                    "executed_quantity": str(row.executed_quantity),
                    "average_price": str(row.average_price), "order_type": row.order_type,
                }
                for key, row in self._orders.items()
            },
            "requests": {
                key: {
                    "symbol": row.symbol, "side": row.side, "order_type": row.order_type,
                    "quantity": str(row.quantity), "reduce_only": row.reduce_only,
                    "stop_price": str(row.stop_price) if row.stop_price is not None else None,
                    "working_type": row.working_type, "close_position": row.close_position,
                }
                for key, row in self._requests.items()
            },
        }
        temporary = self._state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        os.replace(temporary, self._state_path)

    def __getattr__(self, name):
        return getattr(self.market, name)

    async def account(self) -> AccountContext:
        await self._trigger_protection()
        unrealized = Decimal(0)
        reserved_margin = Decimal(0)
        for position in self._positions.values():
            bid, ask = await self.market.book_ticker(position.symbol)
            mark = bid if position.quantity > 0 else ask
            unrealized += position.quantity * (mark - position.entry_price)
            reserved_margin += (
                abs(position.quantity) * position.entry_price / Decimal(position.leverage)
            )
        equity = self._cash + unrealized
        self._account = replace(
            self._account,
            equity=equity,
            available_balance=max(Decimal(0), equity - reserved_margin),
            peak_equity=max(self._account.peak_equity, equity),
        )
        self._save_state()
        return self._account

    async def position_mode_is_one_way(self) -> bool:
        return True

    async def account_is_single_asset(self) -> bool:
        return True

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        self._leverages[symbol] = leverage
        self._save_state()

    async def symbol_is_isolated(self, symbol: str) -> bool:
        del symbol
        return True

    async def positions(self) -> list[PositionSnapshot]:
        await self._trigger_protection()
        return list(self._positions.values())

    async def open_orders(self, symbol: str | None = None) -> list[OrderResult]:
        await self._trigger_protection()
        values = [order for order in self._orders.values() if order.status == "NEW"]
        return [order for order in values if symbol is None or order.symbol == symbol]

    async def submit_order(self, request: OrderRequest) -> OrderResult:
        existing = self._orders.get(request.client_order_id)
        if existing:
            return existing
        bid, ask = await self.market.book_ticker(request.symbol)
        price = ask if request.side == "BUY" else bid
        status = (
            "NEW"
            if request.order_type in {"STOP_MARKET", "TAKE_PROFIT_MARKET"}
            else "FILLED"
        )
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
        self._requests[request.client_order_id] = request
        if request.order_type == "MARKET":
            if request.reduce_only:
                position = self._positions.pop(request.symbol, None)
                if position:
                    gross = position.quantity * (price - position.entry_price)
                    fee = abs(request.quantity * price) * self._fee_rate
                    self._cash += gross - fee
                    realized = gross - fee
                    self._account = replace(
                        self._account,
                        daily_realized_pnl=self._account.daily_realized_pnl + realized,
                        consecutive_losses=(
                            self._account.consecutive_losses + 1 if realized < 0 else 0
                        ),
                    )
            else:
                signed = request.quantity if request.side == "BUY" else -request.quantity
                self._cash -= abs(request.quantity * price) * self._fee_rate
                self._positions[request.symbol] = PositionSnapshot(
                    request.symbol,
                    signed,
                    price,
                    True,
                    self._leverages.get(request.symbol, 1),
                )
        self._save_state()
        return result

    async def query_order(self, symbol: str, client_order_id: str) -> OrderResult | None:
        order = self._orders.get(client_order_id)
        return order if order and order.symbol == symbol else None

    async def cancel_all_orders(self, symbol: str) -> None:
        for client_id, order in list(self._orders.items()):
            if order.symbol == symbol and order.status == "NEW":
                self._orders[client_id] = replace(order, status="CANCELED")
        self._save_state()

    async def _trigger_protection(self) -> None:
        for client_id, order in list(self._orders.items()):
            if order.status != "NEW" or order.order_type not in {
                "STOP_MARKET", "TAKE_PROFIT_MARKET"
            }:
                continue
            request = self._requests[client_id]
            position = self._positions.get(request.symbol)
            if not position or request.stop_price is None:
                continue
            bid, ask = await self.market.book_ticker(request.symbol)
            mark = bid if position.quantity > 0 else ask
            if order.order_type == "STOP_MARKET":
                triggered = (
                    mark <= request.stop_price
                    if position.quantity > 0
                    else mark >= request.stop_price
                )
            else:
                triggered = (
                    mark >= request.stop_price
                    if position.quantity > 0
                    else mark <= request.stop_price
                )
            if not triggered:
                continue
            gross = position.quantity * (mark - position.entry_price)
            fee = abs(position.quantity * mark) * self._fee_rate
            realized = gross - fee
            self._cash += realized
            self._account = replace(
                self._account,
                daily_realized_pnl=self._account.daily_realized_pnl + realized,
                consecutive_losses=(
                    self._account.consecutive_losses + 1 if realized < 0 else 0
                ),
            )
            self._positions.pop(request.symbol, None)
            self._orders[client_id] = replace(
                order,
                status="FILLED",
                executed_quantity=abs(position.quantity),
                average_price=mark,
            )
            for sibling_id, sibling in list(self._orders.items()):
                if sibling.symbol == request.symbol and sibling.status == "NEW":
                    self._orders[sibling_id] = replace(sibling, status="CANCELED")
            self._save_state()
