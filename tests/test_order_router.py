"""Integration tests for the submission path: guard -> store -> manager -> store, including that a
global limit holds under concurrent contention (the brief's core requirement)."""

import threading

from strategy_platform.contract.orders import OrderIntent, OrderState
from strategy_platform.services.order_manager import MockOrderManager
from strategy_platform.services.order_router import OrderRouter
from strategy_platform.services.order_store import OrderStore
from strategy_platform.services.risk_guard import Limits, RiskGuard


class FakePrices:
    def __init__(self, prices):
        self._prices = prices

    def mid(self, coin):
        return self._prices.get(coin)


def build(tmp_path, rate=1000, open_=100, notional=1e12):
    store = OrderStore(str(tmp_path / "platform.db"))
    guard = RiskGuard(store, Limits(rate, open_, notional))
    manager = MockOrderManager(FakePrices({"BTC": 100.0}))
    return OrderRouter(guard, store, manager), store


def intent(cloid=None, side="buy", sz=1.0, limit_px=50.0):
    # buy at 50 with mid 100 -> rests (stays open), so it counts against limits
    return OrderIntent(plugin_id="p", coin="BTC", side=side, sz=sz, limit_px=limit_px, cloid=cloid)


def test_happy_path_rests_and_persists(tmp_path):
    router, store = build(tmp_path)
    result = router.submit(intent())
    assert result.state is OrderState.RESTING
    assert store.get(result.cloid).state is OrderState.RESTING


def test_rejection_is_clear_and_not_persisted(tmp_path):
    router, store = build(tmp_path, open_=1)
    router.submit(intent(cloid="c1"))
    result = router.submit(intent(cloid="c2"))  # 1 already open == cap
    assert result.state is OrderState.REJECTED
    assert "open-order limit" in result.reason
    assert store.get("c2") is None              # a limit-rejected order is never recorded
    assert store.open_count() == 1


def test_venue_rejection_after_passing_risk(tmp_path):
    # Risk allows it (generous limits) but the venue rejects it (below the mock's $10 min notional).
    # The reserved slot must be released and the order recorded as REJECTED (unlike a risk rejection,
    # which is never persisted).
    router, store = build(tmp_path)
    result = router.submit(intent(sz=0.01, limit_px=100.0))  # $1 notional
    assert result.state is OrderState.REJECTED
    assert "minimum value" in result.reason
    assert store.open_count() == 0                              # reservation released
    assert store.get(result.cloid).state is OrderState.REJECTED  # but recorded


def test_cancel_frees_a_slot(tmp_path):
    router, store = build(tmp_path, open_=1)
    r1 = router.submit(intent())
    assert router.submit(intent()).state is OrderState.REJECTED  # at cap
    assert router.cancel(r1.cloid) is True
    assert router.submit(intent()).state is OrderState.RESTING   # slot freed


def test_global_limit_holds_under_concurrency(tmp_path):
    """20 plugins submit at once; max_open_orders=5 must hold exactly — no over-admission."""
    router, store = build(tmp_path, rate=10_000, open_=5)
    senders = 20
    start = threading.Barrier(senders)
    results = []
    lock = threading.Lock()

    def run(i):
        start.wait()
        r = router.submit(intent(cloid=f"c{i}"))
        with lock:
            results.append(r)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(senders)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    accepted = [r for r in results if r.state is OrderState.RESTING]
    rejected = [r for r in results if r.state is OrderState.REJECTED]
    assert len(accepted) == 5            # exactly the cap, never more
    assert len(rejected) == 15
    assert store.open_count() == 5
