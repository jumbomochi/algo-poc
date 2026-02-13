import pytest
from services.signal_generation.event import (
    EarningsSurpriseSignal,
    NewsSentimentSignal,
    InsiderActivitySignal,
)
from services.signal_generation.base import SignalResult


# ── EarningsSurpriseSignal ───────────────────────────────────────────


class TestEarningsSurpriseSignal:
    def test_earnings_beat_returns_positive(self):
        sig = EarningsSurpriseSignal()
        result = sig.compute({"actual_eps": 2.50, "estimate_eps": 2.00})
        assert result.value > 0
        assert 0.0 <= result.confidence <= 1.0

    def test_earnings_miss_returns_negative(self):
        sig = EarningsSurpriseSignal()
        result = sig.compute({"actual_eps": 1.50, "estimate_eps": 2.00})
        assert result.value < 0

    def test_earnings_inline_returns_near_zero(self):
        sig = EarningsSurpriseSignal()
        result = sig.compute({"actual_eps": 2.00, "estimate_eps": 2.00})
        assert abs(result.value) < 0.1

    def test_name(self):
        assert EarningsSurpriseSignal().name == "earnings_surprise"


# ── NewsSentimentSignal ──────────────────────────────────────────────


class TestNewsSentimentSignal:
    def test_positive_sentiment(self):
        sig = NewsSentimentSignal()
        result = sig.compute({"sentiment_score": 0.8})
        assert result.value > 0
        assert 0.0 <= result.confidence <= 1.0

    def test_negative_sentiment(self):
        sig = NewsSentimentSignal()
        result = sig.compute({"sentiment_score": -0.6})
        assert result.value < 0

    def test_neutral_sentiment(self):
        sig = NewsSentimentSignal()
        result = sig.compute({"sentiment_score": 0.0})
        assert abs(result.value) < 0.1

    def test_extreme_sentiment_is_clamped(self):
        sig = NewsSentimentSignal()
        result = sig.compute({"sentiment_score": 1.5})
        assert result.value <= 1.0

    def test_name(self):
        assert NewsSentimentSignal().name == "news_sentiment"


# ── InsiderActivitySignal ────────────────────────────────────────────


class TestInsiderActivitySignal:
    def test_net_buying_returns_positive(self):
        sig = InsiderActivitySignal()
        result = sig.compute(
            {"insider_buy_value": 500_000, "insider_sell_value": 100_000}
        )
        assert result.value > 0
        assert 0.0 <= result.confidence <= 1.0

    def test_net_selling_returns_negative(self):
        sig = InsiderActivitySignal()
        result = sig.compute(
            {"insider_buy_value": 50_000, "insider_sell_value": 400_000}
        )
        assert result.value < 0

    def test_no_activity_returns_zero(self):
        sig = InsiderActivitySignal()
        result = sig.compute(
            {"insider_buy_value": 0, "insider_sell_value": 0}
        )
        assert abs(result.value) < 0.01
        assert result.confidence == 0.0

    def test_name(self):
        assert InsiderActivitySignal().name == "insider_activity"
