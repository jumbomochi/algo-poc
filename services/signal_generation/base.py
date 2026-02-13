from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class SignalResult:
    """Result of a signal computation.

    Attributes:
        value: Signal value clamped to [-1.0, 1.0].
               Positive = bullish, negative = bearish.
        confidence: Confidence score clamped to [0.0, 1.0].
    """

    value: float  # -1.0 to 1.0
    confidence: float  # 0.0 to 1.0

    def __post_init__(self):
        self.value = max(-1.0, min(1.0, self.value))
        self.confidence = max(0.0, min(1.0, self.confidence))


class Signal(ABC):
    """Abstract base class for all trading signals."""

    name: str = ""

    @abstractmethod
    def compute(self, data: dict[str, Any]) -> SignalResult:
        ...
