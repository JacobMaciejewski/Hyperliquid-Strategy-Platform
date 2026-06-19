"""Tests for the positions/P&L projection and the JSON status endpoint."""

import json
import urllib.request
from types import SimpleNamespace

from strategy_platform.services.observability import StatusServer, positions_and_pnl


def fill(plugin, coin, side, sz, px):
    return SimpleNamespace(plugin_id=plugin, coin=coin, side=side, filled_sz=sz, avg_px=px, limit_px=px)


def test_pnl_long_position_marked_up():
    fills = [fill("p", "BTC", "buy", 1.0, 100.0)]      # long 1 @ 100, cash -100
    out = positions_and_pnl(fills, lambda c: 110.0)    # mark 110 -> pnl = -100 + 1*110 = 10
    assert out["p"]["BTC"] == {"position": 1.0, "pnl": 10.0}


def test_pnl_round_trip_is_realized():
    fills = [fill("p", "BTC", "buy", 1.0, 100.0), fill("p", "BTC", "sell", 1.0, 105.0)]
    out = positions_and_pnl(fills, lambda c: 999.0)    # flat -> mark irrelevant; pnl = -100 + 105 = 5
    assert out["p"]["BTC"]["position"] == 0.0
    assert out["p"]["BTC"]["pnl"] == 5.0


def test_pnl_none_when_mark_unknown():
    fills = [fill("p", "ETH", "buy", 1.0, 100.0)]
    out = positions_and_pnl(fills, lambda c: None)     # open position, no mark
    assert out["p"]["ETH"]["pnl"] is None


def test_groups_by_plugin_and_coin():
    fills = [fill("a", "BTC", "buy", 1.0, 100.0), fill("b", "ETH", "sell", 2.0, 50.0)]
    out = positions_and_pnl(fills, lambda c: 0.0)
    assert set(out) == {"a", "b"}
    assert out["b"]["ETH"]["position"] == -2.0


def test_status_server_serves_json():
    server = StatusServer(lambda: {"ok": True, "open_orders": 3}, port=0)  # port 0 -> OS picks
    server.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{server.port}/status", timeout=2) as resp:
            assert resp.status == 200
            body = json.loads(resp.read())
        assert body == {"ok": True, "open_orders": 3}
    finally:
        server.stop()
