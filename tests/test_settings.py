from pathlib import Path

import pytest

from kronos_futures.bot.settings import load_settings, load_strategy


@pytest.fixture(autouse=True)
def live_environment(monkeypatch):
    monkeypatch.setenv("BINANCE_API_KEY", "key")
    monkeypatch.setenv("BINANCE_API_SECRET", "secret")
    monkeypatch.setenv(
        "LIVE_RISK_ACKNOWLEDGEMENT", "I_ACCEPT_EXTREME_FUTURES_RISK"
    )


def test_config_loads_single_guarded_cross_momentum_portfolio():
    settings = load_settings(Path("config/bot.yaml"))
    enabled = [binding for binding in settings.bindings if binding.enabled]

    assert enabled == []
    assert len(settings.portfolios) == 1
    portfolio = settings.portfolios[0]
    assert portfolio.enabled
    assert len(portfolio.symbols) == 15
    assert portfolio.lookback_bars == 30
    assert portfolio.leverage == 20
    assert portfolio.margin_fraction == 0.015
    assert portfolio.minimum_margin_usdt == 2.0
    assert portfolio.positions_per_side == 4
    assert portfolio.stop_pct == 0.03
    assert portfolio.max_portfolio_drawdown_pct == 0.15


def test_composite_bindings_do_not_require_inference():
    settings = load_settings(Path("config/bot.yaml"))

    for binding in settings.bindings:
        strategy = load_strategy(binding.strategy, binding.parameters)
        assert strategy.requires_inference is False


def test_invalid_trading_mode_has_actionable_error(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "lve")

    with pytest.raises(ValueError, match="TRADING_MODE=live exactly"):
        load_settings(Path("config/bot.yaml"))


def test_live_portfolio_loads_with_credentials_and_acknowledgement(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "live")
    monkeypatch.setenv("BINANCE_API_KEY", "key")
    monkeypatch.setenv("BINANCE_API_SECRET", "secret")
    monkeypatch.setenv("LIVE_RISK_ACKNOWLEDGEMENT", "I_ACCEPT_EXTREME_FUTURES_RISK")
    settings = load_settings(Path("config/bot.yaml"))
    assert settings.mode.value == "live"
