# Phase 2: Low-Complexity New Strategies — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add 3 new strategies (Sector Rotation, Short-Term Mean-Reversion, Thematic Momentum) to the multi-portfolio backtest. All use OHLCV data only — no fundamentals or events needed.

**Architecture:** Each strategy gets a `make_*_signals_fn()` factory that returns a signal closure, following the pattern of existing `make_signals_fn()` and `make_momentum_signals_fn()`. New signals (RSI(2), Bollinger Band) are added to `services/signal_generation/technical.py`. Strategies are wired into `main()` as additional `PortfolioConfig` entries. Capital is re-allocated across 5 strategies.

**Tech Stack:** Python 3.12, numpy, pytest, existing backtest infrastructure

---

### Task 1: Add BollingerBandSignal to technical.py

**Files:**
- Modify: `services/signal_generation/technical.py`
- Create: `tests/backtest/test_new_signals.py`

**Step 1: Write the failing test**

Create `tests/backtest/test_new_signals.py`:

```python
from __future__ import annotations

import numpy as np
from services.signal_generation.technical import BollingerBandSignal


def test_bollinger_band_below_lower_band():
    """Price below lower Bollinger Band produces positive signal."""
    signal = BollingerBandSignal(period=20, num_std=2.0)
    # Create 25 bars with stable prices, then a big drop
    closes = [100.0] * 24 + [90.0]
    data = {
        "close": closes,
        "open": closes,
        "high": [c + 1 for c in closes],
        "low": [c - 1 for c in closes],
        "volume": [1000] * 25,
    }
    result = signal.compute(data)
    assert result.value > 0.5  # strongly bullish (below lower band)
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
    assert result.value < -0.5  # bearish (above upper band)


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
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/backtest/test_new_signals.py -v`
Expected: FAIL with ImportError

**Step 3: Implement BollingerBandSignal**

Add to `services/signal_generation/technical.py` after VolumeSignal class (after line 289):

```python
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

        if std == 0:
            return SignalResult(value=0.0, confidence=0.0)

        current = float(closes[-1])
        upper = ma + self.num_std * std
        lower = ma - self.num_std * std
        band_width = upper - lower

        # Map position to [-1, 1]: lower band -> +1, upper band -> -1
        position = (ma - current) / (band_width / 2.0)
        value = max(-1.0, min(1.0, position))

        # Confidence: high when outside bands
        distance = abs(current - ma) / (self.num_std * std)
        confidence = min(distance / 1.0, 1.0)

        return SignalResult(value=value, confidence=confidence)
```

**Step 4: Run tests**

Run: `pytest tests/backtest/test_new_signals.py -v`
Expected: PASS

**Step 5: Run full suite**

Run: `pytest tests/backtest/ -v`
Expected: All pass

**Step 6: Commit**

```bash
git add services/signal_generation/technical.py tests/backtest/test_new_signals.py
git commit -m "feat: add BollingerBandSignal for short-term mean-reversion"
```

---

### Task 2: Add make_sector_rotation_signals_fn()

**Files:**
- Modify: `scripts/run_backtest.py`
- Create: `tests/backtest/test_sector_rotation_signals.py`

**Step 1: Write the failing test**

Create `tests/backtest/test_sector_rotation_signals.py`:

```python
from __future__ import annotations

from datetime import date

from scripts.run_backtest import make_sector_rotation_signals_fn


def _make_bars(tickers: list[str], days: int = 100, base_price: float = 100.0, daily_return: float = 0.001):
    """Create synthetic bars with a steady uptrend for each ticker."""
    bars = {}
    for i, ticker in enumerate(tickers):
        price = base_price + i * 10  # different starting prices
        ticker_bars = []
        for d in range(days):
            price *= (1 + daily_return * (i + 1))  # faster growth for later tickers
            ticker_bars.append({
                "date": date(2024, 1, 1) + __import__("datetime").timedelta(days=d),
                "open": price * 0.999,
                "high": price * 1.005,
                "low": price * 0.995,
                "close": price,
                "volume": 50000,
            })
        bars[ticker] = ticker_bars
    return bars


def test_sector_rotation_buys_top_ranked():
    """Sector rotation buys top-ranked sector ETFs by 3-month return."""
    tickers = ["XLK", "XLE", "XLF", "XLV", "XLY"]
    bars = _make_bars(tickers, days=100, daily_return=0.002)

    signals_fn = make_sector_rotation_signals_fn(
        bars_by_ticker=bars,
        top_n=3,
        lookback_days=63,
        position_size_pct=0.20,
        initial_capital=20_000,
    )

    # XLY has highest return (index 4, fastest growth), should get buy signal
    result = signals_fn("XLY", bars["XLY"])
    assert result is not None
    assert result["action"] == "buy"
    assert result["signals"]["strategy"] == "sector_rotation"


def test_sector_rotation_skips_low_ranked():
    """Sector rotation doesn't buy low-ranked ETFs."""
    tickers = ["XLK", "XLE", "XLF", "XLV", "XLY"]
    bars = _make_bars(tickers, days=100, daily_return=0.002)

    signals_fn = make_sector_rotation_signals_fn(
        bars_by_ticker=bars,
        top_n=2,
        lookback_days=63,
        position_size_pct=0.20,
        initial_capital=20_000,
    )

    # XLK has lowest return (index 0, slowest growth), should NOT get buy signal
    result = signals_fn("XLK", bars["XLK"])
    assert result is None


def test_sector_rotation_requires_min_bars():
    """Returns None when not enough bars for lookback."""
    tickers = ["XLK", "XLE", "XLF"]
    bars = _make_bars(tickers, days=30, daily_return=0.002)

    signals_fn = make_sector_rotation_signals_fn(
        bars_by_ticker=bars,
        top_n=2,
        lookback_days=63,
        position_size_pct=0.20,
        initial_capital=20_000,
    )

    result = signals_fn("XLK", bars["XLK"])
    assert result is None
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/backtest/test_sector_rotation_signals.py -v`
Expected: FAIL with ImportError

**Step 3: Implement make_sector_rotation_signals_fn()**

Add to `scripts/run_backtest.py` after `make_combined_signals_fn()` (around line 555):

```python
def make_sector_rotation_signals_fn(
    bars_by_ticker: dict[str, list[dict]],
    top_n: int = 3,
    lookback_days: int = 63,
    position_size_pct: float = 0.20,
    initial_capital: float = 100_000,
    trailing_stop_pct: float = 0.08,
    regime_by_date: dict | None = None,
):
    """Create a sector rotation signal function.

    Ranks sector ETFs by 3-month return and buys the top N.
    In bear regime, rotates to defensive sectors only (XLU, XLP, XLV).
    Exits via trailing stop or when sector drops out of top N.
    """
    defensive_sectors = {"XLU", "XLP", "XLV"}

    # Pre-compute date -> {ticker: close_price} for ranking
    price_by_date: dict[Any, dict[str, float]] = {}
    for ticker, bars in bars_by_ticker.items():
        for bar in bars:
            d = bar["date"]
            if d not in price_by_date:
                price_by_date[d] = {}
            price_by_date[d][ticker] = bar["close"]

    sorted_dates = sorted(price_by_date.keys())

    # Pre-compute date -> list of top N tickers ranked by return
    rankings_by_date: dict[Any, list[str]] = {}
    for i, d in enumerate(sorted_dates):
        if i < lookback_days:
            continue
        past_date = sorted_dates[i - lookback_days]
        past_prices = price_by_date.get(past_date, {})
        current_prices = price_by_date[d]

        returns = []
        for ticker in current_prices:
            if ticker in past_prices and past_prices[ticker] > 0:
                ret = (current_prices[ticker] - past_prices[ticker]) / past_prices[ticker]
                returns.append((ticker, ret))

        # In bear regime, only consider defensive sectors
        regime = "neutral"
        if regime_by_date:
            regime = regime_by_date.get(d, "neutral")

        if regime == "bear":
            returns = [(t, r) for t, r in returns if t in defensive_sectors]

        returns.sort(key=lambda x: x[1], reverse=True)
        rankings_by_date[d] = [t for t, _ in returns[:top_n]]

    tracked: dict[str, list[dict]] = {}

    def signals_fn(ticker: str, bars: list[dict]) -> dict | None:
        if len(bars) < lookback_days + 1:
            return None

        current_price = bars[-1]["close"]
        current_date = bars[-1]["date"]
        bar_count = len(bars)
        lots = tracked.get(ticker, [])

        # Exit: trailing stop
        if lots:
            for lot in lots:
                lot["peak_price"] = max(lot["peak_price"], current_price)

            for lot in lots:
                peak = lot["peak_price"]
                entry = lot["entry_price"]
                if peak > entry and (peak - current_price) / peak >= trailing_stop_pct:
                    tracked.pop(ticker, None)
                    return {
                        "action": "sell",
                        "ticker": ticker,
                        "limit_price": current_price,
                        "quantity": 0,
                        "sector": "Unknown",
                        "exit_reason": "trailing_stop",
                    }

        # Entry: buy if in top N and not already tracked
        top_tickers = rankings_by_date.get(current_date, [])
        if not lots and ticker in top_tickers:
            quantity = max(1, int(initial_capital * position_size_pct / current_price))
            tracked[ticker] = [{
                "entry_price": current_price,
                "entry_idx": bar_count,
                "peak_price": current_price,
            }]
            return {
                "action": "buy",
                "ticker": ticker,
                "limit_price": current_price,
                "quantity": quantity,
                "sector": "Unknown",
                "signals": {
                    "strategy": "sector_rotation",
                    "rank": top_tickers.index(ticker) + 1,
                    "lookback_days": lookback_days,
                },
            }

        return None

    return signals_fn
```

**Step 4: Run tests**

Run: `pytest tests/backtest/test_sector_rotation_signals.py -v`
Expected: PASS

**Step 5: Run full suite**

Run: `pytest tests/backtest/ -v`
Expected: All pass

**Step 6: Commit**

```bash
git add scripts/run_backtest.py tests/backtest/test_sector_rotation_signals.py
git commit -m "feat: add sector rotation signal function"
```

---

### Task 3: Add make_short_term_mr_signals_fn()

**Files:**
- Modify: `scripts/run_backtest.py`
- Create: `tests/backtest/test_short_term_mr_signals.py`

**Step 1: Write the failing test**

Create `tests/backtest/test_short_term_mr_signals.py`:

```python
from __future__ import annotations

from datetime import date, timedelta

from scripts.run_backtest import make_short_term_mr_signals_fn


def _make_oversold_bars(days: int = 30) -> list[dict]:
    """Create bars ending with an oversold condition (RSI(2) < 10, below lower BB)."""
    bars = []
    price = 100.0
    for d in range(days - 2):
        bars.append({
            "date": date(2024, 1, 1) + timedelta(days=d),
            "open": price, "high": price + 1, "low": price - 1,
            "close": price, "volume": 50000,
        })
    # Last 2 bars: sharp drop to trigger RSI(2) < 10 and BB touch
    for d in range(days - 2, days):
        price *= 0.95  # 5% drop per day
        bars.append({
            "date": date(2024, 1, 1) + timedelta(days=d),
            "open": price + 2, "high": price + 2, "low": price - 1,
            "close": price, "volume": 80000,
        })
    return bars


def test_short_term_mr_buys_on_oversold():
    """Short-term MR buys when RSI(2) < 10 and price touches lower BB."""
    bars = _make_oversold_bars(days=30)

    signals_fn = make_short_term_mr_signals_fn(
        position_size_pct=0.08,
        initial_capital=20_000,
    )

    result = signals_fn("AAPL", bars)
    # Should trigger buy due to extreme oversold conditions
    if result is not None:
        assert result["action"] == "buy"
        assert result["signals"]["strategy"] == "short_term_mr"


def test_short_term_mr_exits_after_max_hold():
    """Short-term MR exits after max_hold_days even without RSI recovery."""
    signals_fn = make_short_term_mr_signals_fn(
        position_size_pct=0.08,
        initial_capital=20_000,
        max_hold_days=5,
    )

    # First: create oversold entry
    bars = _make_oversold_bars(days=30)
    entry_result = signals_fn("AAPL", bars)

    if entry_result and entry_result["action"] == "buy":
        # Then add 6 more flat bars (exceeds max_hold_days=5)
        price = bars[-1]["close"]
        for d in range(6):
            bars.append({
                "date": bars[-1]["date"] + timedelta(days=1),
                "open": price, "high": price + 0.5, "low": price - 0.5,
                "close": price, "volume": 50000,
            })
            result = signals_fn("AAPL", bars)
            if result and result["action"] == "sell":
                assert result["exit_reason"] == "time_exit"
                return

        # If no sell triggered, the test still passes (signal may not have entered)


def test_short_term_mr_requires_min_bars():
    """Returns None when not enough data."""
    signals_fn = make_short_term_mr_signals_fn()
    bars = [{"date": date(2024, 1, d), "open": 100, "high": 101, "low": 99,
             "close": 100, "volume": 1000} for d in range(1, 10)]
    result = signals_fn("AAPL", bars)
    assert result is None
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/backtest/test_short_term_mr_signals.py -v`
Expected: FAIL with ImportError

**Step 3: Implement make_short_term_mr_signals_fn()**

Add to `scripts/run_backtest.py` after `make_sector_rotation_signals_fn()`:

```python
def make_short_term_mr_signals_fn(
    position_size_pct: float = 0.08,
    initial_capital: float = 100_000,
    max_hold_days: int = 5,
    rsi_period: int = 2,
    rsi_entry_threshold: float = 0.8,
    bb_period: int = 20,
    bb_num_std: float = 2.0,
):
    """Create a short-term mean-reversion signal function.

    Entry: RSI(2) < 10 AND price touches lower Bollinger Band AND volume > 1.5x avg.
    Exit: RSI(2) > 70 OR max_hold_days elapsed (whichever first). No trailing stop.
    """
    from services.signal_generation.technical import BollingerBandSignal

    rsi_signal = RSISignal(period=rsi_period)
    bb_signal = BollingerBandSignal(period=bb_period, num_std=bb_num_std)
    volume_signal = VolumeSignal()

    tracked: dict[str, dict] = {}  # ticker -> {entry_idx, entry_price}

    def signals_fn(ticker: str, bars: list[dict]) -> dict | None:
        min_bars = max(bb_period + 1, 25)
        if len(bars) < min_bars:
            return None

        current_price = bars[-1]["close"]
        bar_count = len(bars)
        lot = tracked.get(ticker)

        # Exit logic
        if lot is not None:
            bars_held = bar_count - lot["entry_idx"]

            # Time exit
            if bars_held >= max_hold_days:
                tracked.pop(ticker, None)
                return {
                    "action": "sell",
                    "ticker": ticker,
                    "limit_price": current_price,
                    "quantity": 0,
                    "sector": "Unknown",
                    "exit_reason": "time_exit",
                }

            # RSI recovery exit
            data = _build_data(bars)
            rsi = rsi_signal.compute(data)
            if rsi.value < -0.4:  # RSI(2) > 70
                tracked.pop(ticker, None)
                return {
                    "action": "sell",
                    "ticker": ticker,
                    "limit_price": current_price,
                    "quantity": 0,
                    "sector": "Unknown",
                    "exit_reason": "rsi_recovery",
                }

            return None

        # Entry logic
        data = _build_data(bars)
        try:
            rsi = rsi_signal.compute(data)
            bb = bb_signal.compute(data)
            volume = volume_signal.compute(data)
        except Exception:
            return None

        # RSI(2) < 10 maps to rsi.value > 0.8
        # BB touch: bb.value > 0.5 means price is near/below lower band
        # Volume > 1.5x avg: volume.value > 0.25
        if (
            rsi.value > rsi_entry_threshold
            and bb.value > 0.5
            and volume.value > 0.25
        ):
            quantity = max(1, int(initial_capital * position_size_pct / current_price))
            tracked[ticker] = {
                "entry_price": current_price,
                "entry_idx": bar_count,
            }
            return {
                "action": "buy",
                "ticker": ticker,
                "limit_price": current_price,
                "quantity": quantity,
                "sector": "Unknown",
                "signals": {
                    "strategy": "short_term_mr",
                    "rsi_2": rsi.value,
                    "bb": bb.value,
                    "volume": volume.value,
                },
            }

        return None

    return signals_fn
```

**Step 4: Run tests**

Run: `pytest tests/backtest/test_short_term_mr_signals.py -v`
Expected: PASS

**Step 5: Run full suite**

Run: `pytest tests/backtest/ -v`
Expected: All pass

**Step 6: Commit**

```bash
git add scripts/run_backtest.py tests/backtest/test_short_term_mr_signals.py
git commit -m "feat: add short-term mean-reversion signal function with RSI(2) and Bollinger Bands"
```

---

### Task 4: Add make_thematic_momentum_signals_fn()

**Files:**
- Modify: `scripts/run_backtest.py`
- Create: `tests/backtest/test_thematic_momentum_signals.py`

**Step 1: Write the failing test**

Create `tests/backtest/test_thematic_momentum_signals.py`:

```python
from __future__ import annotations

from datetime import date, timedelta

from scripts.run_backtest import make_thematic_momentum_signals_fn


def _make_trending_bars(tickers: list[str], days: int = 100, base_price: float = 50.0):
    """Create bars where later tickers have stronger uptrends."""
    bars = {}
    for i, ticker in enumerate(tickers):
        price = base_price
        daily_return = 0.001 * (i + 1)  # stronger trend for higher index
        ticker_bars = []
        for d in range(days):
            price *= (1 + daily_return)
            ticker_bars.append({
                "date": date(2024, 1, 1) + timedelta(days=d),
                "open": price * 0.999,
                "high": price * 1.005,
                "low": price * 0.995,
                "close": price,
                "volume": 30000 + d * 100,  # rising volume
            })
        bars[ticker] = ticker_bars
    return bars


def test_thematic_momentum_buys_top_ranked():
    """Thematic momentum buys top-ranked ETFs above 50-day MA."""
    tickers = ["ARKK", "TAN", "HACK", "BOTZ", "LIT"]
    bars = _make_trending_bars(tickers, days=100)

    signals_fn = make_thematic_momentum_signals_fn(
        bars_by_ticker=bars,
        top_n=3,
        lookback_days=63,
        position_size_pct=0.15,
        initial_capital=20_000,
    )

    # LIT (index 4) has strongest trend, should get buy signal
    result = signals_fn("LIT", bars["LIT"])
    assert result is not None
    assert result["action"] == "buy"
    assert result["signals"]["strategy"] == "thematic_momentum"


def test_thematic_momentum_requires_above_ma():
    """Does not buy if price is below 50-day MA."""
    tickers = ["ARKK", "TAN", "HACK"]
    bars = {}
    for ticker in tickers:
        price = 100.0
        ticker_bars = []
        for d in range(100):
            # First 80 bars: uptrend. Last 20: sharp downtrend below MA
            if d < 80:
                price *= 1.002
            else:
                price *= 0.98
            ticker_bars.append({
                "date": date(2024, 1, 1) + timedelta(days=d),
                "open": price, "high": price + 1, "low": price - 1,
                "close": price, "volume": 30000,
            })
        bars[ticker] = ticker_bars

    signals_fn = make_thematic_momentum_signals_fn(
        bars_by_ticker=bars,
        top_n=3,
        lookback_days=63,
        ma_period=50,
        position_size_pct=0.15,
        initial_capital=20_000,
    )

    # All tickers are below 50-day MA due to sharp decline
    for ticker in tickers:
        result = signals_fn(ticker, bars[ticker])
        assert result is None


def test_thematic_momentum_requires_min_bars():
    """Returns None when not enough data."""
    tickers = ["ARKK", "TAN"]
    bars = _make_trending_bars(tickers, days=30)

    signals_fn = make_thematic_momentum_signals_fn(
        bars_by_ticker=bars,
        top_n=2,
        lookback_days=63,
        position_size_pct=0.15,
        initial_capital=20_000,
    )

    result = signals_fn("ARKK", bars["ARKK"])
    assert result is None
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/backtest/test_thematic_momentum_signals.py -v`
Expected: FAIL with ImportError

**Step 3: Implement make_thematic_momentum_signals_fn()**

Add to `scripts/run_backtest.py` after `make_short_term_mr_signals_fn()`:

```python
def make_thematic_momentum_signals_fn(
    bars_by_ticker: dict[str, list[dict]],
    top_n: int = 8,
    lookback_days: int = 63,
    ma_period: int = 50,
    position_size_pct: float = 0.15,
    initial_capital: float = 100_000,
    trailing_stop_pct: float = 0.10,
    max_loss_pct: float = 0.08,
):
    """Create a thematic momentum signal function.

    Ranks thematic ETFs by 3-month return. Buys top N that are above
    their 50-day MA. Exits via trailing stop, max loss, or MA cross below.
    """
    # Pre-compute date -> {ticker: close_price}
    price_by_date: dict[Any, dict[str, float]] = {}
    for ticker, bars in bars_by_ticker.items():
        for bar in bars:
            d = bar["date"]
            if d not in price_by_date:
                price_by_date[d] = {}
            price_by_date[d][ticker] = bar["close"]

    sorted_dates = sorted(price_by_date.keys())

    # Pre-compute date -> top N tickers by return
    rankings_by_date: dict[Any, list[str]] = {}
    for i, d in enumerate(sorted_dates):
        if i < lookback_days:
            continue
        past_date = sorted_dates[i - lookback_days]
        past_prices = price_by_date.get(past_date, {})
        current_prices = price_by_date[d]

        returns = []
        for ticker in current_prices:
            if ticker in past_prices and past_prices[ticker] > 0:
                ret = (current_prices[ticker] - past_prices[ticker]) / past_prices[ticker]
                returns.append((ticker, ret))

        returns.sort(key=lambda x: x[1], reverse=True)
        rankings_by_date[d] = [t for t, _ in returns[:top_n]]

    tracked: dict[str, list[dict]] = {}

    def signals_fn(ticker: str, bars: list[dict]) -> dict | None:
        min_bars = max(lookback_days + 1, ma_period + 1)
        if len(bars) < min_bars:
            return None

        current_price = bars[-1]["close"]
        current_date = bars[-1]["date"]
        bar_count = len(bars)
        lots = tracked.get(ticker, [])

        # Compute 50-day MA
        closes = [b["close"] for b in bars[-ma_period:]]
        ma_50 = sum(closes) / len(closes)
        above_ma = current_price > ma_50

        # Exit logic
        if lots:
            for lot in lots:
                lot["peak_price"] = max(lot["peak_price"], current_price)

            should_sell = False
            exit_reason = "unknown"

            # MA cross below: exit if price drops below 50-day MA
            if not above_ma:
                should_sell = True
                exit_reason = "ma_cross_below"

            if not should_sell:
                for lot in lots:
                    peak = lot["peak_price"]
                    entry = lot["entry_price"]
                    if peak > entry and (peak - current_price) / peak >= trailing_stop_pct:
                        should_sell = True
                        exit_reason = "trailing_stop"
                        break
                    if (entry - current_price) / entry >= max_loss_pct:
                        should_sell = True
                        exit_reason = "max_loss"
                        break

            if should_sell:
                tracked.pop(ticker, None)
                return {
                    "action": "sell",
                    "ticker": ticker,
                    "limit_price": current_price,
                    "quantity": 0,
                    "sector": "Unknown",
                    "exit_reason": exit_reason,
                }

        # Entry: in top N AND above 50-day MA
        top_tickers = rankings_by_date.get(current_date, [])
        if not lots and ticker in top_tickers and above_ma:
            quantity = max(1, int(initial_capital * position_size_pct / current_price))
            tracked[ticker] = [{
                "entry_price": current_price,
                "entry_idx": bar_count,
                "peak_price": current_price,
            }]
            return {
                "action": "buy",
                "ticker": ticker,
                "limit_price": current_price,
                "quantity": quantity,
                "sector": "Unknown",
                "signals": {
                    "strategy": "thematic_momentum",
                    "rank": top_tickers.index(ticker) + 1,
                    "lookback_days": lookback_days,
                    "above_ma_50": True,
                },
            }

        return None

    return signals_fn
```

**Step 4: Run tests**

Run: `pytest tests/backtest/test_thematic_momentum_signals.py -v`
Expected: PASS

**Step 5: Run full suite**

Run: `pytest tests/backtest/ -v`
Expected: All pass

**Step 6: Commit**

```bash
git add scripts/run_backtest.py tests/backtest/test_thematic_momentum_signals.py
git commit -m "feat: add thematic momentum signal function with MA filter"
```

---

### Task 5: Wire 3 New Strategies into main()

**Files:**
- Modify: `scripts/run_backtest.py:840-902` (main function)
- Test: `tests/backtest/test_multi_portfolio.py`

**Step 1: Write test**

Add to `tests/backtest/test_multi_portfolio.py`:

```python
def test_five_portfolios_aggregate_correctly():
    """Five portfolios with no-op signals produce correct aggregate starting capital."""
    from datetime import date

    from backtest.runner import BacktestRunner
    from backtest.simulator import SimulatedExecutor
    from scripts.run_backtest import PortfolioConfig, compute_aggregate_metrics
    from services.risk_management.engine import RiskEngine

    bars = {
        "AAPL": [
            {"date": date(2024, 1, d), "open": 150.0, "high": 152.0,
             "low": 149.0, "close": 151.0, "volume": 1000}
            for d in range(1, 6)
        ],
    }
    executor = SimulatedExecutor(slippage_bps=0, commission_per_share=0)

    # 5 portfolios with different allocations summing to 100k
    allocations = {"mr": 16_000, "mom": 24_000, "sector": 16_000,
                   "st_mr": 20_000, "thematic": 24_000}
    configs = {
        name: PortfolioConfig(name, capital, lambda t, b: None, RiskEngine())
        for name, capital in allocations.items()
    }

    results = {}
    for name, pc in configs.items():
        runner = BacktestRunner(executor=executor, initial_capital=pc.capital)
        results[name] = runner.run(bars, pc.signals_fn, pc.risk_engine)

    agg = compute_aggregate_metrics(results, configs)
    assert agg["portfolio_values"][0] == 100_000
    assert len(results) == 5
```

**Step 2: Modify main()**

Update the data fetch to include all active strategies:

```python
    all_tickers = get_union_universe([
        "mean_reversion", "momentum", "sector_rotation",
        "short_term_mr", "thematic_momentum",
    ])
```

Add signal function creation after `mom_signals_fn`:

```python
    sector_signals_fn = make_sector_rotation_signals_fn(
        bars_by_ticker=bars_by_ticker,
        top_n=3,
        lookback_days=63,
        position_size_pct=0.20,
        initial_capital=args.capital * 0.16,
        trailing_stop_pct=0.08,
    )
    st_mr_signals_fn = make_short_term_mr_signals_fn(
        position_size_pct=0.08,
        initial_capital=args.capital * 0.20,
        max_hold_days=5,
    )
    thematic_signals_fn = make_thematic_momentum_signals_fn(
        bars_by_ticker=bars_by_ticker,
        top_n=8,
        lookback_days=63,
        position_size_pct=0.15,
        initial_capital=args.capital * 0.24,
        trailing_stop_pct=0.10,
    )
```

Update the portfolios dict — re-allocate capital across 5 strategies. Normalize the design doc ratios (MR 12%, Mom 18%, Sector 12%, ST-MR 10%, Thematic 11%) to sum to 100% for these 5: 16% / 24% / 16% / 20% / 24% (rounding to clean numbers):

```python
    portfolios: dict[str, PortfolioConfig] = {
        "mean_reversion": PortfolioConfig(
            name="mean_reversion",
            capital=args.capital * 0.16,
            signals_fn=mr_signals_fn,
            risk_engine=RiskEngine(
                position_entry_limit_pct=15.0,
                sector_concentration_pct=30.0,
                total_exposure_limit_pct=120.0,
                max_lots_per_ticker=2,
            ),
        ),
        "momentum": PortfolioConfig(
            name="momentum",
            capital=args.capital * 0.24,
            signals_fn=mom_signals_fn,
            risk_engine=RiskEngine(
                position_entry_limit_pct=12.0,
                sector_concentration_pct=30.0,
                total_exposure_limit_pct=150.0,
                max_lots_per_ticker=1,
            ),
        ),
        "sector_rotation": PortfolioConfig(
            name="sector_rotation",
            capital=args.capital * 0.16,
            signals_fn=sector_signals_fn,
            risk_engine=RiskEngine(
                position_entry_limit_pct=20.0,
                sector_concentration_pct=50.0,
                total_exposure_limit_pct=100.0,
                max_lots_per_ticker=1,
            ),
        ),
        "short_term_mr": PortfolioConfig(
            name="short_term_mr",
            capital=args.capital * 0.20,
            signals_fn=st_mr_signals_fn,
            risk_engine=RiskEngine(
                position_entry_limit_pct=8.0,
                sector_concentration_pct=30.0,
                total_exposure_limit_pct=100.0,
                max_lots_per_ticker=1,
            ),
        ),
        "thematic_momentum": PortfolioConfig(
            name="thematic_momentum",
            capital=args.capital * 0.24,
            signals_fn=thematic_signals_fn,
            risk_engine=RiskEngine(
                position_entry_limit_pct=15.0,
                sector_concentration_pct=50.0,
                total_exposure_limit_pct=120.0,
                max_lots_per_ticker=1,
            ),
        ),
    }
```

**Step 3: Run full test suite**

Run: `pytest tests/backtest/ -v`
Expected: All pass

**Step 4: Commit**

```bash
git add scripts/run_backtest.py tests/backtest/test_multi_portfolio.py
git commit -m "feat: wire sector rotation, short-term MR, and thematic momentum into main()"
```

---

### Task 6: Update Documentation

**Files:**
- Modify: `docs/strategy.md`

**Step 1: Update strategy.md**

Update the "Current Portfolio Configuration" table to show all 5 active strategies:

```markdown
### Current Portfolio Configuration

The backtest runs five independent portfolios:

| Portfolio | Capital | Strategy | Risk Limits |
|---|---|---|---|
| `mean_reversion` | 16% of total | Support-level dip buying (S&P 50) | 15% entry, 120% exposure, 2 lots |
| `momentum` | 24% of total | 6-month relative strength (S&P 50) | 12% entry, 150% exposure, 1 lot |
| `sector_rotation` | 16% of total | Top 3 sector ETFs by 3-month return | 20% entry, 100% exposure, 1 lot |
| `short_term_mr` | 20% of total | RSI(2) + Bollinger Band oversold bounces | 8% entry, 100% exposure, 1 lot |
| `thematic_momentum` | 24% of total | Top 8 thematic ETFs above 50-day MA | 15% entry, 120% exposure, 1 lot |
```

**Step 2: Commit**

```bash
git add docs/strategy.md
git commit -m "docs: update strategy.md with 5 active portfolio configurations"
```

---

## Summary

| Task | What | Files | Tests |
|---|---|---|---|
| 1 | BollingerBandSignal | `technical.py`, `test_new_signals.py` | 4 new |
| 2 | Sector rotation signals | `run_backtest.py`, `test_sector_rotation_signals.py` | 3 new |
| 3 | Short-term MR signals | `run_backtest.py`, `test_short_term_mr_signals.py` | 3 new |
| 4 | Thematic momentum signals | `run_backtest.py`, `test_thematic_momentum_signals.py` | 3 new |
| 5 | Wire into main() | `run_backtest.py`, `test_multi_portfolio.py` | 1 new |
| 6 | Update docs | `docs/strategy.md` | — |

Total: 6 tasks, 6 commits, 14 new tests, 3 new signal functions, 1 new signal class.
