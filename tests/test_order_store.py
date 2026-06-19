"""Tests for OrderStore: lifecycle upsert, state-precedence guard, and restart durability."""

from strategy_platform.contract.orders import OrderIntent, OrderResult, OrderState
from strategy_platform.services.order_store import OrderStore


def intent(cloid="c1", coin="BTC", side="buy", sz=1.0, limit_px=50.0):
    return OrderIntent(plugin_id="p1", coin=coin, side=side, sz=sz, limit_px=limit_px, cloid=cloid)


def store(tmp_path):
    return OrderStore(str(tmp_path / "platform.db"))


def test_record_pending_then_get(tmp_path):
    s = store(tmp_path)
    s.record_pending(intent())
    row = s.get("c1")
    assert row.state is OrderState.PENDING
    assert row.plugin_id == "p1" and row.coin == "BTC"
    assert row.created_at == row.updated_at


def test_pending_to_resting_to_filled(tmp_path):
    s = store(tmp_path)
    s.record_pending(intent())
    s.apply(OrderResult("c1", OrderState.RESTING, oid=999))
    assert s.get("c1").state is OrderState.RESTING
    assert s.get("c1").oid == 999

    s.apply(OrderResult("c1", OrderState.FILLED, oid=999, filled_sz=1.0, avg_px=50.0))
    row = s.get("c1")
    assert row.state is OrderState.FILLED
    assert row.filled_sz == 1.0 and row.avg_px == 50.0


def test_state_does_not_regress(tmp_path):
    # A late/out-of-order RESTING must not overwrite a FILLED order.
    s = store(tmp_path)
    s.record_pending(intent())
    s.apply(OrderResult("c1", OrderState.FILLED, oid=1, filled_sz=1.0, avg_px=50.0))
    s.apply(OrderResult("c1", OrderState.RESTING, oid=1))  # stale
    assert s.get("c1").state is OrderState.FILLED


def test_terminal_is_final(tmp_path):
    # Cancel arriving after a fill (race) must not flip a FILLED order to CANCELLED.
    s = store(tmp_path)
    s.record_pending(intent())
    s.apply(OrderResult("c1", OrderState.FILLED, oid=1, filled_sz=1.0, avg_px=50.0))
    s.apply(OrderResult("c1", OrderState.CANCELLED))
    assert s.get("c1").state is OrderState.FILLED
    assert s.get("c1").filled_sz == 1.0  # not wiped by the cancel update


def test_duplicate_apply_is_idempotent(tmp_path):
    s = store(tmp_path)
    s.record_pending(intent())
    s.apply(OrderResult("c1", OrderState.RESTING, oid=1))
    s.apply(OrderResult("c1", OrderState.RESTING, oid=1))  # duplicate delivery
    assert s.get("c1").state is OrderState.RESTING


def test_apply_unknown_order_is_noop(tmp_path):
    s = store(tmp_path)
    s.apply(OrderResult("ghost", OrderState.FILLED, oid=1, filled_sz=1.0))
    assert s.get("ghost") is None


def test_open_orders_and_gross_notional(tmp_path):
    s = store(tmp_path)
    s.record_pending(intent(cloid="c1", sz=1.0, limit_px=50.0))   # open, 50 notional
    s.record_pending(intent(cloid="c2", sz=2.0, limit_px=100.0))  # open, 200 notional
    s.record_pending(intent(cloid="c3", sz=1.0, limit_px=50.0))
    s.apply(OrderResult("c3", OrderState.FILLED, oid=1, filled_sz=1.0, avg_px=50.0))  # no longer open

    open_cloids = {r.cloid for r in s.open_orders()}
    assert open_cloids == {"c1", "c2"}
    assert s.gross_notional() == 50.0 + 200.0


def test_durability_across_restart(tmp_path):
    # The whole point: a new OrderStore over the same file recovers prior orders.
    db = str(tmp_path / "platform.db")
    s1 = OrderStore(db)
    s1.record_pending(intent(cloid="c1"))
    s1.apply(OrderResult("c1", OrderState.RESTING, oid=42))
    s1.close()

    s2 = OrderStore(db)  # simulate platform restart
    row = s2.get("c1")
    assert row.state is OrderState.RESTING
    assert row.oid == 42
    assert {r.cloid for r in s2.open_orders()} == {"c1"}


# --- in-memory working set ---

def test_terminal_order_evicted_from_memory_but_kept_on_disk(tmp_path):
    s = store(tmp_path)
    s.record_pending(intent(cloid="c1"))
    s.apply(OrderResult("c1", OrderState.FILLED, oid=1, filled_sz=1.0, avg_px=50.0))
    assert s.open_count() == 0                  # gone from the working set
    assert s.open_orders() == []
    assert s.get("c1").state is OrderState.FILLED  # still retrievable from disk


def test_gross_notional_tracks_evictions(tmp_path):
    s = store(tmp_path)
    s.record_pending(intent(cloid="c1", sz=1.0, limit_px=50.0))   # +50
    s.record_pending(intent(cloid="c2", sz=2.0, limit_px=100.0))  # +200
    assert s.gross_notional() == 250.0
    s.apply(OrderResult("c1", OrderState.FILLED, oid=1, filled_sz=1.0, avg_px=50.0))  # -50
    assert s.gross_notional() == 200.0
    assert s.open_count() == 1


def test_duplicate_record_pending_does_not_double_count(tmp_path):
    s = store(tmp_path)
    s.record_pending(intent(cloid="c1", sz=1.0, limit_px=50.0))
    s.record_pending(intent(cloid="c1", sz=1.0, limit_px=50.0))  # same cloid again
    assert s.open_count() == 1
    assert s.gross_notional() == 50.0


def test_get_returns_a_copy(tmp_path):
    s = store(tmp_path)
    s.record_pending(intent(cloid="c1"))
    snapshot = s.get("c1")
    snapshot.state = OrderState.FILLED  # mutate the caller's copy
    assert s.get("c1").state is OrderState.PENDING  # working set untouched


def test_gross_notional_rebuilt_after_restart(tmp_path):
    db = str(tmp_path / "platform.db")
    s1 = OrderStore(db)
    s1.record_pending(intent(cloid="c1", sz=1.0, limit_px=50.0))
    s1.record_pending(intent(cloid="c2", sz=2.0, limit_px=100.0))
    s1.close()

    s2 = OrderStore(db)  # restart: aggregate must be rebuilt from disk
    assert s2.gross_notional() == 250.0
    assert s2.open_count() == 2
