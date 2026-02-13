import pytest
from services.signal_generation.fundamental import (
    ValuationSignal,
    QualitySignal,
    GrowthSignal,
)
from services.signal_generation.base import SignalResult


# ── ValuationSignal ──────────────────────────────────────────────────


class TestValuationSignal:
    def test_undervalued_returns_positive(self):
        sig = ValuationSignal()
        result = sig.compute(
            {
                "pe_ratio": 12.0,
                "pb_ratio": 1.0,
                "sector_median_pe": 20.0,
                "sector_median_pb": 2.5,
            }
        )
        assert result.value > 0
        assert 0.0 <= result.confidence <= 1.0

    def test_overvalued_returns_negative(self):
        sig = ValuationSignal()
        result = sig.compute(
            {
                "pe_ratio": 30.0,
                "pb_ratio": 4.0,
                "sector_median_pe": 20.0,
                "sector_median_pb": 2.5,
            }
        )
        assert result.value < 0

    def test_fairly_valued_returns_near_zero(self):
        sig = ValuationSignal()
        result = sig.compute(
            {
                "pe_ratio": 20.0,
                "pb_ratio": 2.5,
                "sector_median_pe": 20.0,
                "sector_median_pb": 2.5,
            }
        )
        assert abs(result.value) < 0.15

    def test_name(self):
        assert ValuationSignal().name == "valuation"


# ── QualitySignal ────────────────────────────────────────────────────


class TestQualitySignal:
    def test_high_quality_returns_positive(self):
        sig = QualitySignal()
        result = sig.compute(
            {
                "roe": 0.25,
                "debt_equity": 0.3,
                "margin": 0.20,
            }
        )
        assert result.value > 0
        assert 0.0 <= result.confidence <= 1.0

    def test_low_quality_returns_negative(self):
        sig = QualitySignal()
        result = sig.compute(
            {
                "roe": 0.02,
                "debt_equity": 3.0,
                "margin": 0.01,
            }
        )
        assert result.value < 0

    def test_mixed_quality(self):
        sig = QualitySignal()
        result = sig.compute(
            {
                "roe": 0.15,
                "debt_equity": 1.0,
                "margin": 0.10,
            }
        )
        assert -1.0 <= result.value <= 1.0

    def test_name(self):
        assert QualitySignal().name == "quality"


# ── GrowthSignal ─────────────────────────────────────────────────────


class TestGrowthSignal:
    def test_strong_growth_returns_positive(self):
        sig = GrowthSignal()
        result = sig.compute(
            {
                "revenue_growth": 0.25,
                "earnings_growth": 0.30,
            }
        )
        assert result.value > 0
        assert 0.0 <= result.confidence <= 1.0

    def test_negative_growth_returns_negative(self):
        sig = GrowthSignal()
        result = sig.compute(
            {
                "revenue_growth": -0.10,
                "earnings_growth": -0.15,
            }
        )
        assert result.value < 0

    def test_flat_growth_returns_near_zero(self):
        sig = GrowthSignal()
        result = sig.compute(
            {
                "revenue_growth": 0.0,
                "earnings_growth": 0.0,
            }
        )
        assert abs(result.value) < 0.15

    def test_name(self):
        assert GrowthSignal().name == "growth"
