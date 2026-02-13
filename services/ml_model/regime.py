from __future__ import annotations

import math


class RegimeDetector:
    """Detects market regime (bull/bear/sideways) from return and volatility data.

    Classification logic:
        - Bull: positive mean returns, low volatility
        - Bear: negative mean returns, high volatility
        - Sideways: near-zero returns, moderate volatility

    Familiarity is a confidence score (0-1) indicating how clearly the
    data matches a single regime.
    """

    # Thresholds for regime classification
    _RETURN_BULL_THRESHOLD = 0.005   # > 0.5% mean return = positive trend
    _RETURN_BEAR_THRESHOLD = -0.005  # < -0.5% mean return = negative trend
    _VOL_LOW_THRESHOLD = 0.10        # < 10% = low volatility
    _VOL_HIGH_THRESHOLD = 0.20       # > 20% = high volatility

    def detect(
        self,
        returns: list[float],
        volatilities: list[float],
    ) -> tuple[str, float]:
        """Detect the market regime from historical returns and volatilities.

        Args:
            returns: List of period returns (e.g., weekly returns).
            volatilities: List of period volatilities.

        Returns:
            Tuple of (regime, familiarity) where:
                regime: "bull", "bear", or "sideways"
                familiarity: 0.0 to 1.0 confidence in the classification
        """
        if not returns or not volatilities:
            return "sideways", 0.0

        mean_return = sum(returns) / len(returns)
        mean_vol = sum(volatilities) / len(volatilities)

        # Score each regime dimension
        # Return score: how clearly positive or negative
        bull_return_score = self._sigmoid(
            mean_return, center=self._RETURN_BULL_THRESHOLD, steepness=200
        )
        bear_return_score = self._sigmoid(
            -mean_return, center=-self._RETURN_BEAR_THRESHOLD, steepness=200
        )

        # Volatility score: how clearly low or high
        low_vol_score = self._sigmoid(
            -mean_vol, center=-self._VOL_LOW_THRESHOLD, steepness=20
        )
        high_vol_score = self._sigmoid(
            mean_vol, center=self._VOL_HIGH_THRESHOLD, steepness=20
        )

        # Composite regime scores
        bull_score = bull_return_score * low_vol_score
        bear_score = bear_return_score * high_vol_score
        # Sideways: neither strong returns nor extreme volatility
        sideways_score = (1.0 - abs(bull_return_score - 0.5) * 2) * (
            1.0 - abs(high_vol_score - 0.5) * 2
        )
        # Ensure non-negative
        sideways_score = max(0.0, sideways_score)

        # Select regime with highest score
        scores = {
            "bull": bull_score,
            "bear": bear_score,
            "sideways": sideways_score,
        }
        regime = max(scores, key=scores.get)  # type: ignore[arg-type]

        # Familiarity: how dominant is the winning regime vs. others
        max_score = scores[regime]
        total_score = sum(scores.values())

        if total_score == 0:
            familiarity = 0.0
        else:
            # Ratio of winning score to total, scaled to be more discriminating
            dominance = max_score / total_score
            # Also factor in absolute strength of the winning signal
            familiarity = min(1.0, dominance * max_score * 2.0)

        return regime, familiarity

    @staticmethod
    def _sigmoid(x: float, center: float = 0.0, steepness: float = 1.0) -> float:
        """Smooth step function mapping values to [0, 1]."""
        z = steepness * (x - center)
        # Clamp to avoid overflow
        z = max(-500.0, min(500.0, z))
        return 1.0 / (1.0 + math.exp(-z))
