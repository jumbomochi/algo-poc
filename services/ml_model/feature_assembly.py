from __future__ import annotations

from shared.schemas.messages import SignalMessage

REQUIRED_SIGNALS = frozenset([
    "support_proximity",
    "support_strength",
    "support_trend",
    "valuation",
    "quality",
    "growth",
    "earnings_surprise",
    "news_sentiment",
    "insider_activity",
])

# Signals with confidence below this threshold are considered stale.
STALENESS_CONFIDENCE_THRESHOLD = 0.3


class FeatureAssembler:
    """Collects signals per ticker into feature vectors for ML model input.

    A complete feature vector requires all 9 signal names to be present.
    Tracks staleness flags for low-confidence signals.
    """

    def assemble(
        self,
        ticker: str,
        signals: list[SignalMessage],
    ) -> dict[str, float] | None:
        """Assemble a feature vector from a list of signals for a ticker.

        Args:
            ticker: The stock ticker to assemble features for.
            signals: List of SignalMessage instances (may include other tickers).

        Returns:
            Dict mapping signal_name -> signal_value if all 9 signals are
            present for the ticker, or None if incomplete.
        """
        relevant = [s for s in signals if s.ticker == ticker]

        # Build feature dict, last signal wins for duplicates
        features: dict[str, float] = {}
        for signal in relevant:
            if signal.signal_name in REQUIRED_SIGNALS:
                features[signal.signal_name] = signal.signal_value

        if set(features.keys()) != REQUIRED_SIGNALS:
            return None

        return features

    def get_staleness_flags(
        self,
        ticker: str,
        signals: list[SignalMessage],
    ) -> dict[str, bool]:
        """Return staleness flags for each signal.

        A signal is flagged as stale if its confidence is below
        the staleness threshold.

        Args:
            ticker: The stock ticker to check.
            signals: List of SignalMessage instances.

        Returns:
            Dict mapping signal_name -> is_stale (True if low confidence).
        """
        relevant = [s for s in signals if s.ticker == ticker]

        # Last signal for each name wins (same as assemble)
        signal_map: dict[str, SignalMessage] = {}
        for signal in relevant:
            if signal.signal_name in REQUIRED_SIGNALS:
                signal_map[signal.signal_name] = signal

        flags: dict[str, bool] = {}
        for name in REQUIRED_SIGNALS:
            if name in signal_map:
                flags[name] = signal_map[name].confidence < STALENESS_CONFIDENCE_THRESHOLD
            else:
                flags[name] = True  # Missing signals are stale

        return flags
