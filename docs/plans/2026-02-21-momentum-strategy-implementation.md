# Momentum Strategy Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a relative-strength momentum strategy alongside the existing mean-reversion strategy so the backtest captures both dip-buys and sustained uptrends.

**Architecture:** Create `make_momentum_signals_fn()` that ranks tickers by 6-month return and buys the top 5. Compose it with the existing mean-reversion function via `make_combined_signals_fn()`. Both share the same risk engine and trailing stop exit logic.

**Tech Stack:** Python, numpy, existing backtest framework. No new dependencies.

---

### Task 1: Add make_momentum_signals_fn()

**Files:**
- Modify: `scripts/run_backtest.py:275` (insert after `make_signals_fn`)
- Test: `tests/backtest/test_momentum_signals.py`

**Step 1: Write the failing test**

Create `tests/backtest/test_momentum_signals.py`:

```python
from __future__ import annotations

from datetime import date, timedelta

from scripts.run_backtest import make_momentum_signals_fn


def _make_bars(ticker: str, days: int, base_price: float, daily_return: float):
    """Generate synthetic bars with a steady return."""
    bars = []
    price = base_price
    for i in range(days):
        d = date(2024, 1, 2) + timedelta(days=i)
        bars.append({
            "date": d,
            "open": price,
            "high": price * 1.01,
            "low": price * 0.99,
            "close": price,
            "volume": 500_000,
        })
        price *= (1 + daily_return)
    return bars


def test_momentum_buys_top_ranked_ticker():
    """Momentum should generate a buy for the strongest performer."""
    # WINNER has +50% over 6 months, LOSER has -10%
    bars_by_ticker = {
        "WINNER": _make_bars("WINNER", 200, 100.0, 0.003),  # ~+80% over 200 days
        "LOSER": _make_bars("LOSER", 200, 100.0, -0.001),   # ~-18% over 200 days
    }
    fn = make_momentum_signals_fn(
        bars_by_ticker=bars_by_ticker,
        top_n=1,
        lookback_days=126,
        position_size_pct=0.07,
        initial_capital=100_000,
        trailing_stop_pct=0.10,
    )
    # Feed enough bars for ranking (>126)
    winner_bars = bars_by_ticker["WINNER"]
    signal = fn("WINNER", winner_bars[:150])
    assert signal is not None
    assert signal["action"] == "buy"
    assert signal["signals"]["strategy"] == "momentum"

    # LOSER should NOT get a buy
    loser_bars = bars_by_ticker["LOSER"]
    signal = fn("LOSER", loser_bars[:150])
    assert signal is None


def test_momentum_requires_min_bars():
    """Momentum should return None with insufficient history."""
    bars_by_ticker = {
        "SHORT": _make_bars("SHORT", 50, 100.0, 0.003),
    }
    fn = make_momentum_signals_fn(
        bars_by_ticker=bars_by_ticker,
        top_n=1,
        lookback_days=126,
    )
    signal = fn("SHORT", bars_by_ticker["SHORT"])
    assert signal is None


def test_momentum_trailing_stop_exits():
    """Momentum should sell when trailing stop triggers after profit."""
    bars_by_ticker = {
        "TEST": _make_bars("TEST", 200, 100.0, 0.003),
    }
    fn = make_momentum_signals_fn(
        bars_by_ticker=bars_by_ticker,
        top_n=1,
        lookback_days=126,
        trailing_stop_pct=0.05,
    )
    # Enter at bar 150
    bars = bars_by_ticker["TEST"]
    signal = fn("TEST", bars[:150])
    assert signal is not None and signal["action"] == "buy"

    # Price keeps rising — update peak (no sell)
    signal = fn("TEST", bars[:170])
    assert signal is None or signal["action"] != "sell"

    # Now simulate a 6% drop from peak
    peak_price = bars[169]["close"]
    drop_bars = list(bars[:170])
    for i in range(5):
        d = bars[169]["date"] + timedelta(days=i + 1)
        drop_price = peak_price * (0.93 + i * 0.001)  # stays below 95% of peak
        drop_bars.append({
            "date": d, "open": drop_price, "high": drop_price,
            "low": drop_price, "close": drop_price, "volume": 500_000,
        })
    signal = fn("TEST", drop_bars)
    assert signal is not None
    assert signal["action"] == "sell"
    assert signal["exit_reason"] == "trailing_stop"
```

**Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/backtest/test_momentum_signals.py::test_momentum_buys_top_ranked_ticker -v`
Expected: FAIL — ImportError, `make_momentum_signals_fn` not found

**Step 3: Implement make_momentum_signals_fn**

Insert after `make_signals_fn` (line 275) in `scripts/run_backtest.py`:

```python
def make_momentum_signals_fn(
    bars_by_ticker: dict[str, list[dict]],
    top_n: int = 5,
    lookback_days: int = 126,
    position_size_pct: float = 0.07,
    initial_capital: float = 100_000,
    trailing_stop_pct: float = 0.10,
    max_lots: int = 2,
):
    """Create a momentum signal function based on 6-month relative strength.

    Ranks all tickers by their return over the lookback period.
    Buys the top N performers. Exits via trailing stop.
    """
    # Pre-compute date -> {ticker: close_price} for ranking
    price_by_date: dict[Any, dict[str, float]] = {}
    for ticker, bars in bars_by_ticker.items():
        for bar in bars:
            d = bar["date"]
            if d not in price_by_date:
                price_by_date[d] = {}
            price_by_date[d][ticker] = bar["close"]

    sorted_dates = sorted(price_by_date.keys())

    # Pre-compute date -> list of (ticker, return) ranked by return descending
    rankings_by_date: dict[Any, list[str]] = {}
    date_to_idx = {d: i for i, d in enumerate(sorted_dates)}
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

    # Per-ticker lot tracking for exits
    tracked: dict[str, list[dict]] = {}

    def signals_fn(ticker: str, bars: list[dict]) -> dict | None:
        if len(bars) < lookback_days + 1:
            return None

        current_bar = bars[-1]
        current_price = current_bar["close"]
        current_date = current_bar["date"]
        bar_count = len(bars)
        lots = tracked.get(ticker, [])

        # === Exit logic: trailing stop (same as mean-reversion) ===
        if lots:
            for lot in lots:
                lot["peak_price"] = max(lot["peak_price"], current_price)

            should_sell = False
            for lot in lots:
                peak = lot["peak_price"]
                entry = lot["entry_price"]
                if peak > entry and (peak - current_price) / peak >= trailing_stop_pct:
                    should_sell = True
                    break

            if should_sell:
                tracked.pop(ticker, None)
                return {
                    "action": "sell",
                    "ticker": ticker,
                    "limit_price": current_price,
                    "quantity": 0,
                    "sector": "Unknown",
                    "exit_reason": "trailing_stop",
                }

        # === Entry logic: buy if in top N and not already tracked ===
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
                    "strategy": "momentum",
                    "rank": top_tickers.index(ticker) + 1,
                    "lookback_days": lookback_days,
                },
            }

        return None

    return signals_fn
```

**Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/backtest/test_momentum_signals.py -v`
Expected: All 3 pass

**Step 5: Commit**

```bash
git add scripts/run_backtest.py tests/backtest/test_momentum_signals.py
git commit -m "feat: add momentum signal function with relative strength ranking"
```

---

### Task 2: Add make_combined_signals_fn() and update main()

**Files:**
- Modify: `scripts/run_backtest.py` (insert after `make_momentum_signals_fn`, update `main()` at line 426)
- Test: `tests/backtest/test_combined_signals.py`

**Step 1: Write the failing test**

Create `tests/backtest/test_combined_signals.py`:

```python
from __future__ import annotations


def test_combined_prefers_mean_reversion():
    """When mean-reversion produces a signal, it takes priority."""
    from scripts.run_backtest import make_combined_signals_fn

    def mr_fn(ticker, bars):
        return {"action": "buy", "ticker": ticker, "signals": {"strategy": "mean_reversion"}}

    def mom_fn(ticker, bars):
        return {"action": "buy", "ticker": ticker, "signals": {"strategy": "momentum"}}

    combined = make_combined_signals_fn(mr_fn, mom_fn)
    signal = combined("AAPL", [{"close": 150}])
    assert signal["signals"]["strategy"] == "mean_reversion"


def test_combined_falls_through_to_momentum():
    """When mean-reversion returns None, momentum is checked."""
    from scripts.run_backtest import make_combined_signals_fn

    def mr_fn(ticker, bars):
        return None

    def mom_fn(ticker, bars):
        return {"action": "buy", "ticker": ticker, "signals": {"strategy": "momentum"}}

    combined = make_combined_signals_fn(mr_fn, mom_fn)
    signal = combined("AAPL", [{"close": 150}])
    assert signal is not None
    assert signal["signals"]["strategy"] == "momentum"


def test_combined_sell_takes_priority():
    """If one strategy says sell and the other says buy, sell wins."""
    from scripts.run_backtest import make_combined_signals_fn

    def mr_fn(ticker, bars):
        return {"action": "sell", "ticker": ticker, "exit_reason": "trailing_stop"}

    def mom_fn(ticker, bars):
        return {"action": "buy", "ticker": ticker, "signals": {"strategy": "momentum"}}

    combined = make_combined_signals_fn(mr_fn, mom_fn)
    signal = combined("AAPL", [{"close": 150}])
    assert signal["action"] == "sell"
```

**Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/backtest/test_combined_signals.py -v`
Expected: FAIL — ImportError

**Step 3: Implement make_combined_signals_fn and update main()**

Insert after `make_momentum_signals_fn` in `scripts/run_backtest.py`:

```python
def make_combined_signals_fn(
    mean_reversion_fn: Callable[[str, list[dict]], dict | None],
    momentum_fn: Callable[[str, list[dict]], dict | None],
) -> Callable[[str, list[dict]], dict | None]:
    """Compose mean-reversion and momentum signal functions.

    Priority: sell signals first, then mean-reversion buys, then momentum buys.
    """
    def combined_fn(ticker: str, bars: list[dict]) -> dict | None:
        mr_signal = mean_reversion_fn(ticker, bars)
        mom_signal = momentum_fn(ticker, bars)

        # Sell signals take highest priority from either strategy
        if mr_signal and mr_signal.get("action") == "sell":
            return mr_signal
        if mom_signal and mom_signal.get("action") == "sell":
            return mom_signal

        # Buy: mean-reversion first (more selective), then momentum
        if mr_signal and mr_signal.get("action") == "buy":
            return mr_signal
        if mom_signal and mom_signal.get("action") == "buy":
            return mom_signal

        return None

    return combined_fn
```

Update `main()` at line 426. Replace:

```python
    signals_fn = make_signals_fn(initial_capital=args.capital)
```

With:

```python
    mr_signals_fn = make_signals_fn(initial_capital=args.capital)
    mom_signals_fn = make_momentum_signals_fn(
        bars_by_ticker=bars_by_ticker,
        top_n=5,
        lookback_days=126,
        initial_capital=args.capital,
    )
    signals_fn = make_combined_signals_fn(mr_signals_fn, mom_signals_fn)
```

**Step 4: Run all tests**

Run: `.venv/bin/pytest tests/backtest/ -v`
Expected: All pass (new + existing)

**Step 5: Commit**

```bash
git add scripts/run_backtest.py tests/backtest/test_combined_signals.py
git commit -m "feat: add combined signal function composing mean-reversion and momentum"
```

---

### Task 3: Run backtest and generate report

Integration test with IB Gateway.

**Step 1: Run 5-year backtest**

Run: `.venv/bin/python scripts/run_backtest.py --tickers 50 --years 5 --output-dir output`
Expected: Completes. Check that trades include both `strategy: mean_reversion` and `strategy: momentum` signals.

**Step 2: Generate HTML report**

Run: `.venv/bin/python scripts/visualize_backtest.py output/backtest_*.json` (use latest file)
Expected: HTML report shows both strategies' trades.

**Step 3: Run 10-year backtest**

Run: `.venv/bin/python scripts/run_backtest.py --tickers 50 --years 10 --output-dir output`

**Step 4: Generate HTML report for 10-year**

Run: `.venv/bin/python scripts/visualize_backtest.py output/backtest_*.json` (use latest file)

**Step 5: Commit**

```bash
git add scripts/run_backtest.py
git commit -m "chore: validate dual strategy backtest pipeline"
```
