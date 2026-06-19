"""Tests for MockOrderManager: classification, validation, and concurrent-sender integrity."""

import threading

from strategy_platform.contract.orders import OrderIntent, OrderState
from strategy_platform.services.order_manager import MockOrderManager


class FakePrices:
    """A fixed price source for deterministic tests."""

    def __init__(self, prices: dict[str, float]) -> None:
        self._prices = prices

    def mid(self, coin: str):
        return self._prices.get(coin)


def intent(side="buy", sz=1.0, limit_px=50.0, coin="BTC", cloid=None):
    return OrderIntent(plugin_id="p", coin=coin, side=side, sz=sz, limit_px=limit_px, cloid=cloid)


def test_buy_below_mid_rests():
    mgr = MockOrderManager(FakePrices({"BTC": 100.0}))
    result = mgr.place(intent(side="buy", limit_px=50.0))
    assert result.state is OrderState.RESTING
    assert result.oid is not None


def test_buy_at_or_above_mid_fills():
    mgr = MockOrderManager(FakePrices({"BTC": 100.0}))
    result = mgr.place(intent(side="buy", limit_px=110.0))
    assert result.state is OrderState.FILLED
    assert result.filled_sz == 1.0
    assert result.avg_px == 100.0


def test_sell_above_mid_rests():
    mgr = MockOrderManager(FakePrices({"BTC": 100.0}))
    result = mgr.place(intent(side="sell", limit_px=110.0))
    assert result.state is OrderState.RESTING


def test_sell_at_or_below_mid_fills():
    mgr = MockOrderManager(FakePrices({"BTC": 100.0}))
    result = mgr.place(intent(side="sell", limit_px=90.0))
    assert result.state is OrderState.FILLED


def test_below_min_notional_rejected():
    mgr = MockOrderManager(FakePrices({"BTC": 100.0}))
    result = mgr.place(intent(sz=0.01, limit_px=100.0))  # $1 notional
    assert result.state is OrderState.REJECTED
    assert "minimum value" in result.reason


def test_unknown_coin_rejected():
    mgr = MockOrderManager(FakePrices({"BTC": 100.0}))
    result = mgr.place(intent(coin="DOGE"))
    assert result.state is OrderState.REJECTED


def test_cancel_resting_then_not():
    mgr = MockOrderManager(FakePrices({"BTC": 100.0}))
    result = mgr.place(intent(side="buy", limit_px=50.0, cloid="abc"))
    assert result.state is OrderState.RESTING
    assert mgr.cancel("abc") is True   # first cancel works
    assert mgr.cancel("abc") is False  # already gone


def test_poll_fills_emits_async_fill_when_market_crosses():
    mgr = MockOrderManager(FakePrices({"BTC": 100.0}))
    events = []
    mgr.set_event_handler(events.append)

    result = mgr.place(intent(side="buy", limit_px=50.0, cloid="x"))  # rests (50 < 100)
    assert result.state is OrderState.RESTING

    mgr.poll_fills("BTC", 100.0)   # mid unchanged -> no fill
    assert events == []
    mgr.poll_fills("BTC", 40.0)    # mid falls to 40 <= 50 -> fills
    assert len(events) == 1
    assert events[0].state is OrderState.FILLED and events[0].cloid == "x"
    assert mgr.cancel("x") is False  # no longer resting


def test_concurrent_senders_get_distinct_oids():
    """Stand-in for several plugins submitting at once: no oid collisions, none lost."""
    mgr = MockOrderManager(FakePrices({"BTC": 100.0}))
    senders, per_sender = 3, 50
    start = threading.Barrier(senders)
    results: list = []
    results_lock = threading.Lock()

    def run(sender_id: int):
        start.wait()  # release all threads together to maximize contention
        local = [mgr.place(intent(side="buy", limit_px=50.0, cloid=f"{sender_id}-{i}"))
                 for i in range(per_sender)]
        with results_lock:
            results.extend(local)

    threads = [threading.Thread(target=run, args=(s,)) for s in range(senders)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == senders * per_sender
    oids = [r.oid for r in results]
    assert all(oid is not None for oid in oids)
    assert len(set(oids)) == len(oids)  # every oid unique
