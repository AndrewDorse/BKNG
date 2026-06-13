from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from statistics import mean, stdev

from .domain import (
    AccountContext,
    ForecastContext,
    MarketContext,
    PositionContext,
    Side,
    SignalIntent,
)


@dataclass(frozen=True)
class KronosMeanReversionStrategy:
    """Live equivalent of the BTCUSDT 1m discovery signal."""

    name: str = "btc_kronos_mean_reversion"
    zscore_lookback: int = 30
    zscore_threshold: float = 1.0
    confidence_cutoff: Decimal = Decimal("0.0004220834304313748")
    minimum_agreement: Decimal = Decimal("0.8125")
    maximum_hold_minutes: int = 60

    def __post_init__(self) -> None:
        object.__setattr__(self, "confidence_cutoff", Decimal(str(self.confidence_cutoff)))
        object.__setattr__(self, "minimum_agreement", Decimal(str(self.minimum_agreement)))

    def evaluate(
        self,
        market: MarketContext,
        forecast: ForecastContext,
        position: PositionContext,
        account: AccountContext,
    ) -> SignalIntent:
        del account
        if position.is_open:
            return SignalIntent(
                market.symbol, market.last.close_time, None, "position_already_open"
            )
        if len(market.candles) < max(512, self.zscore_lookback):
            return SignalIntent(
                market.symbol, market.last.close_time, None, "insufficient_context"
            )
        closes = [float(candle.close) for candle in market.candles[-self.zscore_lookback :]]
        sigma = stdev(closes)
        if sigma == 0:
            return SignalIntent(market.symbol, market.last.close_time, None, "zero_volatility")
        zscore = (closes[-1] - mean(closes)) / sigma
        if abs(zscore) < self.zscore_threshold:
            return SignalIntent(market.symbol, market.last.close_time, None, "zscore_below_threshold")

        current = market.last.close
        predicted_return = forecast.median_close / current - Decimal(1)
        mean_reversion_side = Side.SHORT if zscore > 0 else Side.LONG
        kronos_side = Side.LONG if predicted_return > 0 else Side.SHORT
        agreeing_paths = sum(
            1
            for path in forecast.close_paths
            if (path > current and kronos_side is Side.LONG)
            or (path < current and kronos_side is Side.SHORT)
        )
        agreement = Decimal(agreeing_paths) / Decimal(len(forecast.close_paths))
        if abs(predicted_return) < self.confidence_cutoff:
            return SignalIntent(
                market.symbol,
                market.last.close_time,
                None,
                "forecast_below_confidence",
                abs(predicted_return),
            )
        if agreement < self.minimum_agreement:
            return SignalIntent(
                market.symbol,
                market.last.close_time,
                None,
                "forecast_agreement_below_threshold",
                abs(predicted_return),
                metadata={"agreement": str(agreement)},
            )
        if kronos_side is not mean_reversion_side:
            return SignalIntent(
                market.symbol,
                market.last.close_time,
                None,
                "forecast_disagrees",
                abs(predicted_return),
            )
        return SignalIntent(
            symbol=market.symbol,
            candle_close_time=market.last.close_time,
            side=mean_reversion_side,
            reason="kronos_mean_reversion_agreement",
            confidence=abs(predicted_return),
            metadata={
                "zscore": f"{zscore:.8f}",
                "predicted_return": str(predicted_return),
                "agreement": str(agreement),
                "maximum_hold_minutes": str(self.maximum_hold_minutes),
            },
        )
