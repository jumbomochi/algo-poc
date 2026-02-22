from __future__ import annotations

from typing import Any

import numpy as np

from services.signal_generation.base import Signal, SignalResult


def find_support_levels(
    data: dict[str, Any],
    lookback_days: int = 252,
    cluster_pct: float = 0.02,
) -> list[float]:
    """Identify support price levels from historical low prices.

    Algorithm:
    1. Extract local minima from the ``low`` price series (points where
       low[i] < low[i-1] and low[i] < low[i+1]).
    2. Cluster minima that fall within *cluster_pct* of each other into
       support zones.
    3. Score each zone by the number of touches.
    4. Return a sorted list of support level prices (zone averages).
    """
    lows: np.ndarray = np.asarray(data["low"], dtype=float)
    lows = lows[-lookback_days:]

    # --- Step 1: find local minima ---
    minima: list[float] = []
    for i in range(1, len(lows) - 1):
        if lows[i] < lows[i - 1] and lows[i] < lows[i + 1]:
            minima.append(float(lows[i]))

    if not minima:
        return []

    # --- Step 2: cluster minima within cluster_pct ---
    minima_sorted = sorted(minima)
    clusters: list[list[float]] = [[minima_sorted[0]]]
    for price in minima_sorted[1:]:
        cluster_avg = np.mean(clusters[-1])
        if abs(price - cluster_avg) / cluster_avg <= cluster_pct:
            clusters[-1].append(price)
        else:
            clusters.append([price])

    # --- Step 3 & 4: score by touches, return zone averages ---
    # Sort by number of touches descending, then return averages
    clusters.sort(key=len, reverse=True)
    levels = [float(np.mean(c)) for c in clusters]
    return levels


class SupportProximitySignal(Signal):
    """Measures how close the current price is to the nearest support level.

    Positive value when price is near/at support (bullish); negative when
    price has fallen below support.

    Scale:
    *  1.0 = right at support
    *  0.0 = 5 % above support
    * -1.0 = 5 % below support
    """

    name = "support_proximity"

    def compute(self, data: dict[str, Any]) -> SignalResult:
        closes = np.asarray(data["close"], dtype=float)
        current_close = float(closes[-1])

        levels = find_support_levels(data)
        if not levels:
            return SignalResult(value=0.0, confidence=0.0)

        # Find nearest support level at or below the current price.
        # Support is classically *below* price; only fall back to the
        # closest level above if no level exists below.
        below = [(lvl, current_close - lvl) for lvl in levels if lvl <= current_close]
        above = [(lvl, lvl - current_close) for lvl in levels if lvl > current_close]

        if below:
            # Closest support below (smallest positive distance)
            nearest_support = min(below, key=lambda t: t[1])[0]
        elif above:
            nearest_support = min(above, key=lambda t: t[1])[0]
        else:
            return SignalResult(value=0.0, confidence=0.0)

        # Percentage distance: positive means price is above support
        pct_distance = (current_close - nearest_support) / nearest_support

        # Map to signal value:
        # pct_distance == 0    -> value = 1.0  (at support)
        # pct_distance == 0.05 -> value = 0.0  (5% above)
        # pct_distance == -0.05 -> value = -1.0 (5% below)
        threshold = 0.05
        if pct_distance >= 0:
            value = 1.0 - (pct_distance / threshold)
        else:
            value = -abs(pct_distance) / threshold

        confidence = max(0.0, 1.0 - abs(pct_distance) / threshold)

        return SignalResult(value=value, confidence=confidence)


class SupportStrengthSignal(Signal):
    """Measures the strength of the nearest support level based on touch count.

    More touches = stronger support = higher positive value.
    """

    name = "support_strength"

    _MAX_EXPECTED_TOUCHES = 10

    def compute(self, data: dict[str, Any]) -> SignalResult:
        closes = np.asarray(data["close"], dtype=float)
        current_close = float(closes[-1])
        lows = np.asarray(data["low"], dtype=float)

        levels = find_support_levels(data)
        if not levels:
            return SignalResult(value=0.0, confidence=0.0)

        # Find nearest support level
        abs_distances = [abs(current_close - lvl) for lvl in levels]
        nearest_idx = int(np.argmin(abs_distances))
        nearest_support = levels[nearest_idx]

        # Count how many times price touched the nearest support zone (within 2%)
        cluster_pct = 0.02
        touches = sum(
            1
            for low_val in lows
            if abs(float(low_val) - nearest_support) / nearest_support <= cluster_pct
        )

        # Normalize: map touches to [0, 1] range, then scale to signal value
        normalized = min(touches / self._MAX_EXPECTED_TOUCHES, 1.0)
        # More touches = more bullish signal (stronger support)
        value = normalized * 2.0 - 1.0  # map [0, 1] -> [-1, 1]

        confidence = min(touches / 3.0, 1.0)  # need at least 3 touches for full confidence

        return SignalResult(value=value, confidence=confidence)


class SupportTrendSignal(Signal):
    """Measures whether support levels are trending up (higher lows) or down.

    Positive = rising supports (bullish), negative = falling supports (bearish).
    """

    name = "support_trend"

    def compute(self, data: dict[str, Any]) -> SignalResult:
        lows = np.asarray(data["low"], dtype=float)

        # Find local minima across the full series
        minima_indices: list[int] = []
        minima_values: list[float] = []
        for i in range(1, len(lows) - 1):
            if lows[i] < lows[i - 1] and lows[i] < lows[i + 1]:
                minima_indices.append(i)
                minima_values.append(float(lows[i]))

        if len(minima_values) < 2:
            return SignalResult(value=0.0, confidence=0.0)

        # Use simple linear regression on the minima to determine trend
        x = np.array(minima_indices, dtype=float)
        y = np.array(minima_values, dtype=float)

        # Normalize x to [0, 1]
        x_norm = (x - x[0]) / (x[-1] - x[0]) if x[-1] != x[0] else x - x[0]

        # Linear fit: y = mx + b
        n = len(x_norm)
        x_mean = np.mean(x_norm)
        y_mean = np.mean(y)
        numerator = np.sum((x_norm - x_mean) * (y - y_mean))
        denominator = np.sum((x_norm - x_mean) ** 2)

        if denominator == 0:
            return SignalResult(value=0.0, confidence=0.0)

        slope = numerator / denominator

        # Normalize slope: express as percentage of mean price
        slope_pct = slope / y_mean if y_mean != 0 else 0.0

        # Map to [-1, 1]: a 10% rise over the period maps to 1.0
        value = slope_pct / 0.10

        # Confidence based on R-squared
        y_pred = slope * x_norm + (y_mean - slope * x_mean)
        ss_res = np.sum((y - y_pred) ** 2)
        ss_tot = np.sum((y - y_mean) ** 2)
        r_squared = 1.0 - (ss_res / ss_tot) if ss_tot != 0 else 0.0
        confidence = max(0.0, r_squared)

        return SignalResult(value=value, confidence=confidence)


class RSISignal(Signal):
    """14-day Relative Strength Index mapped to a mean-reversion signal.

    Oversold (RSI < 35) produces positive values (bullish for mean reversion).
    Overbought (RSI > 65) produces negative values (bearish).

    Scale:
    *  1.0 = RSI at 0 (extremely oversold)
    *  0.0 = RSI at 50 (neutral)
    * -1.0 = RSI at 100 (extremely overbought)
    """

    name = "rsi"

    def __init__(self, period: int = 14) -> None:
        self.period = period

    def compute(self, data: dict[str, Any]) -> SignalResult:
        closes = np.asarray(data["close"], dtype=float)

        if len(closes) < self.period + 1:
            return SignalResult(value=0.0, confidence=0.0)

        deltas = np.diff(closes[-(self.period + 1):])
        gains = np.where(deltas > 0, deltas, 0.0)
        losses = np.where(deltas < 0, -deltas, 0.0)

        avg_gain = np.mean(gains)
        avg_loss = np.mean(losses)

        if avg_loss == 0:
            rsi = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi = 100.0 - (100.0 / (1.0 + rs))

        # Map RSI to signal value: RSI 50 -> 0, RSI 0 -> +1, RSI 100 -> -1
        value = (50.0 - rsi) / 50.0

        # Confidence: highest when RSI is at extremes (< 30 or > 70)
        distance_from_center = abs(rsi - 50.0)
        confidence = min(distance_from_center / 30.0, 1.0)

        return SignalResult(value=value, confidence=confidence)


class VolumeSignal(Signal):
    """Compares current volume to the 20-day moving average.

    Positive when volume is elevated (confirms institutional activity).
    Neutral at 1x average, maximum at 3x average.

    Scale:
    *  1.0 = volume at 3x+ the 20-day average
    *  0.0 = volume at 1x the 20-day average
    * -1.0 = volume at 0 (no trading)
    """

    name = "volume_ratio"

    def __init__(self, lookback: int = 20) -> None:
        self.lookback = lookback

    def compute(self, data: dict[str, Any]) -> SignalResult:
        volumes = np.asarray(data["volume"], dtype=float)

        if len(volumes) < self.lookback + 1:
            return SignalResult(value=0.0, confidence=0.0)

        avg_volume = np.mean(volumes[-(self.lookback + 1):-1])
        if avg_volume == 0:
            return SignalResult(value=0.0, confidence=0.0)

        current_volume = float(volumes[-1])
        ratio = current_volume / avg_volume

        # Map ratio to signal: 1x -> 0, 3x -> +1, 0x -> -1
        value = (ratio - 1.0) / 2.0

        # Confidence: high when ratio is clearly above or below 1
        confidence = min(abs(ratio - 1.0) / 1.0, 1.0)

        return SignalResult(value=value, confidence=confidence)


class BollingerBandSignal(Signal):
    """Bollinger Band signal for mean-reversion.

    Measures where price sits relative to the Bollinger Bands.
    Below lower band = bullish (oversold), above upper band = bearish (overbought).

    Scale:
    *  1.0 = price at or below lower band (2 std below MA)
    *  0.0 = price at middle band (MA)
    * -1.0 = price at or above upper band (2 std above MA)
    """

    name = "bollinger_band"

    def __init__(self, period: int = 20, num_std: float = 2.0) -> None:
        self.period = period
        self.num_std = num_std

    def compute(self, data: dict[str, Any]) -> SignalResult:
        closes = np.asarray(data["close"], dtype=float)

        if len(closes) < self.period + 1:
            return SignalResult(value=0.0, confidence=0.0)

        window = closes[-(self.period + 1):-1]
        ma = float(np.mean(window))
        std = float(np.std(window))

        current = float(closes[-1])

        # Use a minimum std of 1% of MA so bands are never zero-width
        min_std = ma * 0.01 if ma != 0 else 1e-8
        effective_std = max(std, min_std)

        upper = ma + self.num_std * effective_std
        lower = ma - self.num_std * effective_std
        band_width = upper - lower

        # Map position to [-1, 1]: lower band -> +1, upper band -> -1
        position = (ma - current) / (band_width / 2.0)
        value = max(-1.0, min(1.0, position))

        # Confidence: high when outside bands
        distance = abs(current - ma) / (self.num_std * effective_std)
        confidence = min(distance / 1.0, 1.0)

        return SignalResult(value=value, confidence=confidence)
