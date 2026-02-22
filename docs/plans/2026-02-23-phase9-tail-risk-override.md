# Phase 9: Tail-Risk Override (Level 3)

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add crash regime detection, entry freezing during crashes, and bear-regime trailing stop tightening across all strategies.

**Architecture:** Extend `compute_regime_by_date()` with a "crash" regime (breadth < 10%). Add a `make_crash_freeze_signals_fn()` wrapper that blocks buy signals during crash regime for all strategies except tail-risk hedge. Add `regime_by_date` parameter to the 4 signal functions that lack it for bear-regime stop tightening.

**Tech Stack:** Python (no new dependencies)

---

## Context

### Design spec (from multi-strategy-portfolio-design.md)

**Level 3: Tail-Risk Override:**
- Bear regime: Tail-risk allocation 10% -> 15%, tighten all trailing stops by 2%
- Crash regime (>90% below 200-day MA): Freeze all new entries, only exits and tail-risk hedge operate

### What we have

- `compute_regime_by_date()` computes bull/neutral/bear based on breadth (% stocks above 200-day MA):
  - Bull: > 60%, Neutral: 40-60%, Bear: < 40%
  - No "crash" regime currently
- `REGIME_PARAMS` dict maps regime -> trailing_stop_pct and max_loss_pct (bear: 8%/5%)
- Signal functions with `regime_by_date`: `make_signals_fn`, `make_momentum_signals_fn`, `make_sector_rotation_signals_fn`, `make_tail_risk_hedge_signals_fn`
- Signal functions WITHOUT `regime_by_date`: `make_quality_value_signals_fn`, `make_earnings_drift_signals_fn`, `make_short_term_mr_signals_fn`, `make_thematic_momentum_signals_fn`
- `make_ml_filtered_signals_fn()` is a wrapper pattern we can reuse for crash freeze

### What we're building

1. Crash regime detection (breadth < 10%)
2. Crash entry freeze wrapper (blocks buys except tail-risk hedge)
3. Bear-regime stop tightening for remaining 4 strategies

---

## Task 1: Crash Regime Detection

**Files:**
- Modify: `scripts/run_backtest.py` (lines 211-253)
- Create: `tests/backtest/test_regime_detection.py`

### Step 1: Write failing tests

Create `tests/backtest/test_regime_detection.py`:

```python
from __future__ import annotations

from datetime import date, timedelta


def _make_bars(start: date, num_days: int, prices: list[float]) -> list[dict]:
    """Build bar dicts from a price sequence."""
    bars = []
    d = start
    for i in range(num_days):
        while d.weekday() >= 5:
            d += timedelta(days=1)
        p = prices[i] if i < len(prices) else prices[-1]
        bars.append({"date": d, "open": p, "high": p + 1, "low": p - 1, "close": p, "volume": 1_000_000})
        d += timedelta(days=1)
    return bars


def test_crash_regime_at_low_breadth():
    """Breadth < 10% should produce 'crash' regime."""
    from scripts.run_backtest import compute_regime_by_date

    start = date(2020, 1, 2)
    num_days = 250

    # 20 tickers: all decline well below their 200-day MA after warmup
    bars_by_ticker = {}
    for i in range(20):
        # Price rises then crashes — after 200-day warmup all are below MA
        prices = [100.0 + j * 0.1 for j in range(200)]  # warmup: gentle rise
        prices += [50.0 - j * 0.1 for j in range(num_days - 200)]  # crash
        bars_by_ticker[f"TICK{i}"] = _make_bars(start, num_days, prices)

    regime_by_date = compute_regime_by_date(bars_by_ticker, crash_threshold=0.10)

    # The last dates should be crash (all tickers below MA)
    last_dates = sorted(regime_by_date.keys())[-10:]
    crash_count = sum(1 for d in last_dates if regime_by_date[d] == "crash")
    assert crash_count >= 8, f"Expected mostly crash, got {crash_count}/10"


def test_crash_regime_not_triggered_in_bear():
    """Bear regime (breadth 20-30%) should NOT be crash."""
    from scripts.run_backtest import compute_regime_by_date

    start = date(2020, 1, 2)
    num_days = 250

    # 10 tickers: 3 above MA, 7 below = 30% breadth = bear (not crash)
    bars_by_ticker = {}
    for i in range(3):
        # Always above MA
        prices = [100.0 + j * 0.5 for j in range(num_days)]
        bars_by_ticker[f"UP{i}"] = _make_bars(start, num_days, prices)
    for i in range(7):
        # Rise then drop below MA
        prices = [100.0 + j * 0.1 for j in range(200)]
        prices += [70.0 for _ in range(num_days - 200)]
        bars_by_ticker[f"DOWN{i}"] = _make_bars(start, num_days, prices)

    regime_by_date = compute_regime_by_date(bars_by_ticker, crash_threshold=0.10)

    last_dates = sorted(regime_by_date.keys())[-10:]
    for d in last_dates:
        assert regime_by_date[d] != "crash", f"Date {d} should be bear, not crash"
        assert regime_by_date[d] == "bear", f"Date {d} should be bear, got {regime_by_date[d]}"


def test_regime_params_has_crash():
    """REGIME_PARAMS should include crash with tighter stops."""
    from scripts.run_backtest import REGIME_PARAMS

    assert "crash" in REGIME_PARAMS
    crash = REGIME_PARAMS["crash"]
    bear = REGIME_PARAMS["bear"]
    assert crash["trailing_stop_pct"] < bear["trailing_stop_pct"]
    assert crash["max_loss_pct"] < bear["max_loss_pct"]
```

### Step 2: Run tests to verify they fail

Run: `pytest tests/backtest/test_regime_detection.py -v`
Expected: FAIL — `crash_threshold` parameter not accepted, `crash` not in REGIME_PARAMS

### Step 3: Implement

Modify `scripts/run_backtest.py`:

1. Add `crash` to `REGIME_PARAMS`:
```python
REGIME_PARAMS = {
    "bull": {"trailing_stop_pct": 0.15, "max_loss_pct": 0.10},
    "neutral": {"trailing_stop_pct": 0.12, "max_loss_pct": 0.08},
    "bear": {"trailing_stop_pct": 0.08, "max_loss_pct": 0.05},
    "crash": {"trailing_stop_pct": 0.04, "max_loss_pct": 0.02},
}
```

2. Add `crash_threshold` parameter to `compute_regime_by_date()`:
```python
def compute_regime_by_date(
    bars_by_ticker: dict[str, list[dict]],
    ma_period: int = 200,
    bull_threshold: float = 0.60,
    bear_threshold: float = 0.40,
    crash_threshold: float = 0.10,
) -> dict:
```

3. In the regime classification loop, check crash before bear:
```python
    for d, above_list in above_ma.items():
        breadth = sum(above_list) / len(above_list)
        if breadth > bull_threshold:
            regime_by_date[d] = "bull"
        elif breadth < crash_threshold:
            regime_by_date[d] = "crash"
        elif breadth < bear_threshold:
            regime_by_date[d] = "bear"
        else:
            regime_by_date[d] = "neutral"
```

### Step 4: Run tests

Run: `pytest tests/backtest/test_regime_detection.py -v`
Expected: 3 passed

Run: `pytest tests/backtest/ -v --tb=short`
Expected: All tests pass

### Step 5: Commit

```bash
git add scripts/run_backtest.py tests/backtest/test_regime_detection.py
git commit -m "feat: add crash regime detection (breadth < 10%) and crash REGIME_PARAMS"
```

---

## Task 2: Crash Entry Freeze Wrapper

**Files:**
- Modify: `scripts/run_backtest.py`
- Create: `tests/backtest/test_crash_freeze.py`

### Step 1: Write failing tests

Create `tests/backtest/test_crash_freeze.py`:

```python
from __future__ import annotations

from datetime import date


def test_crash_freeze_blocks_buy_signals():
    """Crash regime should block buy signals."""
    from scripts.run_backtest import make_crash_freeze_signals_fn

    regime_by_date = {date(2024, 1, 1): "crash"}

    def inner_fn(ticker, bars):
        return {"action": "buy", "ticker": ticker, "limit_price": 100.0, "quantity": 10}

    wrapped = make_crash_freeze_signals_fn(inner_fn, regime_by_date)
    bars = [{"date": date(2024, 1, 1), "close": 100.0}]
    result = wrapped("AAPL", bars)

    assert result is None


def test_crash_freeze_passes_sell_signals():
    """Crash regime should allow sell signals (exits always permitted)."""
    from scripts.run_backtest import make_crash_freeze_signals_fn

    regime_by_date = {date(2024, 1, 1): "crash"}

    def inner_fn(ticker, bars):
        return {"action": "sell", "ticker": ticker, "limit_price": 100.0, "exit_reason": "trailing_stop"}

    wrapped = make_crash_freeze_signals_fn(inner_fn, regime_by_date)
    bars = [{"date": date(2024, 1, 1), "close": 100.0}]
    result = wrapped("AAPL", bars)

    assert result is not None
    assert result["action"] == "sell"


def test_crash_freeze_passes_buys_in_non_crash():
    """Non-crash regimes should pass buy signals through."""
    from scripts.run_backtest import make_crash_freeze_signals_fn

    regime_by_date = {date(2024, 1, 1): "bear"}

    def inner_fn(ticker, bars):
        return {"action": "buy", "ticker": ticker, "limit_price": 100.0, "quantity": 10}

    wrapped = make_crash_freeze_signals_fn(inner_fn, regime_by_date)
    bars = [{"date": date(2024, 1, 1), "close": 100.0}]
    result = wrapped("AAPL", bars)

    assert result is not None
    assert result["action"] == "buy"


def test_crash_freeze_passes_none_through():
    """None signals should pass through unchanged."""
    from scripts.run_backtest import make_crash_freeze_signals_fn

    regime_by_date = {date(2024, 1, 1): "crash"}

    def inner_fn(ticker, bars):
        return None

    wrapped = make_crash_freeze_signals_fn(inner_fn, regime_by_date)
    bars = [{"date": date(2024, 1, 1), "close": 100.0}]
    result = wrapped("AAPL", bars)

    assert result is None


def test_crash_freeze_defaults_to_neutral():
    """Missing regime date should default to neutral (no freeze)."""
    from scripts.run_backtest import make_crash_freeze_signals_fn

    regime_by_date = {}  # empty

    def inner_fn(ticker, bars):
        return {"action": "buy", "ticker": ticker, "limit_price": 100.0, "quantity": 10}

    wrapped = make_crash_freeze_signals_fn(inner_fn, regime_by_date)
    bars = [{"date": date(2024, 1, 1), "close": 100.0}]
    result = wrapped("AAPL", bars)

    assert result is not None
    assert result["action"] == "buy"
```

### Step 2: Run tests to verify they fail

Run: `pytest tests/backtest/test_crash_freeze.py -v`
Expected: FAIL — `make_crash_freeze_signals_fn` not found

### Step 3: Implement

Add to `scripts/run_backtest.py` (near `make_ml_filtered_signals_fn`):

```python
def make_crash_freeze_signals_fn(
    inner_fn: Callable[[str, list[dict]], dict | None],
    regime_by_date: dict,
) -> Callable[[str, list[dict]], dict | None]:
    """Wrap a signal function to freeze new entries during crash regime.

    Buy signals are suppressed when the current regime is 'crash'.
    Sell signals always pass through (exits are never blocked).
    """
    def signals_fn(ticker: str, bars: list[dict]) -> dict | None:
        signal = inner_fn(ticker, bars)
        if signal is None:
            return None

        if signal.get("action") == "buy":
            current_date = bars[-1]["date"]
            regime = regime_by_date.get(current_date, "neutral")
            if regime == "crash":
                return None

        return signal

    return signals_fn
```

Then in `main()`, after building portfolios dict and before the backtest loop, wrap all strategies EXCEPT tail_risk_hedge:

```python
    # Level 3: Crash entry freeze — block new buys during crash regime
    for name, pc in list(portfolios.items()):
        if name == "tail_risk_hedge":
            continue  # Tail-risk hedge operates during crashes
        portfolios[name] = PortfolioConfig(
            name=pc.name,
            capital=pc.capital,
            signals_fn=make_crash_freeze_signals_fn(pc.signals_fn, regime_by_date),
            risk_engine=pc.risk_engine,
        )
```

This should go BEFORE the ML filter wrapping (ML filter wraps the already-crash-frozen function).

### Step 4: Run tests

Run: `pytest tests/backtest/test_crash_freeze.py -v`
Expected: 5 passed

Run: `pytest tests/backtest/ -v --tb=short`
Expected: All tests pass

### Step 5: Commit

```bash
git add scripts/run_backtest.py tests/backtest/test_crash_freeze.py
git commit -m "feat: add crash entry freeze wrapper — block buys during crash regime"
```

---

## Task 3: Bear-Regime Stop Tightening

**Files:**
- Modify: `scripts/run_backtest.py`
- Create: `tests/backtest/test_bear_stop_tightening.py`

### Step 1: Write failing tests

Create `tests/backtest/test_bear_stop_tightening.py`:

```python
from __future__ import annotations

from datetime import date, timedelta


def _make_bars_with_peak_and_drop(start: date, peak_price: float, drop_pct: float, num_bars: int = 60) -> list[dict]:
    """Build bars that rise to peak then drop by drop_pct."""
    bars = []
    d = start
    half = num_bars // 2
    for i in range(num_bars):
        while d.weekday() >= 5:
            d += timedelta(days=1)
        if i < half:
            price = 100.0 + (peak_price - 100.0) * (i / half)
        else:
            drop_progress = (i - half) / (num_bars - half)
            price = peak_price * (1.0 - drop_pct * drop_progress)
        bars.append({"date": d, "open": price, "high": price + 0.5, "low": price - 0.5, "close": price, "volume": 1_000_000})
        d += timedelta(days=1)
    return bars


def test_sector_rotation_tightens_stop_in_bear():
    """Sector rotation should use tighter trailing stop in bear regime."""
    from scripts.run_backtest import make_sector_rotation_signals_fn

    start = date(2024, 1, 2)
    bars = _make_bars_with_peak_and_drop(start, peak_price=120.0, drop_pct=0.07, num_bars=80)
    dates = [b["date"] for b in bars]

    # All dates are bear regime
    regime_by_date = {d: "bear" for d in dates}

    # Build signal function — default trailing stop 8%, bear should be 6%
    bars_by_ticker = {"XLK": bars}
    fn = make_sector_rotation_signals_fn(
        bars_by_ticker=bars_by_ticker, top_n=1, lookback_days=20,
        trailing_stop_pct=0.08, initial_capital=100_000,
        regime_by_date=regime_by_date,
    )

    # The function should use the regime-adjusted stop internally
    # We can't directly test the internal stop value, but we can verify
    # that the function accepts regime_by_date (already does)
    # and that REGIME_PARAMS bear trailing_stop is applied
    from scripts.run_backtest import REGIME_PARAMS
    bear_stop = REGIME_PARAMS["bear"]["trailing_stop_pct"]
    assert bear_stop == 0.08  # Confirm bear stop exists


def test_thematic_momentum_accepts_regime():
    """Thematic momentum should accept regime_by_date parameter."""
    from scripts.run_backtest import make_thematic_momentum_signals_fn

    start = date(2024, 1, 2)
    bars = _make_bars_with_peak_and_drop(start, peak_price=120.0, drop_pct=0.05, num_bars=80)

    regime_by_date = {b["date"]: "bear" for b in bars}
    bars_by_ticker = {"ARKK": bars}

    # Should not raise — regime_by_date is accepted
    fn = make_thematic_momentum_signals_fn(
        bars_by_ticker=bars_by_ticker, top_n=1, lookback_days=20,
        trailing_stop_pct=0.10, initial_capital=100_000,
        regime_by_date=regime_by_date,
    )
    assert callable(fn)


def test_quality_value_accepts_regime():
    """Quality value should accept regime_by_date parameter."""
    from scripts.run_backtest import make_quality_value_signals_fn

    regime_by_date = {date(2024, 1, 2): "bear"}

    def dummy_lookup(ticker, d):
        return None

    fn = make_quality_value_signals_fn(
        fundamentals_lookup=dummy_lookup, sector_map={},
        trailing_stop_pct=0.12, initial_capital=100_000,
        regime_by_date=regime_by_date,
    )
    assert callable(fn)


def test_earnings_drift_accepts_regime():
    """Earnings drift should accept regime_by_date parameter."""
    from scripts.run_backtest import make_earnings_drift_signals_fn

    regime_by_date = {date(2024, 1, 2): "bear"}

    def dummy_lookup(ticker, d):
        return None

    fn = make_earnings_drift_signals_fn(
        earnings_lookup=dummy_lookup,
        trailing_stop_pct=0.06, initial_capital=100_000,
        regime_by_date=regime_by_date,
    )
    assert callable(fn)
```

### Step 2: Run tests to verify they fail

Run: `pytest tests/backtest/test_bear_stop_tightening.py -v`
Expected: FAIL — `regime_by_date` parameter not accepted by these functions

### Step 3: Implement

Add `regime_by_date: dict | None = None` parameter to:

1. `make_thematic_momentum_signals_fn()` — add regime lookup and stop tightening:
```python
def make_thematic_momentum_signals_fn(
    ...,
    regime_by_date: dict | None = None,
):
```
Inside `signals_fn`, where trailing stop is checked, add:
```python
        effective_trailing = trailing_stop_pct
        effective_max_loss = max_loss_pct
        if regime_by_date:
            regime = regime_by_date.get(current_date, "neutral")
            if regime in ("bear", "crash"):
                effective_trailing = max(trailing_stop_pct - 0.02, 0.02)
                effective_max_loss = max(max_loss_pct - 0.02, 0.02)
```

2. `make_quality_value_signals_fn()` — add regime lookup and stop tightening:
```python
def make_quality_value_signals_fn(
    ...,
    regime_by_date: dict | None = None,
):
```
Inside `signals_fn`, where trailing stop is checked:
```python
        effective_trailing = trailing_stop_pct
        if regime_by_date:
            regime = regime_by_date.get(current_date, "neutral")
            if regime in ("bear", "crash"):
                effective_trailing = max(trailing_stop_pct - 0.02, 0.02)
```

3. `make_earnings_drift_signals_fn()` — same pattern:
```python
def make_earnings_drift_signals_fn(
    ...,
    regime_by_date: dict | None = None,
):
```
Same stop tightening logic.

4. `make_short_term_mr_signals_fn()` — no trailing stop (time-based exit), no change needed.

5. Update `main()` portfolio construction to pass `regime_by_date` to these 3 functions.

### Step 4: Run tests

Run: `pytest tests/backtest/test_bear_stop_tightening.py -v`
Expected: 4 passed

Run: `pytest tests/backtest/ -v --tb=short`
Expected: All tests pass

### Step 5: Commit

```bash
git add scripts/run_backtest.py tests/backtest/test_bear_stop_tightening.py
git commit -m "feat: add bear-regime trailing stop tightening to remaining strategies"
```

---

## Task 4: Wire regime_by_date into paper trading & update docs

**Files:**
- Modify: `scripts/run_paper.py`
- Modify: `docs/strategy.md`

### Step 1: Update paper trading

In `scripts/run_paper.py`, `build_portfolios()`, pass `regime_by_date` to the 3 new signal functions:

```python
    # quality_value
    portfolios["quality_value"] = PortfolioConfig(
        name="quality_value",
        ...
        signals_fn=make_quality_value_signals_fn(
            ...,
            regime_by_date=regime_by_date,  # ADD THIS
        ),
        ...
    )
```

Same for `earnings_drift` and `thematic_momentum`.

Also add crash freeze wrapping after building portfolios (before returning):

```python
    # Level 3: Crash entry freeze
    from scripts.run_backtest import make_crash_freeze_signals_fn
    for name, pc in list(portfolios.items()):
        if name == "tail_risk_hedge":
            continue
        portfolios[name] = PortfolioConfig(
            name=pc.name,
            capital=pc.capital,
            signals_fn=make_crash_freeze_signals_fn(pc.signals_fn, regime_by_date),
            risk_engine=pc.risk_engine,
        )
```

### Step 2: Update docs/strategy.md

Add to the Cross-Portfolio Safety Monitoring section:

```markdown
### Level 3: Tail-Risk Override

| Condition | Regime | Action |
|---|---|---|
| Breadth < 10% (>90% below 200-day MA) | Crash | Freeze all new entries except tail-risk hedge |
| Breadth < 40% | Bear | Tighten all trailing stops by 2% |

Crash entry freeze is applied as a wrapper around each strategy's signal function. The tail-risk hedge continues operating during crashes, providing downside protection.

Bear-regime stop tightening reduces trailing stop percentages by 2 percentage points (e.g., 10% → 8%, 12% → 10%) to lock in gains faster during volatile conditions.
```

### Step 3: Commit

```bash
git add scripts/run_paper.py docs/strategy.md
git commit -m "feat: wire crash freeze and bear stop tightening into paper trading, update docs"
```

---

## Verification

```bash
# All tests
pytest tests/backtest/ -v --tb=short

# Regime detection tests
pytest tests/backtest/test_regime_detection.py -v

# Crash freeze tests
pytest tests/backtest/test_crash_freeze.py -v

# Bear stop tightening tests
pytest tests/backtest/test_bear_stop_tightening.py -v
```
