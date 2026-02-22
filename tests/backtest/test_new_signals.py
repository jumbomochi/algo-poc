from __future__ import annotations

import numpy as np
from services.signal_generation.technical import BollingerBandSignal


def test_bollinger_band_below_lower_band():
    """Price below lower Bollinger Band produces positive signal."""
    signal = BollingerBandSignal(period=20, num_std=2.0)
    closes = [100.0] * 24 + [90.0]
    data = {
        "close": closes,
        "open": closes,
        "high": [c + 1 for c in closes],
        "low": [c - 1 for c in closes],
        "volume": [1000] * 25,
    }
    result = signal.compute(data)
    assert result.value > 0.5
    assert result.confidence > 0.5


def test_bollinger_band_above_upper_band():
    """Price above upper Bollinger Band produces negative signal."""
    signal = BollingerBandSignal(period=20, num_std=2.0)
    closes = [100.0] * 24 + [110.0]
    data = {
        "close": closes,
        "open": closes,
        "high": [c + 1 for c in closes],
        "low": [c - 1 for c in closes],
        "volume": [1000] * 25,
    }
    result = signal.compute(data)
    assert result.value < -0.5


def test_bollinger_band_at_middle():
    """Price at middle band produces near-zero signal."""
    signal = BollingerBandSignal(period=20, num_std=2.0)
    closes = [100.0] * 25
    data = {
        "close": closes,
        "open": closes,
        "high": [c + 1 for c in closes],
        "low": [c - 1 for c in closes],
        "volume": [1000] * 25,
    }
    result = signal.compute(data)
    assert abs(result.value) < 0.2


def test_bollinger_band_insufficient_data():
    """Returns zero signal when not enough data."""
    signal = BollingerBandSignal(period=20, num_std=2.0)
    data = {"close": [100.0] * 10, "open": [100.0] * 10,
            "high": [101.0] * 10, "low": [99.0] * 10, "volume": [1000] * 10}
    result = signal.compute(data)
    assert result.value == 0.0
    assert result.confidence == 0.0
