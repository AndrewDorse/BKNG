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


def test_config_loads_pair_portfolio_bindings():
    settings = load_settings(Path("config/bot.yaml"))
    enabled = [binding for binding in settings.bindings if binding.enabled]

    assert len(enabled) == 12
    assert settings.portfolios == ()
    assert {binding.symbol for binding in enabled} == {
        "TSLAUSDT",
        "METAUSDT",
        "GOOGLUSDT",
        "PLTRUSDT",
        "AAPLUSDT",
        "ETHUSDT",
        "SOLUSDT",
        "ADAUSDT",
        "BTCUSDT",
        "XRPUSDT",
        "BNBUSDT",
        "DOGEUSDT",
    }
    assert all(binding.interval == "1h" for binding in enabled)
    assert all(binding.risk.leverage == 20 for binding in enabled)
    assert all(binding.risk.margin_fraction == 0.05 for binding in enabled)
    assert all(len(binding.parameters["rules"]) == 6 for binding in enabled)


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
