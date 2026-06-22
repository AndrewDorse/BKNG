from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from statistics import mean, stdev
from types import MappingProxyType

from .domain import (
    AccountContext,
    ForecastContext,
    MarketContext,
    PositionContext,
    Side,
    SignalIntent,
)


INTERVAL_SECONDS = {
    "1m": 60,
    "15m": 15 * 60,
    "1h": 60 * 60,
    "2h": 2 * 60 * 60,
    "4h": 4 * 60 * 60,
    "1d": 24 * 60 * 60,
}


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


@dataclass(frozen=True)
class CandleRule:
    name: str
    family: str
    interval: str
    side: Side
    target_pct: Decimal
    stop_pct: Decimal
    hold_candles: int
    parameters: MappingProxyType

    @property
    def priority(self) -> int:
        return int(self.parameters.get("priority", 999))

    @property
    def enabled(self) -> bool:
        return bool(self.parameters.get("enabled", True))

    @property
    def strategy_id(self) -> str:
        return str(self.parameters.get("strategy_id", self.name))

    @property
    def max_hold_minutes(self) -> int:
        return int(self.hold_candles * INTERVAL_SECONDS[self.interval] / 60)


def _ema(values: list[Decimal], span: int) -> Decimal:
    alpha = Decimal(2) / Decimal(span + 1)
    result = values[0]
    for value in values[1:]:
        result = value * alpha + result * (Decimal(1) - alpha)
    return result


def _rsi(values: list[Decimal], period: int) -> Decimal | None:
    if len(values) <= period:
        return None
    alpha = Decimal(1) / Decimal(period)
    average_gain = Decimal(0)
    average_loss = Decimal(0)
    initialized = False
    for left, right in zip(values, values[1:]):
        delta = right - left
        gain = max(delta, Decimal(0))
        loss = max(-delta, Decimal(0))
        if not initialized:
            average_gain = gain
            average_loss = loss
            initialized = True
        else:
            average_gain = gain * alpha + average_gain * (Decimal(1) - alpha)
            average_loss = loss * alpha + average_loss * (Decimal(1) - alpha)
    if average_loss == 0:
        return Decimal(100) if average_gain > 0 else Decimal(50)
    relative_strength = average_gain / average_loss
    return Decimal(100) - Decimal(100) / (Decimal(1) + relative_strength)


def _zscore(values: list[Decimal], lookback: int) -> Decimal | None:
    if len(values) < lookback:
        return None
    selected = [float(value) for value in values[-lookback:]]
    sigma = stdev(selected)
    if sigma == 0:
        return None
    return Decimal(str((selected[-1] - mean(selected)) / sigma))


def _rolling_average(values: list[Decimal], lookback: int) -> Decimal | None:
    if len(values) < lookback or lookback <= 0:
        return None
    return sum(values[-lookback:], Decimal(0)) / Decimal(lookback)


def _rolling_high(values: list[Decimal], lookback: int, *, shift: int = 0) -> Decimal | None:
    if lookback <= 0 or len(values) < lookback + shift:
        return None
    end = len(values) - shift
    start = end - lookback
    return max(values[start:end])


def _rolling_low(values: list[Decimal], lookback: int, *, shift: int = 0) -> Decimal | None:
    if lookback <= 0 or len(values) < lookback + shift:
        return None
    end = len(values) - shift
    start = end - lookback
    return min(values[start:end])


@dataclass(frozen=True)
class CompositeCandleStrategy:
    """Deterministic candle-close rules grouped by one Binance symbol owner."""

    rules: tuple[CandleRule, ...]
    name: str = "composite_candle_rules"
    requires_inference: bool = False

    def __init__(self, rules: list[dict], name: str = "composite_candle_rules"):
        parsed = []
        for raw in rules:
            parsed.append(
                CandleRule(
                    name=raw["name"],
                    family=raw["family"],
                    interval=raw["interval"],
                    side=Side(raw["side"]),
                    target_pct=Decimal(str(raw["target_pct"])),
                    stop_pct=Decimal(str(raw["stop_pct"])),
                    hold_candles=int(raw["hold_candles"]),
                    parameters=MappingProxyType(dict(raw.get("parameters", {}))),
                )
            )
        parsed.sort(key=lambda item: (int(item.parameters.get("priority", 999)), item.name))
        object.__setattr__(self, "rules", tuple(parsed))
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "requires_inference", False)
        object.__setattr__(
            self,
            "_bootstrap_pending",
            {rule.name for rule in parsed if rule.parameters.get("bootstrap_once")},
        )
        object.__setattr__(self, "_cadence_origin_slots", {})

    @property
    def required_intervals(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(rule.interval for rule in self.rules))

    @property
    def required_candles(self) -> int:
        return 512

    def evaluate(
        self,
        market: MarketContext,
        forecast: ForecastContext,
        position: PositionContext,
        account: AccountContext,
    ) -> SignalIntent:
        del forecast, account
        if position.is_open:
            return SignalIntent(market.symbol, market.last.close_time, None, "position_already_open")
        matches: list[CandleRule] = []
        for rule in self.rules:
            if not rule.enabled:
                continue
            candles = market.multi_timeframe.get(rule.interval, ())
            if self._matches(rule, candles):
                matches.append(rule)
        if matches:
            selected = matches[0]
            candles = market.multi_timeframe.get(selected.interval, ())
            last = candles[-1]
            skipped = ",".join(rule.name for rule in matches[1:])
            return SignalIntent(
                symbol=market.symbol,
                candle_close_time=last.close_time,
                side=selected.side,
                reason=selected.name,
                metadata={
                    "rule": selected.name,
                    "family": selected.family,
                    "interval": selected.interval,
                    "strategy_id": selected.strategy_id,
                    "priority": str(selected.priority),
                    "skipped_rules": skipped,
                },
                target_pct=selected.target_pct,
                stop_pct=selected.stop_pct,
                max_hold_minutes=selected.max_hold_minutes,
            )
        return SignalIntent(market.symbol, market.last.close_time, None, "no_rule_signal")

    def _matches(self, rule: CandleRule, candles: tuple) -> bool:
        if len(candles) < 205:
            return False
        closes = [candle.close for candle in candles]
        last = candles[-1]
        params = rule.parameters
        if rule.family == "range_fade":
            if len(candles) < 5 or last.open <= 0:
                return False
            candle_range = (last.high - last.low) / last.open
            ret4 = last.close / candles[-5].close - Decimal(1)
            minimum_range = Decimal(str(params["range"]))
            move = Decimal(str(params["move"]))
            if rule.side is Side.LONG:
                return candle_range >= minimum_range and ret4 <= -move
            return candle_range >= minimum_range and ret4 >= move
        if rule.family == "pullback_trend":
            rsi = _rsi(closes, int(params["rsi_period"]))
            if rsi is None:
                return False
            ema_fast = _ema(closes[-250:], int(params["fast"]))
            ema_slow = _ema(closes[-250:], int(params["slow"]))
            threshold = Decimal(str(params["rsi"]))
            if rule.side is Side.LONG:
                return ema_fast > ema_slow and rsi <= threshold
            return ema_fast < ema_slow and rsi >= Decimal(100) - threshold
        if rule.family == "rsi2_reversion":
            rsi = _rsi(closes, 2)
            if rsi is None:
                return False
            ema_slow = _ema(closes[-250:], int(params["slow"]))
            threshold = Decimal(str(params["rsi"]))
            if rule.side is Side.LONG:
                return rsi <= threshold and last.close > ema_slow
            return rsi >= Decimal(100) - threshold and last.close < ema_slow
        if rule.family == "bollinger_reversion":
            z = _zscore(closes, int(params["lookback"]))
            if z is None:
                return False
            ema_slow = _ema(closes[-250:], int(params["slow"]))
            threshold = Decimal(str(params["z"]))
            if rule.side is Side.LONG:
                return z <= -threshold and last.close > ema_slow
            return z >= threshold and last.close < ema_slow
        if rule.family == "ema_momentum":
            ema_fast = _ema(closes[-250:], int(params["fast"]))
            ema_slow = _ema(closes[-250:], int(params["slow"]))
            if len(closes) < 2:
                return False
            if rule.side is Side.LONG:
                directional_match = ema_fast > ema_slow and closes[-1] > closes[-2]
            else:
                directional_match = ema_fast < ema_slow and closes[-1] < closes[-2]
            if not directional_match:
                return False
            cadence_hours = int(params.get("cadence_hours", 0))
            interval_hours = INTERVAL_SECONDS[rule.interval] // 3600
            if interval_hours <= 0 or (cadence_hours > 0 and cadence_hours % interval_hours):
                raise ValueError(f"Invalid ema_momentum cadence for interval {rule.interval}")
            slot = int(last.open_time.timestamp() // 3600)
            if rule.name in self._bootstrap_pending:
                self._bootstrap_pending.remove(rule.name)
                self._cadence_origin_slots[rule.name] = slot
                return True
            if cadence_hours <= 0:
                return True
            cadence_slots = cadence_hours // interval_hours
            origin_slot = self._cadence_origin_slots.get(rule.name)
            if origin_slot is None:
                self._cadence_origin_slots[rule.name] = slot
                origin_slot = slot
            if slot <= origin_slot:
                return False
            return (slot - origin_slot) % cadence_slots == 0
        if rule.family == "orb":
            if len(candles) < max(205, int(params.get("breakout", 4)) + 21):
                return False
            bars = int(params.get("breakout", 4))
            prior_high = _rolling_high([candle.high for candle in candles], bars, shift=1)
            prior_low = _rolling_low([candle.low for candle in candles], bars, shift=1)
            average_volume = _rolling_average([candle.volume for candle in candles[:-1]], 20)
            if prior_high is None or prior_low is None or average_volume is None or average_volume <= 0:
                return False
            volume_ok = last.volume > average_volume
            if rule.side is Side.LONG:
                return last.close > prior_high and last.close > last.open and volume_ok
            return last.close < prior_low and last.close < last.open and volume_ok
        if rule.family == "breakout_expansion":
            lookback = int(params.get("lookback", 24))
            breakout = int(params.get("breakout", 20))
            if len(candles) < max(205, lookback * 2 + breakout + 2):
                return False
            highs = [candle.high for candle in candles]
            lows = [candle.low for candle in candles]
            volumes = [candle.volume for candle in candles]
            breakout_high = _rolling_high(highs, breakout, shift=1)
            breakout_low = _rolling_low(lows, breakout, shift=1)
            average_volume = _rolling_average(volumes[:-1], lookback)
            if breakout_high is None or breakout_low is None or average_volume is None or average_volume <= 0:
                return False
            current_range = _rolling_high(highs, lookback) - _rolling_low(lows, lookback)
            compression_samples: list[Decimal] = []
            for shift in range(1, lookback * 2 + 1):
                high_sample = _rolling_high(highs, lookback, shift=shift)
                low_sample = _rolling_low(lows, lookback, shift=shift)
                if high_sample is None or low_sample is None:
                    continue
                compression_samples.append(high_sample - low_sample)
            if not compression_samples:
                return False
            compression_threshold = sorted(compression_samples)[len(compression_samples) // 2]
            compressed = current_range <= compression_threshold
            volume_ok = last.volume >= average_volume
            if rule.side is Side.LONG:
                return compressed and volume_ok and last.close > breakout_high and last.close > last.open
            return compressed and volume_ok and last.close < breakout_low and last.close < last.open
        raise ValueError(f"Unsupported candle rule family: {rule.family}")
