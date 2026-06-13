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
    stop_pct: float = 0.01
    max_daily_loss_pct: float = 0.15
    max_drawdown_pct: float = 0.25
    consecutive_loss_limit: int = 3
    loss_pause_hours: int = 6
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
class BotSettings:
    mode: TradingMode
    database_url: str
    inference_url: str
    binance_api_key: str
    binance_api_secret: str
    live_acknowledgement: str
    required_live_acknowledgement: str
    allow_unvalidated_pairs: bool
    allow_research_full_margin_live: bool
    health_host: str
    health_port: int
    bindings: tuple[BindingSettings, ...]

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
    settings = BotSettings(
        mode=TradingMode(os.getenv("TRADING_MODE", raw.get("mode", "paper"))),
        database_url=os.getenv("DATABASE_URL", raw["database_url"]),
        inference_url=os.getenv("INFERENCE_URL", raw.get("inference_url", "http://inference:8081")),
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
        bindings=bindings,
    )
    validate_settings(settings)
    return settings


def validate_settings(settings: BotSettings) -> None:
    enabled = [binding for binding in settings.bindings if binding.enabled]
    owners = [binding.symbol for binding in enabled]
    duplicates = sorted({symbol for symbol in owners if owners.count(symbol) > 1})
    if duplicates:
        raise ValueError(f"Multiple enabled strategy owners for symbols: {duplicates}")
    if settings.mode is TradingMode.LIVE and not settings.is_live_authorized:
        raise ValueError("Live trading requires the exact LIVE_RISK_ACKNOWLEDGEMENT")
    if settings.mode is not TradingMode.PAPER and (
        not settings.binance_api_key or not settings.binance_api_secret
    ):
        raise ValueError("Testnet and live modes require Binance API credentials")
    for binding in enabled:
        if not binding.validated and not settings.allow_unvalidated_pairs:
            raise ValueError(f"{binding.symbol} is unvalidated; set ALLOW_UNVALIDATED_PAIRS=true")
        if (
            settings.mode is TradingMode.LIVE
            and binding.risk.profile == "research_full_margin"
            and not settings.allow_research_full_margin_live
        ):
            raise ValueError("research_full_margin is blocked in live mode")


def load_strategy(import_path: str, parameters: dict[str, Any]):
    module_name, class_name = import_path.rsplit(":", 1)
    cls = getattr(importlib.import_module(module_name), class_name)
    return cls(**parameters)
