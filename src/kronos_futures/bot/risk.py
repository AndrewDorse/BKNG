from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN

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
        now = datetime.now(timezone.utc)
        if intent.side is None:
            return False, intent.reason
        if account.halted_until and account.halted_until > now:
            return False, "risk_halt_active"
        if account.daily_realized_pnl < -(account.peak_equity * Decimal(str(
            self.settings.max_daily_loss_pct
        ))):
            return False, "daily_loss_limit"
        if account.peak_equity > 0:
            drawdown = (account.peak_equity - account.equity) / account.peak_equity
            if drawdown >= Decimal(str(self.settings.max_drawdown_pct)):
                return False, "account_drawdown_limit"
        if account.consecutive_losses >= self.settings.consecutive_loss_limit:
            return False, "consecutive_loss_limit"
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
        if quantity < rules.minimum_quantity:
            return False, "quantity_below_minimum"
        if quantity * market.ask < rules.minimum_notional:
            return False, "notional_below_minimum"
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
        return min(floor_to_step(raw, rules.quantity_step), rules.maximum_quantity)

    def stop_price(self, entry_price: Decimal, side_sign: int, tick: Decimal) -> Decimal:
        raw = entry_price * (
            Decimal(1) - Decimal(side_sign) * Decimal(str(self.settings.stop_pct))
        )
        return floor_to_step(raw, tick)
