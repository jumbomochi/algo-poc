from __future__ import annotations

from typing import Any

from services.signal_generation.base import Signal, SignalResult


class EarningsSurpriseSignal(Signal):
    """Measures the magnitude of an earnings surprise.

    Reads actual_eps and estimate_eps from data.
    Positive when actual beats estimate, negative when it misses.
    """

    name = "earnings_surprise"

    # A 10% surprise maps to roughly signal value 1.0
    _SURPRISE_SCALE = 0.10

    def compute(self, data: dict[str, Any]) -> SignalResult:
        actual = data["actual_eps"]
        estimate = data["estimate_eps"]

        if estimate == 0:
            # Avoid division by zero; use raw difference
            surprise = actual - estimate
        else:
            surprise = (actual - estimate) / abs(estimate)

        value = surprise / self._SURPRISE_SCALE

        confidence = min(abs(value), 1.0)

        return SignalResult(value=value, confidence=confidence)


class NewsSentimentSignal(Signal):
    """Pass-through signal for pre-computed news sentiment.

    Reads sentiment_score (already -1 to 1 range) from data.
    Normalizes and passes through as signal value.
    """

    name = "news_sentiment"

    def compute(self, data: dict[str, Any]) -> SignalResult:
        score = data["sentiment_score"]

        # The score is already in [-1, 1] range; SignalResult clamps if needed
        value = score
        confidence = abs(score)

        return SignalResult(value=value, confidence=confidence)


class InsiderActivitySignal(Signal):
    """Measures net insider buying/selling activity.

    Reads insider_buy_value and insider_sell_value from data.
    Positive when net buying, negative when net selling.
    """

    name = "insider_activity"

    # $1M net activity maps to roughly signal value 1.0
    _ACTIVITY_SCALE = 1_000_000

    def compute(self, data: dict[str, Any]) -> SignalResult:
        buy_value = data["insider_buy_value"]
        sell_value = data["insider_sell_value"]

        net = buy_value - sell_value
        total = buy_value + sell_value

        if total == 0:
            return SignalResult(value=0.0, confidence=0.0)

        # Scale by the reference amount
        value = net / self._ACTIVITY_SCALE

        # Confidence based on total activity volume relative to scale
        confidence = min(total / self._ACTIVITY_SCALE, 1.0)

        return SignalResult(value=value, confidence=confidence)
