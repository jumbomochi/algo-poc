import pytest
from services.signal_generation.base import Signal, SignalResult

class DummySignal(Signal):
    name = "dummy"
    def compute(self, data: dict) -> SignalResult:
        return SignalResult(value=0.5, confidence=0.9)

def test_signal_compute_returns_result():
    sig = DummySignal()
    result = sig.compute({"close": [100, 105, 110]})
    assert isinstance(result, SignalResult)
    assert -1.0 <= result.value <= 1.0
    assert 0.0 <= result.confidence <= 1.0

def test_signal_result_clamps_value():
    r = SignalResult(value=1.5, confidence=0.5)
    assert r.value == 1.0

def test_signal_has_name():
    sig = DummySignal()
    assert sig.name == "dummy"

def test_signal_is_abstract():
    with pytest.raises(TypeError):
        Signal()
