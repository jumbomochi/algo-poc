from __future__ import annotations

from typing import Any

from services.signal_generation.base import Signal, SignalResult


class ValuationSignal(Signal):
    """Assesses whether a stock is under- or over-valued relative to sector.

    Reads pe_ratio, pb_ratio, sector_median_pe, sector_median_pb from data.
    Positive value when undervalued (ratios below sector median).
    """

    name = "valuation"

    def compute(self, data: dict[str, Any]) -> SignalResult:
        pe = data["pe_ratio"]
        pb = data["pb_ratio"]
        sector_pe = data["sector_median_pe"]
        sector_pb = data["sector_median_pb"]

        # Compute discount/premium as percentage deviation from sector median.
        # Negative deviation = undervalued (good) -> positive signal.
        pe_score = (sector_pe - pe) / sector_pe if sector_pe != 0 else 0.0
        pb_score = (sector_pb - pb) / sector_pb if sector_pb != 0 else 0.0

        # Weighted average: PE gets 60%, PB gets 40%
        raw = pe_score * 0.6 + pb_score * 0.4

        # Confidence based on how far from fair value (stronger signal = more confident)
        confidence = min(abs(raw) * 2.0, 1.0)

        return SignalResult(value=raw, confidence=confidence)


class QualitySignal(Signal):
    """Assesses the quality of a company's fundamentals.

    Reads roe, debt_equity, margin from data.
    Positive when high quality (high ROE, low debt, high margin).
    """

    name = "quality"

    # Thresholds for "good" quality
    _ROE_TARGET = 0.15
    _DEBT_EQUITY_TARGET = 1.0  # lower is better
    _MARGIN_TARGET = 0.10

    def compute(self, data: dict[str, Any]) -> SignalResult:
        roe = data["roe"]
        debt_equity = data["debt_equity"]
        margin = data["margin"]

        # ROE score: positive if above target, scaled
        roe_score = (roe - self._ROE_TARGET) / self._ROE_TARGET

        # Debt/equity score: positive if below target (less debt = good), inverted
        de_score = (self._DEBT_EQUITY_TARGET - debt_equity) / self._DEBT_EQUITY_TARGET

        # Margin score: positive if above target
        margin_score = (margin - self._MARGIN_TARGET) / self._MARGIN_TARGET

        # Equal-weighted average
        raw = (roe_score + de_score + margin_score) / 3.0

        confidence = min(abs(raw) * 1.5, 1.0)

        return SignalResult(value=raw, confidence=confidence)


class GrowthSignal(Signal):
    """Assesses growth trajectory based on revenue and earnings growth.

    Reads revenue_growth, earnings_growth from data.
    Positive when growing.
    """

    name = "growth"

    # A 20% growth rate maps to roughly signal value 1.0
    _GROWTH_SCALE = 0.20

    def compute(self, data: dict[str, Any]) -> SignalResult:
        rev_growth = data["revenue_growth"]
        earn_growth = data["earnings_growth"]

        # Weighted average: earnings growth gets slightly more weight
        raw = rev_growth * 0.4 + earn_growth * 0.6

        # Scale so that 20% combined growth -> ~1.0 signal
        value = raw / self._GROWTH_SCALE

        confidence = min(abs(value) * 1.5, 1.0)

        return SignalResult(value=value, confidence=confidence)
