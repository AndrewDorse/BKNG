import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from kronos_futures.bot.domain import (
    AccountContext,
    OrderResult,
    PositionSnapshot,
    Side,
    SymbolRules,
)
from kronos_futures.bot.portfolio import (
    PortfolioTradingEngine,
    rank_targets,
    rebalance_due,
)
from kronos_futures.bot.settings import PortfolioSettings


SYMBOLS = tuple(f"S{index}USDT" for index in range(15))


def settings(**overrides):
    values = {"name": "portfolio", "symbols": SYMBOLS}
    values.update(overrides)
    return PortfolioSettings(**values)


def account(equity="100"):
    value = Decimal(equity)
    return AccountContext(value, value, value, Decimal(0), 0)


def rules(symbol="S0USDT"):
    return SymbolRules(
        symbol,
        Decimal("0.01"),
        Decimal("0.001"),
        Decimal("0.001"),
        Decimal("5"),
        Decimal("1000"),
        20,
    )


def test_rank_targets_selects_bottom_and_top_three():
    closes = {
        symbol: tuple([Decimal("100")] * 24 + [Decimal(100 + index)])
        for index, symbol in enumerate(SYMBOLS)
    }

    targets = rank_targets(closes, 24, 3)

    assert [symbol for symbol, side in targets.items() if side is Side.SHORT] == list(SYMBOLS[:3])
    assert [symbol for symbol, side in targets.items() if side is Side.LONG] == list(SYMBOLS[-3:])


def test_rebalance_schedule_is_fixed_to_unix_utc():
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    assert rebalance_due(epoch, 18)
    assert not rebalance_due(epoch + timedelta(hours=4), 18)
    assert rebalance_due(epoch + timedelta(hours=72), 18)


class Exchange:
    mode = type("Mode", (), {"value": "paper"})()

    def __init__(self):
        self.position_rows = []
        self.order_rows = []
        self.submissions = []
        self.account_row = account()

    async def positions(self):
        return list(self.position_rows)

    async def open_orders(self, symbol=None):
        return [row for row in self.order_rows if symbol is None or row.symbol == symbol]

    async def cancel_all_orders(self, symbol):
        self.order_rows = [row for row in self.order_rows if row.symbol != symbol]

    async def account(self):
        return self.account_row

    async def book_ticker(self, symbol):
        return Decimal("99.99"), Decimal("100.01")

    async def submit_order(self, request):
        self.submissions.append(request)
        if request.order_type == "MARKET":
            result = OrderResult(
                request.symbol, request.client_order_id, len(self.submissions), "FILLED",
                request.quantity, Decimal("100"), request.order_type
            )
        else:
            result = OrderResult(
                request.symbol, request.client_order_id, len(self.submissions), "NEW",
                Decimal(0), Decimal(0), request.order_type
            )
            self.order_rows.append(result)
        return result


def test_entry_is_persisted_and_protected(tmp_path):
    exchange = Exchange()
    engine = PortfolioTradingEngine(settings(), exchange, str(tmp_path / "state.json"))
    engine.rules["S0USDT"] = rules()
    now = datetime.now(timezone.utc)

    asyncio.run(
        engine._enter(
            "S0USDT", Side.LONG, Decimal("100"), now, now + timedelta(seconds=1), account()
        )
    )

    assert engine.state.positions["S0USDT"]["status"] == "protected"
    assert [request.order_type for request in exchange.submissions] == ["MARKET", "STOP_MARKET"]
    assert exchange.submissions[0].quantity == Decimal("0.099")
    assert exchange.submissions[1].close_position is True


def test_reconcile_rejects_unknown_position(tmp_path):
    exchange = Exchange()
    exchange.position_rows = [
        PositionSnapshot("S0USDT", Decimal("1"), Decimal("100"), True, 10)
    ]
    engine = PortfolioTradingEngine(settings(), exchange, str(tmp_path / "state.json"))

    with pytest.raises(RuntimeError, match="Unknown portfolio positions"):
        asyncio.run(engine.reconcile(restore_protection=True))


def test_reconcile_rejects_unmanaged_order(tmp_path):
    exchange = Exchange()
    exchange.order_rows = [
        OrderResult(
            "S0USDT", "manual-order", 1, "NEW", Decimal(0), Decimal(0), "LIMIT"
        )
    ]
    engine = PortfolioTradingEngine(settings(), exchange, str(tmp_path / "state.json"))

    with pytest.raises(RuntimeError, match="Unmanaged open orders"):
        asyncio.run(engine.reconcile(restore_protection=True))


def test_reconcile_restores_missing_stop(tmp_path):
    exchange = Exchange()
    exchange.position_rows = [
        PositionSnapshot("S0USDT", Decimal("1"), Decimal("100"), True, 10)
    ]
    engine = PortfolioTradingEngine(settings(), exchange, str(tmp_path / "state.json"))
    engine.rules["S0USDT"] = rules()
    engine.state.positions["S0USDT"] = {"status": "filled_unprotected"}

    asyncio.run(engine.reconcile(restore_protection=True))

    assert exchange.submissions[-1].order_type == "STOP_MARKET"


def test_drawdown_kill_halts_before_new_market_data(tmp_path):
    exchange = Exchange()
    exchange.account_row = account("84")
    engine = PortfolioTradingEngine(settings(), exchange, str(tmp_path / "state.json"))
    engine.state.peak_equity = "100"

    asyncio.run(engine.cycle())

    assert engine.state.halted_reason == "portfolio_drawdown_kill"


def test_flatten_retries_partial_fill_until_position_is_zero(tmp_path):
    class PartialExchange(Exchange):
        def __init__(self):
            super().__init__()
            self.position_rows = [
                PositionSnapshot("S0USDT", Decimal("1"), Decimal("100"), True, 10)
            ]

        async def submit_order(self, request):
            self.submissions.append(request)
            if len(self.submissions) == 1:
                self.position_rows[0] = PositionSnapshot(
                    "S0USDT", Decimal("0.6"), Decimal("100"), True, 10
                )
                return OrderResult(
                    request.symbol, request.client_order_id, 1, "PARTIALLY_FILLED",
                    Decimal("0.4"), Decimal("100"), "MARKET"
                )
            self.position_rows = []
            return OrderResult(
                request.symbol, request.client_order_id, 2, "FILLED",
                request.quantity, Decimal("100"), "MARKET"
            )

    exchange = PartialExchange()
    engine = PortfolioTradingEngine(settings(), exchange, str(tmp_path / "state.json"))
    engine.state.positions["S0USDT"] = {"status": "protected"}

    asyncio.run(engine.flatten_all("test"))

    assert len(exchange.submissions) == 2
    assert engine.state.positions == {}
    assert engine.state.closed_trades == 1
