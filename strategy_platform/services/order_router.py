"""The submission path — the one component that wires RiskGuard, OrderManager, and OrderStore
together for an order. Centralizing the sequencing here keeps each service unaware of the others.

It serializes check + reserve under one lock so two plugins (each on its own thread) can't both slip
past a limit at once. The flow is strictly sequential: guard checks, store reserves (record pending),
manager places, store records the outcome. The store is touched before *and* after the manager —
never in parallel with it.
"""

from __future__ import annotations

import threading

from ..contract.orders import OrderIntent, OrderResult, OrderState, new_cloid
from .order_manager import OrderManager
from .order_store import OrderStore
from .risk_guard import RiskGuard


class OrderRouter:
    def __init__(self, risk_guard: RiskGuard, store: OrderStore, manager: OrderManager) -> None:
        self._risk = risk_guard
        self._store = store
        self._manager = manager
        self._lock = threading.Lock()

    def submit(self, intent: OrderIntent) -> OrderResult:
        if intent.cloid is None:
            intent.cloid = new_cloid()

        with self._lock:
            # Atomic admission + reservation: nothing can interleave between the limit check and the
            # reservation, so concurrent submits can't jointly breach a limit.
            verdict = self._risk.check(intent)
            if not verdict.ok:
                return OrderResult(intent.cloid, OrderState.REJECTED, reason=verdict.reason)
            self._risk.note_submission()
            self._store.record_pending(intent)
            # place() stays inside the lock for now: it serializes venue calls (dodging the live SDK's
            # nonce race), and the capped order rate makes the blocking cost negligible.
            result = self._manager.place(intent)

        self._store.apply(result)  # the reserved order is already counted; safe to record outside
        return result

    def cancel(self, cloid: str) -> bool:
        """Cancel a working order. Cancellation only frees exposure, so it needs no limit check."""
        if self._manager.cancel(cloid):
            self._store.apply(OrderResult(cloid, OrderState.CANCELLED))
            return True
        return False
