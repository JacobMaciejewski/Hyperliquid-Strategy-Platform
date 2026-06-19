"""Unit tests for RiskGuard's limit logic and orders/sec rate window."""

from strategy_platform.contract.orders import OrderIntent, OrderResult, OrderState
from strategy_platform.services.order_store import OrderStore
from strategy_platform.services.risk_guard import Limits, RiskGuard


def intent(cloid="c", sz=1.0, limit_px=50.0, reduce_only=False):
    return OrderIntent(plugin_id="p", coin="BTC", side="buy", sz=sz, limit_px=limit_px,
                       reduce_only=reduce_only, cloid=cloid)


def guard(tmp_path, **overrides):
    limits = Limits(max_orders_per_sec=overrides.get("rate", 100),
                    max_open_orders=overrides.get("open", 100),
                    max_gross_notional=overrides.get("notional", 1e9))
    store = OrderStore(str(tmp_path / "platform.db"))
    return RiskGuard(store, limits), store


def test_allows_within_limits(tmp_path):
    g, _ = guard(tmp_path)
    assert g.check(intent()).ok


def test_rejects_when_open_orders_at_cap(tmp_path):
    g, store = guard(tmp_path, open=2)
    store.record_pending(intent(cloid="c1"))
    store.record_pending(intent(cloid="c2"))  # now 2 open == cap
    v = g.check(intent(cloid="c3"))
    assert not v.ok and "open-order limit" in v.reason


def test_rejects_when_notional_would_exceed(tmp_path):
    g, store = guard(tmp_path, notional=100.0)
    store.record_pending(intent(cloid="c1", sz=1.0, limit_px=80.0))  # 80 open notional
    v = g.check(intent(cloid="c2", sz=1.0, limit_px=50.0))            # +50 -> 130 > 100
    assert not v.ok and "gross-notional limit" in v.reason


def test_reduce_only_bypasses_notional_cap(tmp_path):
    g, store = guard(tmp_path, notional=100.0)
    store.record_pending(intent(cloid="c1", sz=1.0, limit_px=120.0))  # already over cap
    assert g.check(intent(cloid="c2", sz=1.0, limit_px=50.0, reduce_only=True)).ok


def test_rate_window_rejects_then_recovers(tmp_path):
    g, _ = guard(tmp_path, rate=2)
    # Drive the window with an explicit clock so the test is deterministic.
    assert g.check(intent(), now=1000.0).ok
    g.note_submission(now=1000.0)
    assert g.check(intent(), now=1000.1).ok
    g.note_submission(now=1000.1)
    assert not g.check(intent(), now=1000.2).ok      # 2 in the last second -> reject
    assert g.check(intent(), now=1001.2).ok          # window slid past both -> allowed again
