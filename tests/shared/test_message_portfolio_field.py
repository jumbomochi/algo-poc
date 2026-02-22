from __future__ import annotations

from datetime import datetime, timezone

from shared.schemas.messages import ApprovedOrderMessage, FillMessage


def test_approved_order_has_portfolio_field():
    """ApprovedOrderMessage should accept an optional portfolio field."""
    order = ApprovedOrderMessage(
        ticker="AAPL",
        timestamp=datetime.now(timezone.utc),
        action="buy",
        quantity=10,
        order_type="limit",
        limit_price=150.0,
        recommendation_id="test-123",
        portfolio="momentum",
    )
    assert order.portfolio == "momentum"

    # Stream round-trip preserves portfolio
    d = order.to_stream_dict()
    restored = ApprovedOrderMessage.from_stream_dict(d)
    assert restored.portfolio == "momentum"


def test_approved_order_portfolio_defaults_to_none():
    """Portfolio field should be optional and default to None."""
    order = ApprovedOrderMessage(
        ticker="AAPL",
        timestamp=datetime.now(timezone.utc),
        action="buy",
        quantity=10,
        order_type="limit",
        limit_price=150.0,
        recommendation_id="test-123",
    )
    assert order.portfolio is None


def test_fill_message_has_portfolio_field():
    """FillMessage should accept an optional portfolio field."""
    fill = FillMessage(
        ticker="AAPL",
        timestamp=datetime.now(timezone.utc),
        side="buy",
        quantity=10,
        fill_price=150.0,
        commission=0.05,
        recommendation_id="test-123",
        order_id="order-456",
        portfolio="mean_reversion",
    )
    assert fill.portfolio == "mean_reversion"

    # Stream round-trip preserves portfolio
    d = fill.to_stream_dict()
    restored = FillMessage.from_stream_dict(d)
    assert restored.portfolio == "mean_reversion"
