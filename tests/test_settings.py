from pathlib import Path

from kronos_futures.bot.settings import load_settings, load_strategy


def test_high_wr_config_loads_five_unique_composite_bindings():
    settings = load_settings(Path("config/bot.yaml"))
    enabled = [binding for binding in settings.bindings if binding.enabled]

    assert len(enabled) == 5
    assert sorted(binding.symbol for binding in enabled) == [
        "BTCUSDT",
        "COINUSDT",
        "MSTRUSDT",
        "PLTRUSDT",
        "SOLUSDT",
    ]
    assert len({binding.symbol for binding in enabled}) == len(enabled)
    assert all(binding.risk.leverage == 10 for binding in enabled)
    assert all(binding.risk.margin_fraction == 0.05 for binding in enabled)
    assert all(binding.risk.profile == "high_wr_guarded" for binding in enabled)


def test_composite_bindings_do_not_require_inference():
    settings = load_settings(Path("config/bot.yaml"))

    for binding in settings.bindings:
        strategy = load_strategy(binding.strategy, binding.parameters)
        assert strategy.requires_inference is False
