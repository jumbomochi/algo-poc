from __future__ import annotations

from datetime import datetime, timezone

import pytest

from shared.schemas.messages import SignalMessage
from services.ml_model.feature_assembly import FeatureAssembler

SIGNAL_NAMES = [
    "support_proximity",
    "support_strength",
    "support_trend",
    "valuation",
    "quality",
    "growth",
    "earnings_surprise",
    "news_sentiment",
    "insider_activity",
]


def _make_signal(
    ticker: str,
    name: str,
    value: float = 0.5,
    confidence: float = 0.9,
) -> SignalMessage:
    now = datetime.now(timezone.utc)
    return SignalMessage(
        ticker=ticker,
        timestamp=now,
        signal_name=name,
        signal_value=value,
        confidence=confidence,
        computed_at=now,
    )


def _make_complete_signals(ticker: str = "AAPL") -> list[SignalMessage]:
    return [_make_signal(ticker, name, value=0.1 * i) for i, name in enumerate(SIGNAL_NAMES)]


class TestFeatureAssembler:
    def test_assemble_complete_signals(self):
        assembler = FeatureAssembler()
        signals = _make_complete_signals("AAPL")
        result = assembler.assemble("AAPL", signals)

        assert result is not None
        assert len(result) == 9
        for name in SIGNAL_NAMES:
            assert name in result

    def test_assemble_returns_correct_values(self):
        assembler = FeatureAssembler()
        signals = [
            _make_signal("AAPL", "support_proximity", value=0.8),
            _make_signal("AAPL", "support_strength", value=-0.3),
            _make_signal("AAPL", "support_trend", value=0.5),
            _make_signal("AAPL", "valuation", value=0.2),
            _make_signal("AAPL", "quality", value=0.9),
            _make_signal("AAPL", "growth", value=-0.1),
            _make_signal("AAPL", "earnings_surprise", value=0.4),
            _make_signal("AAPL", "news_sentiment", value=0.6),
            _make_signal("AAPL", "insider_activity", value=-0.7),
        ]
        result = assembler.assemble("AAPL", signals)

        assert result is not None
        assert result["support_proximity"] == pytest.approx(0.8)
        assert result["support_strength"] == pytest.approx(-0.3)
        assert result["insider_activity"] == pytest.approx(-0.7)

    def test_assemble_incomplete_returns_none(self):
        assembler = FeatureAssembler()
        # Only 5 signals out of 9
        signals = [_make_signal("AAPL", name) for name in SIGNAL_NAMES[:5]]
        result = assembler.assemble("AAPL", signals)

        assert result is None

    def test_assemble_empty_signals_returns_none(self):
        assembler = FeatureAssembler()
        result = assembler.assemble("AAPL", [])

        assert result is None

    def test_assemble_filters_by_ticker(self):
        assembler = FeatureAssembler()
        # 8 signals for AAPL + 1 for GOOG (wrong ticker)
        signals = [_make_signal("AAPL", name) for name in SIGNAL_NAMES[:8]]
        signals.append(_make_signal("GOOG", SIGNAL_NAMES[8]))
        result = assembler.assemble("AAPL", signals)

        assert result is None

    def test_staleness_flags_low_confidence(self):
        assembler = FeatureAssembler()
        signals = []
        for i, name in enumerate(SIGNAL_NAMES):
            # Make first signal low confidence (stale)
            conf = 0.1 if i == 0 else 0.9
            signals.append(_make_signal("AAPL", name, confidence=conf))

        result = assembler.assemble("AAPL", signals)
        assert result is not None

        stale = assembler.get_staleness_flags("AAPL", signals)
        assert "support_proximity" in stale
        assert stale["support_proximity"] is True

    def test_staleness_flags_all_fresh(self):
        assembler = FeatureAssembler()
        signals = _make_complete_signals("AAPL")
        stale = assembler.get_staleness_flags("AAPL", signals)

        # All high confidence, none should be flagged
        assert all(v is False for v in stale.values())

    def test_duplicate_signals_uses_latest(self):
        assembler = FeatureAssembler()
        signals = _make_complete_signals("AAPL")
        # Add a duplicate with different value
        duplicate = _make_signal("AAPL", "support_proximity", value=0.99)
        signals.append(duplicate)

        result = assembler.assemble("AAPL", signals)
        assert result is not None
        # Should use the last signal (latest)
        assert result["support_proximity"] == pytest.approx(0.99)
