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


def test_entry_quantity_uses_fixed_margin_without_compounding():
    account, market, rules = contexts("100", "50000")
    risk = GuardedRiskEngine(
        RiskSettings(
            leverage=50,
            margin_fraction=1.0,
            fixed_margin_usdt=20.0,
        )
    )

    first = risk.entry_quantity(account, market, rules)
    larger_account = AccountContext(
        equity=Decimal("1000"),
        available_balance=Decimal("1000"),
        peak_equity=Decimal("1000"),
        daily_realized_pnl=Decimal(0),
        consecutive_losses=0,
    )
    second = risk.entry_quantity(larger_account, market, rules)

    assert first == Decimal("0.020")
    assert second == first


def test_entry_quantity_uses_half_available_balance():
    account, market, rules = contexts("10", "64000")
    risk = GuardedRiskEngine(
        RiskSettings(
            leverage=50,
            margin_fraction=0.50,
        )
    )

    quantity = risk.entry_quantity(account, market, rules)
    required_margin = quantity * market.ask / Decimal(50)

    assert quantity == Decimal("0.003")
    assert required_margin == Decimal("3.840")
    assert required_margin <= account.available_balance * Decimal("0.50")
