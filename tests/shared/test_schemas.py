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
