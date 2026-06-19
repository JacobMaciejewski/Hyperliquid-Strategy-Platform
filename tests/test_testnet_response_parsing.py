"""Tests for TestnetOrderManager's response normalization.

These exercise the parser against the exact response shapes from the Hyperliquid docs, so we
verify our translation of the venue's (untyped) replies without needing a funded account.
"""

from strategy_platform.contract.orders import OrderState
from strategy_platform.services.order_manager import LiveOrderManager

parse = LiveOrderManager._parse_order_response


def test_parse_resting():
    resp = {"status": "ok", "response": {"type": "order",
            "data": {"statuses": [{"resting": {"oid": 77738308}}]}}}
    result = parse("c1", resp)
    assert result.state is OrderState.RESTING
    assert result.oid == 77738308


def test_parse_filled():
    resp = {"status": "ok", "response": {"type": "order",
            "data": {"statuses": [{"filled": {"totalSz": "0.02", "avgPx": "1891.4", "oid": 77747314}}]}}}
    result = parse("c2", resp)
    assert result.state is OrderState.FILLED
    assert result.filled_sz == 0.02
    assert result.avg_px == 1891.4
    assert result.oid == 77747314


def test_parse_per_order_error_is_rejection():
    # A rejected order still comes back as top-level status "ok".
    resp = {"status": "ok", "response": {"type": "order",
            "data": {"statuses": [{"error": "Order must have minimum value of $10."}]}}}
    result = parse("c3", resp)
    assert result.state is OrderState.REJECTED
    assert "minimum value" in result.reason


def test_parse_request_level_error():
    resp = {"status": "err", "response": "Invalid nonce"}
    result = parse("c4", resp)
    assert result.state is OrderState.REJECTED
    assert "Invalid nonce" in result.reason
