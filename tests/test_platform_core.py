"""Platform core tests: lifecycle, market-data dispatch -> orders, plugin isolation, clean shutdown.

Offline — uses the FakeFeed and the mock executor. Plugins load by module path like real ones.
"""

import threading
import time

from strategy_platform.config import PlatformConfig, PluginSpec
from strategy_platform.core import Platform
from strategy_platform.contract.orders import OrderState
from strategy_platform.services.order_store import OrderStore
from strategy_platform.services.risk_guard import Limits

from tests._fixture_plugins import FakeFeed

BBO = {"data": {"coin": "BTC", "bbo": [{"px": "100.0", "sz": "1", "n": 1},
                                       {"px": "100.0", "sz": "1", "n": 1}]}}


def make_platform(tmp_path, plugins, feed):
    config = PlatformConfig(
        limits=Limits(max_orders_per_sec=1000, max_open_orders=100, max_gross_notional=1e12),
        plugins=plugins, execution="mock", market_data="live",
    )
    return Platform(config, feed=feed, store=OrderStore(str(tmp_path / "platform.db")))


def pump(feed, until, times=40, delay=0.05):
    """Push ticks until `until()` holds (covers the startup race) or we give up."""
    for _ in range(times):
        if until():
            return True
        feed.push("BTC", "bbo", BBO)
        time.sleep(delay)
    return until()


def test_market_data_dispatch_places_order(tmp_path):
    feed = FakeFeed()
    plat = make_platform(tmp_path, [PluginSpec("orderer", "tests._fixture_plugins", {"coin": "BTC"},
                                               cls="OrdererPlugin")], feed)
    plat.start()
    try:
        assert pump(feed, lambda: plat.store.open_count() >= 1), "plugin never placed its order"
        assert plat.store.open_count() == 1
    finally:
        plat.stop()


def test_plugin_exception_is_isolated(tmp_path):
    feed = FakeFeed()
    plat = make_platform(tmp_path, [
        PluginSpec("raiser", "tests._fixture_plugins", {"coin": "BTC"}, cls="RaiserPlugin"),
        PluginSpec("orderer", "tests._fixture_plugins", {"coin": "BTC"}, cls="OrdererPlugin"),
    ], feed)
    plat.start()
    try:
        # The orderer still works despite the raiser throwing on every tick.
        assert pump(feed, lambda: plat.store.open_count() >= 1), "healthy plugin was affected"
        assert any(e["plugin"] == "raiser" and e["level"] == "error" for e in plat.errors)
    finally:
        plat.stop()


def test_stuck_plugin_does_not_block_peers_or_shutdown(tmp_path):
    feed = FakeFeed()
    block = threading.Event()
    plat = make_platform(tmp_path, [
        PluginSpec("blocker", "tests._fixture_plugins", {"coin": "BTC", "block_event": block},
                   cls="BlockerPlugin"),
        PluginSpec("orderer", "tests._fixture_plugins", {"coin": "BTC"}, cls="OrdererPlugin"),
    ], feed)
    plat.start()
    try:
        # Peer keeps working while the blocker is stuck in on_market_data.
        assert pump(feed, lambda: plat.store.open_count() >= 1), "stuck plugin starved its peer"
        # Shutdown returns promptly despite the stuck plugin (it gets quarantined, not joined).
        t0 = time.monotonic()
        plat.stop(join_timeout=0.5)
        assert time.monotonic() - t0 < 3.0
    finally:
        block.set()  # release the stuck thread for cleanup


def test_resting_order_fills_async_when_market_crosses(tmp_path):
    feed = FakeFeed()
    plat = make_platform(tmp_path, [PluginSpec("orderer", "tests._fixture_plugins", {"coin": "BTC"},
                                               cls="OrdererPlugin")], feed)
    plat.start()
    try:
        # orderer rests a buy at bid*0.5 = 50 (mid 100)
        assert pump(feed, lambda: plat.store.open_count() >= 1)
        cloid = plat.store.open_orders()[0].cloid

        # market falls to 40 (<= 50): mock fills the resting order asynchronously
        feed.push("BTC", "bbo", {"data": {"coin": "BTC", "bbo": [{"px": "40.0"}, {"px": "40.0"}]}})

        assert plat.store.get(cloid).state is OrderState.FILLED
        assert plat.store.open_count() == 0
    finally:
        plat.stop()


def test_open_orders_survive_platform_restart(tmp_path):
    plugins = [PluginSpec("orderer", "tests._fixture_plugins", {"coin": "BTC"}, cls="OrdererPlugin")]

    # run 1: place a resting order, then shut down (orders are NOT cancelled on shutdown — they survive)
    feed = FakeFeed()
    plat1 = make_platform(tmp_path, plugins, feed)
    plat1.start()
    assert pump(feed, lambda: plat1.store.open_count() >= 1)
    plat1.stop()

    # run 2: restart over the same db file -> the working set is rebuilt from disk, order not lost
    plat2 = make_platform(tmp_path, plugins, FakeFeed())
    try:
        assert plat2.store.open_count() == 1
    finally:
        plat2.stop()


def test_status_reports_state(tmp_path):
    feed = FakeFeed()
    plat = make_platform(tmp_path, [PluginSpec("orderer", "tests._fixture_plugins", {"coin": "BTC"},
                                               cls="OrdererPlugin")], feed)
    plat.start()
    try:
        pump(feed, lambda: plat.store.open_count() >= 1)
        status = plat.status()
        assert status["execution"] == "mock"
        assert status["open_orders"] == 1
        assert status["plugins"][0]["name"] == "orderer"
    finally:
        plat.stop()
