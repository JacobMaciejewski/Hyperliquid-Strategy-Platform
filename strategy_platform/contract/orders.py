"""Platform-internal order types — the shared vocabulary every component speaks.

These shapes are normalized away from Hyperliquid's raw wire format so the rest of the
platform (store, plugins, dashboard) never depends on the SDK's response structure. The
`cloid` (client order id) is our correlation key: it ties an intent to its result, its
fills, and its persisted row, and is what restart-reconciliation matches against.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Literal, Optional

Side = Literal["buy", "sell"]          # order direction
Tif = Literal["Alo", "Ioc", "Gtc"]    # time-in-force: Alo=post-only, Ioc=fill-or-kill rest, Gtc=rest until cancelled


def new_cloid() -> str:
    """A fresh client order id (32 hex chars) — our correlation key across intent/result/store."""
    return uuid.uuid4().hex


class OrderState(str, Enum):
    PENDING = "pending"     # recorded before submit; not yet acknowledged by a venue
    RESTING = "resting"     # accepted, resting on the book
    FILLED = "filled"       # crossed and (fully) filled
    CANCELLED = "cancelled" # cancelled; no longer working
    REJECTED = "rejected"   # not accepted; see `reason`


@dataclass
class OrderIntent:
    """What a plugin asks the platform to place — the request, before it reaches a venue."""

    plugin_id: str               # which plugin asked (for attribution, per-plugin P&L, limits)
    coin: str                    # symbol, e.g. "BTC"
    side: Side                   # "buy" or "sell"
    sz: float                    # size in units of the coin
    limit_px: float              # limit price
    tif: Tif = "Gtc"             # time-in-force (see Tif above)
    reduce_only: bool = False    # if True, may only shrink a position, never grow/flip it
    cloid: Optional[str] = None  # correlation key; the platform assigns it before submitting


@dataclass
class OrderResult:
    """Normalized outcome of a place/cancel — the same shape from the mock and live managers."""

    cloid: str                   # echoes the intent's cloid, so callers can match result to request
    state: OrderState            # lifecycle state (managers emit resting/filled/rejected; see OrderState)
    oid: Optional[int] = None    # venue order id; present once accepted (resting or filled), else None
    filled_sz: float = 0.0       # size filled so far (0 while purely resting)
    avg_px: Optional[float] = None  # average fill price; None until something fills
    reason: Optional[str] = None    # why rejected (only set when state is REJECTED)
