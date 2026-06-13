from datetime import datetime, timezone
from decimal import Decimal

from kronos_futures.bot.domain import AccountContext, Candle, MarketContext, SymbolRules
from kronos_futures.bot.risk import GuardedRiskEngine
from kronos_futures.bot.settings import RiskSettings


def contexts(balance: str, ask: str):
    now = datetime.now(timezone.utc)
    candle = Candle(
        open_time=now,
        close_time=now,
        open=Decimal(ask),
        high=Decimal(ask),
        low=Decimal(ask),
        close=Decimal(ask),
        volume=Decimal("1"),
        amount=Decimal("1"),
    )
    account = AccountContext(
        equity=Decimal(balance),
        available_balance=Decimal(balance),
        peak_equity=Decimal(balance),
        daily_realized_pnl=Decimal(0),
        consecutive_losses=0,
    )
    market = MarketContext(
        symbol="BTCUSDT",
        interval="1m",
        candles=(candle,),
        bid=Decimal(ask),
        ask=Decimal(ask),
        observed_at=now,
    )
    rules = SymbolRules(
        symbol="BTCUSDT",
        price_tick=Decimal("0.10"),
        quantity_step=Decimal("0.001"),
        minimum_quantity=Decimal("0.001"),
        minimum_notional=Decimal("100"),
        maximum_quantity=Decimal("1000"),
        maximum_leverage=125,
    )
    return account, market, rules


def test_entry_quantity_uses_ten_percent_balance_when_above_minimum():
    account, market, rules = contexts("100", "50000")
    risk = GuardedRiskEngine(RiskSettings(leverage=50, margin_fraction=0.10))

    assert risk.entry_quantity(account, market, rules) == Decimal("0.010")


def test_entry_quantity_raises_small_order_to_exchange_minimum():
    account, market, rules = contexts("20", "100000")
    risk = GuardedRiskEngine(RiskSettings(leverage=50, margin_fraction=0.10))

    assert risk.entry_quantity(account, market, rules) == Decimal("0.001")
