from __future__ import annotations

import pytest

from services.ml_model.regime import RegimeDetector


class TestRegimeDetector:
    def test_bull_regime_positive_returns_low_vol(self):
        detector = RegimeDetector()
        # Positive returns, low volatility
        returns = [0.02, 0.01, 0.015, 0.025, 0.01, 0.02, 0.03, 0.015]
        volatilities = [0.05, 0.04, 0.06, 0.05, 0.04, 0.03, 0.05, 0.04]
        regime, familiarity = detector.detect(returns, volatilities)

        assert regime == "bull"
        assert 0.0 <= familiarity <= 1.0

    def test_bear_regime_negative_returns_high_vol(self):
        detector = RegimeDetector()
        # Negative returns, high volatility
        returns = [-0.03, -0.02, -0.04, -0.015, -0.03, -0.025, -0.02, -0.035]
        volatilities = [0.25, 0.30, 0.28, 0.35, 0.32, 0.27, 0.31, 0.29]
        regime, familiarity = detector.detect(returns, volatilities)

        assert regime == "bear"
        assert 0.0 <= familiarity <= 1.0

    def test_sideways_regime_low_returns_moderate_vol(self):
        detector = RegimeDetector()
        # Near-zero returns, moderate volatility
        returns = [0.001, -0.002, 0.003, -0.001, 0.002, -0.003, 0.001, -0.002]
        volatilities = [0.12, 0.11, 0.13, 0.10, 0.12, 0.11, 0.13, 0.12]
        regime, familiarity = detector.detect(returns, volatilities)

        assert regime == "sideways"
        assert 0.0 <= familiarity <= 1.0

    def test_familiarity_high_for_clear_bull(self):
        detector = RegimeDetector()
        # Very clear bull market
        returns = [0.05, 0.04, 0.06, 0.03, 0.05, 0.04, 0.06, 0.05]
        volatilities = [0.02, 0.03, 0.02, 0.01, 0.02, 0.03, 0.02, 0.01]
        _, familiarity = detector.detect(returns, volatilities)

        assert familiarity > 0.5

    def test_familiarity_high_for_clear_bear(self):
        detector = RegimeDetector()
        # Very clear bear market
        returns = [-0.05, -0.06, -0.04, -0.07, -0.05, -0.06, -0.04, -0.05]
        volatilities = [0.35, 0.40, 0.38, 0.42, 0.37, 0.39, 0.41, 0.36]
        _, familiarity = detector.detect(returns, volatilities)

        assert familiarity > 0.5

    def test_empty_inputs_returns_sideways(self):
        detector = RegimeDetector()
        regime, familiarity = detector.detect([], [])

        assert regime == "sideways"
        assert familiarity == 0.0

    def test_single_observation(self):
        detector = RegimeDetector()
        regime, familiarity = detector.detect([0.01], [0.05])

        assert regime in ("bull", "bear", "sideways")
        assert 0.0 <= familiarity <= 1.0

    def test_mixed_signals_lower_familiarity(self):
        detector = RegimeDetector()
        # Mixed returns: some positive, some negative
        returns = [0.02, -0.03, 0.01, -0.02, 0.03, -0.01, 0.02, -0.02]
        volatilities = [0.15, 0.20, 0.12, 0.18, 0.16, 0.14, 0.19, 0.17]

        _, familiarity = detector.detect(returns, volatilities)
        # Mixed signals should produce lower familiarity than clear regimes
        assert familiarity < 0.8
