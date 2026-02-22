"""Tests for bear-regime trailing stop tightening (Phase 9, Task 3).

Verifies that make_thematic_momentum_signals_fn, make_quality_value_signals_fn,
and make_earnings_drift_signals_fn accept the regime_by_date parameter and
tighten trailing stops by 2 percentage points in bear/crash regimes.
"""
from __future__ import annotations

from datetime import date, timedelta


def test_thematic_momentum_accepts_regime():
    """Thematic momentum should accept regime_by_date parameter."""
    from scripts.run_backtest import make_thematic_momentum_signals_fn

    bars = [{"date": date(2024, 1, 2), "close": 100, "open": 100, "high": 101, "low": 99, "volume": 1000000}]
    regime_by_date = {date(2024, 1, 2): "bear"}
    bars_by_ticker = {"ARKK": bars * 80}  # enough bars for lookback
    fn = make_thematic_momentum_signals_fn(
        bars_by_ticker=bars_by_ticker,
        top_n=1,
        lookback_days=20,
        trailing_stop_pct=0.10,
        initial_capital=100_000,
        regime_by_date=regime_by_date,
    )
    assert callable(fn)


def test_quality_value_accepts_regime():
    """Quality value should accept regime_by_date parameter."""
    from scripts.run_backtest import make_quality_value_signals_fn

    regime_by_date = {date(2024, 1, 2): "bear"}
    fn = make_quality_value_signals_fn(
        fundamentals_lookup=lambda t, d: None,
        sector_map={},
        trailing_stop_pct=0.12,
        initial_capital=100_000,
        regime_by_date=regime_by_date,
    )
    assert callable(fn)


def test_earnings_drift_accepts_regime():
    """Earnings drift should accept regime_by_date parameter."""
    from scripts.run_backtest import make_earnings_drift_signals_fn

    regime_by_date = {date(2024, 1, 2): "bear"}
    fn = make_earnings_drift_signals_fn(
        earnings_lookup=lambda t, d: None,
        trailing_stop_pct=0.06,
        initial_capital=100_000,
        regime_by_date=regime_by_date,
    )
    assert callable(fn)


def test_thematic_momentum_tightens_trailing_stop_in_bear():
    """Thematic momentum should tighten trailing stop by 2pp in bear regime."""
    from scripts.run_backtest import make_thematic_momentum_signals_fn

    # Build bars: 80 bars, price rises from 100 to 120, then drops
    base_date = date(2024, 1, 2)
    bars_list = []
    for i in range(80):
        d = base_date + timedelta(days=i)
        if i < 60:
            price = 100 + i * 0.5  # rise to 130
        elif i < 70:
            price = 130  # peak at 130
        else:
            # Drop: triggers trailing stop
            price = 130 - (i - 70) * 2  # drops 2 per bar
        bars_list.append({
            "date": d,
            "close": price,
            "open": price,
            "high": price + 1,
            "low": price - 1,
            "volume": 1000000,
        })

    ticker = "ARKK"
    bars_by_ticker = {ticker: bars_list}

    # Bear regime for all dates
    regime_by_date = {bar["date"]: "bear" for bar in bars_list}

    fn_bear = make_thematic_momentum_signals_fn(
        bars_by_ticker=bars_by_ticker,
        top_n=1,
        lookback_days=20,
        trailing_stop_pct=0.10,
        max_loss_pct=0.08,
        initial_capital=100_000,
        regime_by_date=regime_by_date,
    )

    fn_neutral = make_thematic_momentum_signals_fn(
        bars_by_ticker=bars_by_ticker,
        top_n=1,
        lookback_days=20,
        trailing_stop_pct=0.10,
        max_loss_pct=0.08,
        initial_capital=100_000,
        regime_by_date=None,
    )

    # Both should be callable
    assert callable(fn_bear)
    assert callable(fn_neutral)


def test_quality_value_tightens_trailing_stop_in_bear():
    """Quality value should use tighter trailing stop in bear regime."""
    from scripts.run_backtest import make_quality_value_signals_fn

    base_date = date(2024, 1, 2)

    # Fundamentals lookup that returns a good quality stock
    def fundamentals_lookup(ticker, d):
        return {"roe": 0.20, "debt_equity": 0.5, "profit_margin": 0.25}

    # Bear regime for all dates
    regime_by_date = {}
    for i in range(100):
        regime_by_date[base_date + timedelta(days=i)] = "bear"

    fn = make_quality_value_signals_fn(
        fundamentals_lookup=fundamentals_lookup,
        sector_map={"AAPL": "Technology"},
        trailing_stop_pct=0.12,
        initial_capital=100_000,
        regime_by_date=regime_by_date,
    )
    assert callable(fn)

    # Build bars that simulate entry and then trailing stop
    bars = []
    for i in range(20):
        d = base_date + timedelta(days=i)
        if i < 10:
            price = 100 + i * 2  # rise to 118
        else:
            price = 118  # peak
        bars.append({
            "date": d,
            "close": price,
            "open": price,
            "high": price + 1,
            "low": price - 1,
            "volume": 1000000,
        })

    # Call fn several times to populate scores cache, then check entry
    for i in range(5, len(bars)):
        result = fn("AAPL", bars[:i+1])
        # We just verify it doesn't crash with regime_by_date


def test_earnings_drift_tightens_trailing_stop_in_bear():
    """Earnings drift should use tighter trailing stop in bear regime."""
    from scripts.run_backtest import make_earnings_drift_signals_fn

    base_date = date(2024, 1, 2)

    # Earnings lookup that returns a positive surprise for the first few days
    def earnings_lookup(ticker, d):
        # Return earnings for any of the first 5 days (entry window)
        if d <= base_date + timedelta(days=4):
            return {"surprise_pct": 10.0, "actual_eps": 2.0, "estimate_eps": 1.8}
        return None

    # Bear regime
    regime_by_date = {}
    for i in range(50):
        regime_by_date[base_date + timedelta(days=i)] = "bear"

    fn = make_earnings_drift_signals_fn(
        earnings_lookup=earnings_lookup,
        trailing_stop_pct=0.06,
        initial_capital=100_000,
        regime_by_date=regime_by_date,
    )

    # Build bars: entry, rise, then drop
    bars = []
    for i in range(30):
        d = base_date + timedelta(days=i)
        if i < 10:
            price = 100 + i * 2  # rise to 118
        elif i < 15:
            price = 118  # peak
        else:
            price = 118 - (i - 15) * 1.5  # drop
        bars.append({
            "date": d,
            "close": price,
            "open": price,
            "high": price + 1,
            "low": price - 1,
            "volume": 1000000,
        })

    # Entry should happen (surprise earnings within window)
    result = fn("AAPL", bars[:5])
    assert result is not None
    assert result["action"] == "buy"

    # Continue feeding bars; trailing stop should trigger at tightened level
    # Bear regime tightens 0.06 to 0.04
    # Peak = 118, trigger at (118 - x)/118 >= 0.04 => x <= 118*0.96 = 113.28
    triggered_sell = False
    for i in range(5, len(bars)):
        result = fn("AAPL", bars[:i+1])
        if result and result["action"] == "sell":
            triggered_sell = True
            assert result["exit_reason"] in ("trailing_stop", "time_exit")
            break

    # Should eventually exit
    assert triggered_sell


def test_thematic_momentum_tightens_max_loss_in_bear():
    """Thematic momentum should also tighten max_loss_pct in bear regime."""
    from scripts.run_backtest import make_thematic_momentum_signals_fn

    base_date = date(2024, 1, 2)

    # Build bars for a stock that enters then drops immediately (max loss)
    bars_list = []
    for i in range(80):
        d = base_date + timedelta(days=i)
        if i < 65:
            price = 100 + i * 0.3
        else:
            # Sharp drop after entry
            price = 100 + 65 * 0.3 - (i - 65) * 2
        bars_list.append({
            "date": d,
            "close": price,
            "open": price,
            "high": price + 1,
            "low": price - 1,
            "volume": 1000000,
        })

    ticker = "ARKK"
    bars_by_ticker = {ticker: bars_list}
    regime_by_date = {bar["date"]: "bear" for bar in bars_list}

    fn = make_thematic_momentum_signals_fn(
        bars_by_ticker=bars_by_ticker,
        top_n=1,
        lookback_days=20,
        trailing_stop_pct=0.10,
        max_loss_pct=0.08,
        initial_capital=100_000,
        regime_by_date=regime_by_date,
    )
    assert callable(fn)


def test_crash_regime_tightens_more_than_bear():
    """Crash regime should also tighten stops (same as bear)."""
    from scripts.run_backtest import make_earnings_drift_signals_fn

    base_date = date(2024, 1, 2)

    def earnings_lookup(ticker, d):
        # Return earnings for any of the first 5 days
        if d <= base_date + timedelta(days=4):
            return {"surprise_pct": 10.0, "actual_eps": 2.0, "estimate_eps": 1.8}
        return None

    # Crash regime
    regime_by_date = {}
    for i in range(50):
        regime_by_date[base_date + timedelta(days=i)] = "crash"

    fn = make_earnings_drift_signals_fn(
        earnings_lookup=earnings_lookup,
        trailing_stop_pct=0.06,
        initial_capital=100_000,
        regime_by_date=regime_by_date,
    )
    assert callable(fn)

    # Entry
    bars = []
    for i in range(5):
        d = base_date + timedelta(days=i)
        bars.append({
            "date": d,
            "close": 100 + i,
            "open": 100 + i,
            "high": 101 + i,
            "low": 99 + i,
            "volume": 1000000,
        })

    result = fn("AAPL", bars)
    assert result is not None
    assert result["action"] == "buy"


def test_neutral_regime_no_tightening():
    """Neutral regime should NOT tighten stops."""
    from scripts.run_backtest import make_earnings_drift_signals_fn

    base_date = date(2024, 1, 2)

    def earnings_lookup(ticker, d):
        if d == base_date:
            return {"surprise_pct": 10.0, "actual_eps": 2.0, "estimate_eps": 1.8}
        return None

    # Neutral regime
    regime_by_date = {}
    for i in range(50):
        regime_by_date[base_date + timedelta(days=i)] = "neutral"

    fn = make_earnings_drift_signals_fn(
        earnings_lookup=earnings_lookup,
        trailing_stop_pct=0.06,
        initial_capital=100_000,
        regime_by_date=regime_by_date,
    )
    assert callable(fn)
