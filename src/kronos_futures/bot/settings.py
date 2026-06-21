from __future__ import annotations

import importlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .domain import TradingMode


@dataclass(frozen=True)
class RiskSettings:
    leverage: int = 45
    margin_fraction: float = 0.10
    minimum_margin_usdt: float = 2.0
    risk_fraction: float | None = None
    fixed_margin_usdt: float | None = None
    stop_pct: float = 0.01
    target_pct: float = 0.01
    max_drawdown_pct: float = 0.25
    maximum_signal_age_seconds: int = 10
    maximum_price_drift_pct: float = 0.001
    maximum_spread_pct: float = 0.0002
    profile: str = "guarded"


@dataclass(frozen=True)
class BindingSettings:
    name: str
    strategy: str
    symbol: str
    interval: str
    parameters: dict[str, Any] = field(default_factory=dict)
    risk: RiskSettings = field(default_factory=RiskSettings)
    enabled: bool = True
    validated: bool = False


@dataclass(frozen=True)
class PortfolioSettings:
    name: str
    symbols: tuple[str, ...]
    interval: str = "4h"
    lookback_bars: int = 30
    rebalance_bars: int = 18
    positions_per_side: int = 4
    leverage: int = 20
    margin_fraction: float = 0.015
    minimum_margin_usdt: float = 2.0
    stop_pct: float = 0.03
    max_portfolio_drawdown_pct: float = 0.15
    maximum_spread_pct: float = 0.001
    maximum_price_drift_pct: float = 0.003
    strategy_id: str = "cross_momentum_v1_utc_1pct"
    enabled: bool = True


@dataclass(frozen=True)
class BotSettings:
    mode: TradingMode
    inference_url: str
    inference_timeout_seconds: float
    binance_api_key: str
    binance_api_secret: str
    live_acknowledgement: str
    required_live_acknowledgement: str
    allow_unvalidated_pairs: bool
    allow_research_full_margin_live: bool
    health_host: str
    health_port: int
    poll_seconds: int
    bindings: tuple[BindingSettings, ...]
    portfolios: tuple[PortfolioSettings, ...] = ()
    state_path: str = "/state/portfolio_state.json"

    @property
    def is_live_authorized(self) -> bool:
        return (
            self.mode is not TradingMode.LIVE
            or (
                bool(self.required_live_acknowledgement)
                and self.live_acknowledgement == self.required_live_acknowledgement
            )
        )


def _risk(raw: dict[str, Any]) -> RiskSettings:
    return RiskSettings(**raw)


def _trading_mode(value: str) -> TradingMode:
    normalized = str(value).strip().lower()
    try:
        return TradingMode(normalized)
    except ValueError as exc:
        valid = ", ".join(mode.value for mode in TradingMode)
        raise ValueError(
            f"Invalid TRADING_MODE '{value}'. Use one of: {valid}. "
            "For real Binance trading set TRADING_MODE=live exactly."
        ) from exc


def load_settings(path: str | Path) -> BotSettings:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    bindings = tuple(
        BindingSettings(
            name=item["name"],
            strategy=item["strategy"],
            symbol=item["symbol"].upper(),
            interval=item.get("interval", "1m"),
            parameters=item.get("parameters", {}),
            risk=_risk(item.get("risk", {})),
            enabled=item.get("enabled", True),
            validated=item.get("validated", False),
        )
        for item in raw.get("bindings", [])
    )
    portfolios = tuple(
        PortfolioSettings(
            name=item["name"],
            symbols=tuple(symbol.upper() for symbol in item["symbols"]),
            interval=item.get("interval", "4h"),
            lookback_bars=int(item.get("lookback_bars", 30)),
            rebalance_bars=int(item.get("rebalance_bars", 18)),
            positions_per_side=int(item.get("positions_per_side", 4)),
            leverage=int(item.get("leverage", 20)),
            margin_fraction=float(item.get("margin_fraction", 0.015)),
            minimum_margin_usdt=float(item.get("minimum_margin_usdt", 2.0)),
            stop_pct=float(item.get("stop_pct", 0.03)),
            max_portfolio_drawdown_pct=float(
                item.get("max_portfolio_drawdown_pct", 0.15)
            ),
            maximum_spread_pct=float(item.get("maximum_spread_pct", 0.001)),
            maximum_price_drift_pct=float(
                item.get("maximum_price_drift_pct", 0.003)
            ),
            strategy_id=item.get("strategy_id", "cross_momentum_v1_utc_1pct"),
            enabled=item.get("enabled", True),
        )
        for item in raw.get("portfolios", [])
    )
    settings = BotSettings(
        mode=_trading_mode(os.getenv("TRADING_MODE", raw.get("mode", "paper"))),
        inference_url=os.getenv("INFERENCE_URL", raw.get("inference_url", "http://inference:8081")),
        inference_timeout_seconds=float(os.getenv("INFERENCE_TIMEOUT_SECONDS", "60")),
        binance_api_key=os.getenv("BINANCE_API_KEY", ""),
        binance_api_secret=os.getenv("BINANCE_API_SECRET", ""),
        live_acknowledgement=os.getenv("LIVE_RISK_ACKNOWLEDGEMENT", ""),
        required_live_acknowledgement=os.getenv(
            "REQUIRED_LIVE_RISK_ACKNOWLEDGEMENT", "I_ACCEPT_EXTREME_FUTURES_RISK"
        ),
        allow_unvalidated_pairs=os.getenv("ALLOW_UNVALIDATED_PAIRS", "false").lower() == "true",
        allow_research_full_margin_live=(
            os.getenv("ALLOW_RESEARCH_FULL_MARGIN_LIVE", "false").lower() == "true"
        ),
        health_host=raw.get("health", {}).get("host", "0.0.0.0"),
        health_port=int(raw.get("health", {}).get("port", 8080)),
        poll_seconds=int(os.getenv("POLL_SECONDS", raw.get("poll_seconds", 30))),
        bindings=bindings,
        portfolios=portfolios,
        state_path=os.getenv(
            "TRADER_STATE_PATH", raw.get("state_path", "/state/portfolio_state.json")
        ),
    )
    validate_settings(settings)
    return settings


def validate_settings(settings: BotSettings) -> None:
    enabled = [binding for binding in settings.bindings if binding.enabled]
    owners = [binding.symbol for binding in enabled]
    duplicates = sorted({symbol for symbol in owners if owners.count(symbol) > 1})
    if duplicates:
        raise ValueError(f"Multiple enabled strategy owners for symbols: {duplicates}")
    enabled_portfolios = [item for item in settings.portfolios if item.enabled]
    if len(enabled_portfolios) > 1:
        raise ValueError("Only one enabled cross-sectional portfolio is supported")
    portfolio_symbols = [
        symbol for portfolio in enabled_portfolios for symbol in portfolio.symbols
    ]
    duplicate_portfolio_symbols = sorted(
        {symbol for symbol in portfolio_symbols if portfolio_symbols.count(symbol) > 1}
    )
    overlap = sorted(set(owners).intersection(portfolio_symbols))
    if duplicate_portfolio_symbols or overlap:
        raise ValueError(
            "Symbols must have one owner across bindings and portfolios: "
            f"{sorted(set(duplicate_portfolio_symbols + overlap))}"
        )
    if settings.mode is TradingMode.LIVE and not settings.is_live_authorized:
        raise ValueError("Live trading requires the exact LIVE_RISK_ACKNOWLEDGEMENT")
    if settings.mode is not TradingMode.PAPER and (
        not settings.binance_api_key or not settings.binance_api_secret
    ):
        raise ValueError("Testnet and live modes require Binance API credentials")
    if settings.poll_seconds < 5:
        raise ValueError("poll_seconds must be at least 5")
    if settings.inference_timeout_seconds < 10:
        raise ValueError("INFERENCE_TIMEOUT_SECONDS must be at least 10")
    for binding in enabled:
        if not 1 <= binding.risk.leverage <= 50:
            raise ValueError("Live bot leverage must be between 1x and 50x")
        if not 0 < binding.risk.margin_fraction <= 1:
            raise ValueError("margin_fraction must be greater than 0 and at most 1")
        if binding.risk.risk_fraction is not None and not 0 < binding.risk.risk_fraction <= 0.05:
            raise ValueError("risk_fraction must be greater than 0 and at most 0.05")
        if (
            binding.risk.minimum_margin_usdt <= 0
        ):
            raise ValueError("minimum_margin_usdt must be positive")
        if (
            binding.risk.fixed_margin_usdt is not None
            and binding.risk.fixed_margin_usdt <= 0
        ):
            raise ValueError("fixed_margin_usdt must be positive when configured")
        if not binding.validated and not settings.allow_unvalidated_pairs:
            raise ValueError(f"{binding.symbol} is unvalidated; set ALLOW_UNVALIDATED_PAIRS=true")
        if (
            settings.mode is TradingMode.LIVE
            and binding.risk.profile == "research_full_margin"
            and not settings.allow_research_full_margin_live
        ):
            raise ValueError("research_full_margin is blocked in live mode")
    for portfolio in enabled_portfolios:
        if not 10 <= len(portfolio.symbols) <= 25:
            raise ValueError("Portfolio must contain 10-25 unique symbols")
        if portfolio.interval != "4h":
            raise ValueError("Cross-sectional portfolio currently requires 4h candles")
        if not 1 <= portfolio.leverage <= 20:
            raise ValueError("Portfolio leverage must be between 1x and 20x")
        if not 0 < portfolio.margin_fraction <= 0.02:
            raise ValueError("Portfolio margin_fraction must be at most 2% per position")
        if portfolio.positions_per_side * 2 * portfolio.margin_fraction > 0.12:
            raise ValueError("Portfolio maximum concurrent margin must be at most 12%")
        if portfolio.minimum_margin_usdt <= 0:
            raise ValueError("Portfolio minimum_margin_usdt must be positive")
        if not 0 < portfolio.stop_pct <= 0.06:
            raise ValueError("Portfolio stop_pct must be at most 6%")
        if not 0 < portfolio.max_portfolio_drawdown_pct <= 0.15:
            raise ValueError("Portfolio drawdown kill switch must be at most 15%")


def load_strategy(import_path: str, parameters: dict[str, Any]):
    module_name, class_name = import_path.rsplit(":", 1)
    cls = getattr(importlib.import_module(module_name), class_name)
    return cls(**parameters)
