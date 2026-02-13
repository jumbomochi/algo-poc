import pytest
import numpy as np
from datetime import date, timedelta
from services.signal_generation.technical import (
    SupportProximitySignal, SupportStrengthSignal, SupportTrendSignal, find_support_levels,
)

def make_ohlcv(days=252, base_price=100.0):
    dates = [date(2024, 1, 2) + timedelta(days=i) for i in range(days)]
    np.random.seed(42)
    closes = base_price + np.cumsum(np.random.randn(days) * 0.5)
    for i in [50, 100, 150, 200]:
        closes[i:i+3] = 95.0
    return {
        "dates": dates,
        "open": closes + np.random.rand(days),
        "high": closes + abs(np.random.randn(days)),
        "low": closes - abs(np.random.randn(days)),
        "close": closes,
        "volume": np.random.randint(100000, 1000000, days),
    }

def test_find_support_levels_detects_bounces():
    data = make_ohlcv()
    levels = find_support_levels(data, lookback_days=252)
    assert len(levels) > 0
    assert any(abs(level - 95.0) < 3.0 for level in levels)

def test_support_proximity_signal():
    data = make_ohlcv()
    data["close"][-1] = 96.0
    sig = SupportProximitySignal()
    result = sig.compute(data)
    assert result.value > 0

def test_support_strength_signal():
    data = make_ohlcv()
    sig = SupportStrengthSignal()
    result = sig.compute(data)
    assert -1.0 <= result.value <= 1.0

def test_support_trend_signal():
    data = make_ohlcv()
    sig = SupportTrendSignal()
    result = sig.compute(data)
    assert -1.0 <= result.value <= 1.0
