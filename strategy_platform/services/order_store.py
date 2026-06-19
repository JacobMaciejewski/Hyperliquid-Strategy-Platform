"""Durable order store with an in-memory working set — the single source of truth for every
platform-placed order.

Everything reads order state from here — one source of truth — and because the platform must survive a
crash/restart, that source is persistent. Two layers, kept consistent under one lock:
  * SQLite on disk is the system of record: one row per order, keyed by `cloid`, upserted as the
    order's state advances. It's an embedded stdlib DB — no server, one file — so it adds nothing to
    clone-to-run. Survives restart.
  * An in-memory dict holds only the *open* orders (pending/resting) plus a running gross-notional
    total. Hot-path reads (RiskGuard's limit checks) hit memory — O(1) — never the DB. Terminal
    orders (filled/cancelled/rejected) are evicted from memory and live on only in SQLite, so memory
    is bounded by what's currently open, not by history.

Invariant: the in-memory set equals the DB's set of open orders. Writes are write-through (memory +
DB together, under the lock); on startup the memory set is rebuilt from the DB. So an `apply` for a
cloid that isn't in memory is, by the invariant, either unknown or already terminal — and ignored.

Safety rules:
  * State only ever *advances* (`pending < resting < {filled, cancelled, rejected}`); terminal is
    final and `filled_sz` is monotonic, so duplicate / out-of-order updates can't regress an order.
    Correctness independent of the clock.
  * `record_pending` writes the row *before* submit, so a crash mid-submit leaves a PENDING anchor.
  * One lock guards all state: fills arrive on the SDK's websocket thread while submits run on the
    order path. The store never references an OrderManager and vice-versa — keeping persistence and
    execution decoupled means neither has to know about the other.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Optional

from ..contract.orders import OrderIntent, OrderResult, OrderState

# Lifecycle ranking: an order may only move to a higher rank. Terminal states share the top rank,
# so nothing overwrites them and equal-rank (duplicate) updates are no-ops.
_RANK = {
    OrderState.PENDING: 0,
    OrderState.RESTING: 1,
    OrderState.FILLED: 2,
    OrderState.CANCELLED: 2,
    OrderState.REJECTED: 2,
}

_OPEN_STATES = (OrderState.PENDING.value, OrderState.RESTING.value)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    cloid       TEXT PRIMARY KEY,
    plugin_id   TEXT NOT NULL,
    coin        TEXT NOT NULL,
    side        TEXT NOT NULL,
    sz          REAL NOT NULL,
    limit_px    REAL NOT NULL,
    tif         TEXT NOT NULL,
    reduce_only INTEGER NOT NULL,
    state       TEXT NOT NULL,
    oid         INTEGER,
    filled_sz   REAL NOT NULL DEFAULT 0,
    avg_px      REAL,
    reason      TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_state  ON orders(state);
CREATE INDEX IF NOT EXISTS idx_orders_plugin ON orders(plugin_id);
"""


@dataclass
class OrderRow:
    """A persisted order: its originating intent plus its latest known outcome and timestamps."""

    cloid: str
    plugin_id: str
    coin: str
    side: str
    sz: float
    limit_px: float
    tif: str
    reduce_only: bool
    state: OrderState
    oid: Optional[int]
    filled_sz: float
    avg_px: Optional[float]
    reason: Optional[str]
    created_at: str
    updated_at: str


class OrderStore:
    def __init__(self, path: str = "platform.db") -> None:
        self._lock = threading.Lock()
        # check_same_thread=False: the websocket thread and order path both touch the store;
        # the lock (not the connection's thread affinity) provides the safety.
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL: readers (e.g. a litecli session or curl-driven dashboard) don't block the platform's
        # writes and vice-versa, so you can inspect the DB live while it runs.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

        self._open: dict[str, OrderRow] = {}   # working set: open orders only
        self._gross_notional = 0.0             # running total over the working set
        self._rebuild_open()                   # restart recovery: repopulate from disk

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def record_pending(self, intent: OrderIntent) -> None:
        """Anchor a new order as PENDING before submitting it. cloid must already be assigned."""
        if intent.cloid is None:
            raise ValueError("intent.cloid must be set before persisting (the platform assigns it)")
        now = self._now()
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO orders "
                "(cloid, plugin_id, coin, side, sz, limit_px, tif, reduce_only, state, "
                " filled_sz, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (intent.cloid, intent.plugin_id, intent.coin, intent.side, intent.sz,
                 intent.limit_px, intent.tif, int(intent.reduce_only), OrderState.PENDING.value,
                 0.0, now, now),
            )
            self._conn.commit()
            if cur.rowcount == 1:  # genuinely new (not a duplicate cloid) -> track in memory
                self._open[intent.cloid] = OrderRow(
                    cloid=intent.cloid, plugin_id=intent.plugin_id, coin=intent.coin,
                    side=intent.side, sz=intent.sz, limit_px=intent.limit_px, tif=intent.tif,
                    reduce_only=intent.reduce_only, state=OrderState.PENDING, oid=None,
                    filled_sz=0.0, avg_px=None, reason=None, created_at=now, updated_at=now,
                )
                self._gross_notional += intent.sz * intent.limit_px

    def apply(self, result: OrderResult) -> None:
        """Advance an existing open order from a place result or async fill/cancel event.

        No-op if the order isn't open (unknown or already terminal, by the invariant) or if the
        update wouldn't advance the state — so it's idempotent and order-insensitive.
        """
        with self._lock:
            current = self._open.get(result.cloid)
            if current is None:
                return  # unknown or already terminal — nothing to advance
            if _RANK[result.state] <= _RANK[current.state]:
                return  # regression or duplicate

            now = self._now()
            self._conn.execute(
                "UPDATE orders SET state = ?, oid = COALESCE(?, oid), "
                "filled_sz = MAX(filled_sz, ?), avg_px = COALESCE(?, avg_px), "
                "reason = COALESCE(?, reason), updated_at = ? WHERE cloid = ?",
                (result.state.value, result.oid, result.filled_sz, result.avg_px,
                 result.reason, now, result.cloid),
            )
            self._conn.commit()

            if _RANK[result.state] == 2:  # terminal -> evict from the working set
                self._gross_notional -= current.sz * current.limit_px
                del self._open[result.cloid]
            else:  # still open (resting) -> update the in-memory row in place
                current.state = result.state
                if result.oid is not None:
                    current.oid = result.oid
                current.filled_sz = max(current.filled_sz, result.filled_sz)
                if result.avg_px is not None:
                    current.avg_px = result.avg_px
                current.updated_at = now

    def get(self, cloid: str) -> Optional[OrderRow]:
        """Latest state of an order. Open orders come from memory; terminal ones from disk."""
        with self._lock:
            row = self._open.get(cloid)
            if row is not None:
                return replace(row)  # copy so callers can't mutate the working set
            db = self._conn.execute("SELECT * FROM orders WHERE cloid = ?", (cloid,)).fetchone()
            return self._to_row(db) if db else None

    def open_orders(self) -> list[OrderRow]:
        """All currently-working orders (pending/resting), served from memory."""
        with self._lock:
            return [replace(r) for r in self._open.values()]

    def open_count(self) -> int:
        with self._lock:
            return len(self._open)

    def gross_notional(self) -> float:
        """Notional of open orders (sz * limit_px), O(1). Open-order interpretation; positions TBD."""
        with self._lock:
            return self._gross_notional

    def filled_orders(self) -> list[OrderRow]:
        """All filled orders (from disk) — the input the observability layer projects into positions/P&L."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM orders WHERE state = ?", (OrderState.FILLED.value,)
            ).fetchall()
        return [self._to_row(r) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _rebuild_open(self) -> None:
        """Repopulate the working set from disk (startup / restart recovery)."""
        rows = self._conn.execute(
            f"SELECT * FROM orders WHERE state IN ({','.join('?' * len(_OPEN_STATES))})",
            _OPEN_STATES,
        ).fetchall()
        for r in rows:
            row = self._to_row(r)
            self._open[row.cloid] = row
            self._gross_notional += row.sz * row.limit_px

    @staticmethod
    def _to_row(row: sqlite3.Row) -> OrderRow:
        return OrderRow(
            cloid=row["cloid"], plugin_id=row["plugin_id"], coin=row["coin"], side=row["side"],
            sz=row["sz"], limit_px=row["limit_px"], tif=row["tif"],
            reduce_only=bool(row["reduce_only"]), state=OrderState(row["state"]), oid=row["oid"],
            filled_sz=row["filled_sz"], avg_px=row["avg_px"], reason=row["reason"],
            created_at=row["created_at"], updated_at=row["updated_at"],
        )
