"""Pre-trade admission control for the global limits.

The limits are enforced at the single point every order passes through (before the executor), so the
guarantee "a plugin can't exceed a global limit" is actually expressible. RiskGuard decides
allow/reject for an order intent; it never places or persists. It reads order-state aggregates (open
count, gross notional) from the OrderStore — the store owns those numbers, the single source of truth
— and keeps its own orders/sec window, which is transient submission timing, not order state.

Not thread-safe on its own: the submission path serializes all calls under one lock, so the rate
window needs no internal locking.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Optional

from ..contract.orders import OrderIntent
from .order_store import OrderStore


@dataclass(frozen=True)
class Limits:
    max_orders_per_sec: int
    max_open_orders: int
    max_gross_notional: float


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: Optional[str] = None


class RiskGuard:
    def __init__(self, store: OrderStore, limits: Limits) -> None:
        self._store = store
        self._limits = limits
        self._recent: deque[float] = deque()  # monotonic timestamps of accepted submissions

    def check(self, intent: OrderIntent, now: Optional[float] = None) -> Verdict:
        """Decide whether this intent may be submitted, against the current world state."""
        now = time.monotonic() if now is None else now
        self._trim(now)

        if len(self._recent) >= self._limits.max_orders_per_sec:
            return Verdict(False, f"rate limit reached: {self._limits.max_orders_per_sec} orders/sec")

        if self._store.open_count() >= self._limits.max_open_orders:
            return Verdict(False, f"open-order limit reached: {self._limits.max_open_orders}")

        # Reduce-only orders shrink exposure, so they bypass the notional cap even when over it —
        # otherwise a strategy that wants to de-risk would be trapped by its own exposure.
        if not intent.reduce_only:
            prospective = self._store.gross_notional() + intent.sz * intent.limit_px
            if prospective > self._limits.max_gross_notional:
                return Verdict(False, f"gross-notional limit reached: {self._limits.max_gross_notional}")

        return Verdict(True)

    def note_submission(self, now: Optional[float] = None) -> None:
        """Record that one order was accepted for submission (advances the rate window)."""
        self._recent.append(time.monotonic() if now is None else now)

    def _trim(self, now: float) -> None:
        cutoff = now - 1.0
        while self._recent and self._recent[0] <= cutoff:
            self._recent.popleft()
