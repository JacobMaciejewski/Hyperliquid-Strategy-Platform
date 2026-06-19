"""Prod-like integration tests that hit the live Hyperliquid testnet.

Excluded from the default run (network, slow, non-deterministic). Run explicitly with:
    uv run pytest -m integration

They verify the real things mocks/fakes can't: that we actually receive testnet data, that we
recover from a real socket drop, and that LiveOrderManager normalizes a real venue rejection rather
than crashing (using a random unfunded wallet, so no funds are needed and nothing is left resting).
"""

import time

import pytest

pytestmark = pytest.mark.integration

RECEIVE_TIMEOUT = 15.0
RECOVER_TIMEOUT = 25.0


def _wait_until(predicate, timeout):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.2)
    return predicate()


def test_market_data_live_receives():
    from strategy_platform.services.market_data import LiveMarketFeed, MarketDataService

    svc = MarketDataService(LiveMarketFeed())
    got = []
    token = svc.subscribe("BTC", "bbo", got.append)
    try:
        assert _wait_until(lambda: len(got) > 0, RECEIVE_TIMEOUT), "no testnet data received"
        assert svc.stats()["connected"] is True
    finally:
        svc.unsubscribe(token)


def test_market_data_live_recovers_from_real_drop():
    from strategy_platform.services.market_data import LiveMarketFeed, MarketDataService

    feed = LiveMarketFeed()
    svc = MarketDataService(feed)
    got = []
    token = svc.subscribe("BTC", "bbo", got.append)
    try:
        assert _wait_until(lambda: len(got) > 0, RECEIVE_TIMEOUT), "no data before drop"
        before = len(got)

        feed._info.ws_manager.ws.close()  # force a real socket drop (not via our disconnect())

        assert _wait_until(lambda: len(got) > before, RECOVER_TIMEOUT), \
            "no data after forced drop — reconnect/resubscribe did not recover"
        assert svc.stats()["connected"] is True
    finally:
        svc.unsubscribe(token)


def test_live_order_manager_normalizes_real_rejection():
    from eth_account import Account
    from hyperliquid.exchange import Exchange
    from hyperliquid.utils import constants

    from strategy_platform.contract.orders import OrderIntent, OrderState
    from strategy_platform.services.order_manager import LiveOrderManager

    wallet = Account.create()  # random, unfunded — the venue will reject
    mgr = LiveOrderManager(Exchange(wallet, constants.TESTNET_API_URL))
    result = mgr.place(OrderIntent(plugin_id="t", coin="BTC", side="buy", sz=0.01, limit_px=2000.0))

    assert result.state is OrderState.REJECTED  # a real failure, normalized — not a crash
    assert result.reason
