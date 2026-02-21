# Momentum Strategy Design — Dual Strategy (Mean-Reversion + Relative Strength)

**Date:** 2026-02-21
**Goal:** Add momentum plays alongside the existing mean-reversion strategy to capture sustained uptrends that the current strategy misses.

## Context

The mean-reversion strategy is profitable over 10 years (+7.71%, 18.65% max DD, 80 trades) but has dead years (2019, 2023) where stocks trend up without pulling back to support. Adding momentum captures these sustained trends.

## Strategy Summary

| | Mean-Reversion | Momentum |
|---|---|---|
| Entry | Support + RSI < 30 + Volume 2x + Rising supports | Top 5 by 6-month relative strength |
| Exit | 10% trailing stop (after profitable) | 10% trailing stop (after profitable) |
| Frequency | ~8 trades/year (event-driven) | ~10-20 trades/year (rotation) |
| Position size | 7% NAV | 7% NAV |
| Pyramiding | Up to 2 lots | Up to 2 lots |

## Architecture: Separate Composed Functions (Option B)

### Momentum Signal Function

`make_momentum_signals_fn(bars_by_ticker, ...)` in `scripts/run_backtest.py`:

- **Ranking**: On each bar date, compute 6-month (126-day) return for every ticker with enough history. Rank them. Tag the top 5 as "momentum eligible."
- **Entry**: If a ticker is in the top 5 and not already tracked by the momentum function, generate a buy signal. Position size: 7% of NAV. Limit price: current price (market buy).
- **Exit**: 10% trailing stop from peak. No hard stop, no time cap. Trailing stop only activates after position is profitable.
- **State tracking**: Internal `tracked` dict with `{entry_price, peak_price}` per lot.
- **Re-ranking**: Rankings are recomputed every bar (daily), but new entries only happen when a stock freshly enters the top 5 and isn't already held.

### Composition Wrapper

`make_combined_signals_fn(mean_reversion_fn, momentum_fn)`:

- Calls mean-reversion first (more selective), then momentum.
- If mean-reversion produces a signal, use it. Otherwise check momentum.
- Sell signals take priority — if either strategy says sell, sell.
- Tags each signal with `"strategy": "mean_reversion"` or `"strategy": "momentum"` for analysis.
- The momentum function receives the full `bars_by_ticker` dict at creation time and pre-computes rankings by date. When called per-ticker, it looks up whether that ticker is currently top-5.

### Risk & Position Management

- Shared risk engine with existing limits (7% per position, 30% sector, 100% total exposure, max 2 lots per ticker).
- Dynamic allocation — no fixed capital split. Whichever strategy signals first gets capital.
- Position isolation: each strategy tracks its own positions via internal `tracked` dicts.
- Max 2 lots per ticker applies across both strategies combined.

## Implementation Scope

**Modify:** `scripts/run_backtest.py` — add `make_momentum_signals_fn()`, `make_combined_signals_fn()`, update `main()`.

**No changes to:** signal framework, risk engine, backtest runner, visualization.

**New test:** `tests/backtest/test_momentum_signals.py`
