"""Test that load_config parses limits, plugin blocks, and modes from TOML."""

from strategy_platform.config import load_config

TOML = """
[execution]
mode = "mock"

[market_data]
mode = "live"

[limits]
max_orders_per_sec = 5
max_open_orders = 20
max_gross_notional = 100000

[[plugins]]
name = "mean_rev_btc"
module = "example_plugins.mean_reversion"
[plugins.config]
coin = "BTC"
k_stddev = 2.0
"""


def test_load_config(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(TOML)
    cfg = load_config(str(path))

    assert cfg.execution == "mock" and cfg.market_data == "live"
    assert cfg.limits.max_orders_per_sec == 5
    assert cfg.limits.max_gross_notional == 100000.0
    assert len(cfg.plugins) == 1
    p = cfg.plugins[0]
    assert p.name == "mean_rev_btc" and p.module == "example_plugins.mean_reversion"
    assert p.config == {"coin": "BTC", "k_stddev": 2.0}
