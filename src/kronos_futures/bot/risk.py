from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_UP

from .domain import (
    AccountContext,
    ForecastContext,
    MarketContext,
    SignalIntent,
    SymbolRules,
)
from .settings import RiskSettings


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise ValueError("step must be positive")
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def ceil_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise ValueError("step must be positive")
    return (value / step).to_integral_value(rounding=ROUND_UP) * step


@dataclass(frozen=True)
class GuardedRiskEngine:
    settings: RiskSettings

    def approve_entry(
        self,
        intent: SignalIntent,
        market: MarketContext,
        forecast: ForecastContext,
        account: AccountContext,
        rules: SymbolRules,
    ) -> tuple[bool, str]:
        if intent.side is None:
            return False, intent.reason
        if account.peak_equity > 0:
            drawdown = (account.peak_equity - account.equity) / account.peak_equity
            if drawdown >= Decimal(str(self.settings.max_drawdown_pct)):
                return False, "account_drawdown_limit"
        age = (forecast.generated_at - market.last.close_time).total_seconds()
        if age < 0 or age > self.settings.maximum_signal_age_seconds:
            return False, "stale_forecast"
        midpoint = (market.bid + market.ask) / Decimal(2)
        if midpoint <= 0:
            return False, "invalid_market_price"
        spread = (market.ask - market.bid) / midpoint
        if spread > Decimal(str(self.settings.maximum_spread_pct)):
            return False, "spread_limit"
        drift = abs(midpoint / market.last.close - Decimal(1))
        if drift > Decimal(str(self.settings.maximum_price_drift_pct)):
            return False, "price_drift_limit"
        if self.settings.leverage > rules.maximum_leverage:
            return False, "leverage_above_symbol_limit"
        quantity = self.entry_quantity(account, market, rules)
        if quantity <= 0 or quantity > rules.maximum_quantity:
            return False, "invalid_order_quantity"
        required_margin = quantity * market.ask / Decimal(self.settings.leverage)
        if required_margin > account.available_balance:
            return False, "insufficient_margin_for_minimum_order"
        return True, "approved"

    def entry_quantity(
        self,
        account: AccountContext,
        market: MarketContext,
        rules: SymbolRules,
    ) -> Decimal:
        fraction = (
            Decimal(1)
            if self.settings.profile == "research_full_margin"
            else Decimal(str(self.settings.margin_fraction))
        )
        margin = account.available_balance * fraction
        notional = margin * Decimal(self.settings.leverage)
        price = market.ask
        raw = notional / price if price > 0 else Decimal(0)
        configured_quantity = floor_to_step(raw, rules.quantity_step)
        minimum_by_notional = (
            ceil_to_step(rules.minimum_notional / price, rules.quantity_step)
            if price > 0
            else Decimal(0)
        )
        minimum_trade_quantity = max(rules.minimum_quantity, minimum_by_notional)
        return min(max(configured_quantity, minimum_trade_quantity), rules.maximum_quantity)

    def stop_price(self, entry_price: Decimal, side_sign: int, tick: Decimal) -> Decimal:
        raw = entry_price * (
            Decimal(1) - Decimal(side_sign) * Decimal(str(self.settings.stop_pct))
        )
        return floor_to_step(raw, tick)

    def target_price(self, entry_price: Decimal, side_sign: int, tick: Decimal) -> Decimal:
        raw = entry_price * (
            Decimal(1) + Decimal(side_sign) * Decimal(str(self.settings.target_pct))
        )
        return floor_to_step(raw, tick)
