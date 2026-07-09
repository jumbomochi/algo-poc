from datetime import datetime, timezone
from shared.schemas.messages import (
    MarketDataMessage, FundamentalMessage, EventMessage,
    SignalMessage, RecommendationMessage, ApprovedOrderMessage,
    FillMessage, AlertMessage, KillMessage,
)

def test_market_data_message():
    msg = MarketDataMessage(
        ticker="AAPL", timestamp=datetime.now(timezone.utc),
        open=150.0, high=155.0, low=149.0, close=153.0, volume=1000000,
    )
    assert msg.ticker == "AAPL"
    data = msg.to_stream_dict()
    assert isinstance(data["timestamp"], str)
    assert data["ticker"] == "AAPL"

def test_signal_message_normalization():
    msg = SignalMessage(
        ticker="AAPL", timestamp=datetime.now(timezone.utc),
        signal_name="support_proximity", signal_value=0.85,
        confidence=0.9, computed_at=datetime.now(timezone.utc),
    )
    assert -1.0 <= msg.signal_value <= 1.0

def test_recommendation_message_has_top_features():
    msg = RecommendationMessage(
        ticker="AAPL", timestamp=datetime.now(timezone.utc),
        action="buy", confidence=0.82,
        top_features={"support_proximity": 0.4, "valuation": 0.3},
        recommendation_id="rec-001",
    )
    assert "support_proximity" in msg.top_features

def test_alert_message_has_priority():
    msg = AlertMessage(
        timestamp=datetime.now(timezone.utc),
        event_type="soft_ceiling_breach", priority="medium",
        message="AAPL drifted above 7% of NAV",
        context={"ticker": "AAPL", "pct_of_nav": 7.5},
    )
    assert msg.priority in ("low", "medium", "high", "critical")

def test_kill_message():
    msg = KillMessage(
        timestamp=datetime.now(timezone.utc),
        triggered_by="operator", reason="Manual kill switch",
    )
    assert msg.triggered_by == "operator"


class TestNoneFieldRoundtrip:
    """None fields must survive the stream roundtrip via omission.

    Regression: str(None) wired the literal string "None", making every
    market sell order (limit_price=None) unparseable by the execution
    service — stop-loss and kill-liquidation orders silently never ran.
    """

    def test_market_sell_order_roundtrips(self):
        from datetime import datetime, timezone

        from shared.schemas.messages import ApprovedOrderMessage

        order = ApprovedOrderMessage(
            ticker="AAPL",
            timestamp=datetime.now(timezone.utc),
            action="sell",
            quantity=5,
            order_type="market",
            limit_price=None,
            recommendation_id="r1",
            risk_adjustments={},
        )
        wire = order.to_stream_dict()
        assert "limit_price" not in wire  # omitted, not "None"
        restored = ApprovedOrderMessage.from_stream_dict(wire)
        assert restored.limit_price is None
        assert restored.quantity == 5

    def test_recommendation_bridge_fields_roundtrip(self):
        from datetime import datetime, timezone

        from shared.schemas.messages import RecommendationMessage

        rec = RecommendationMessage(
            ticker="GLD",
            timestamp=datetime.now(timezone.utc),
            action="buy",
            confidence=1.0,
            top_features={},
            recommendation_id="sleeve-2026-07-10-tail_risk_hedge-GLD-buy",
            limit_price=382.13,
            quantity=4.1969,
            portfolio="tail_risk_hedge",
        )
        restored = RecommendationMessage.from_stream_dict(rec.to_stream_dict())
        assert restored.limit_price == 382.13
        assert restored.quantity == 4.1969
        assert restored.portfolio == "tail_risk_hedge"

    def test_ml_path_recommendation_defaults_none(self):
        from datetime import datetime, timezone

        from shared.schemas.messages import RecommendationMessage

        rec = RecommendationMessage(
            ticker="AAPL",
            timestamp=datetime.now(timezone.utc),
            action="buy",
            confidence=0.7,
            top_features={"growth": 0.4},
            recommendation_id="ml-1",
        )
        restored = RecommendationMessage.from_stream_dict(rec.to_stream_dict())
        assert restored.limit_price is None
        assert restored.quantity is None
        assert restored.portfolio is None
