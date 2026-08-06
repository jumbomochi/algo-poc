# T5 — Backtest realism & divergence rebaseline  [P1]

Part of the 2026-08-06 implementation review (`docs/operations/implementation-review-2026-08-06.md`, Theme 4 + § 9). Tracking issue linked via this PR's "Closes #…".

## Problem
The backtest that justifies the strategy is optimistic by construction (survivorship + same-bar look-ahead), and the divergence monitor baselines against it — so it cannot distinguish "backtest was never achievable" from "live is degrading." The newer `research/` framework already does this correctly; route through it.

## Checklist
- [ ] **Point-in-time universe** — replace the static `SP500_TOP50` ("as of early 2025") with membership-by-date (the `research/` panel already supports it); include delisted names. `universe.py:13-21`, `run_backtest.py`
- [ ] **Next-bar fills** — a decision on `close[t]` fills at `open[t+1]` for both entries and exits (today entries fill same-bar at close, exits same-bar at open). `simulator.py:31,45-68`, `backtest/runner.py:98-117`
- [ ] **Filing-lagged fundamentals** — key availability off filing date, not fiscal period-end (this also fixes a **live** leak in paper trading). `fetch_fundamentals.py:82-88,118-119`, `run_paper.py:652`
- [ ] **Purge/embargo the holding period** in the ML walk-forward; never apply a model in-sample. `train_signal_model.py:37-114,117-141`
- [ ] **Cost realism** — per-order commission floor + per-instrument slippage; use `ddof=1` Sharpe. `run_backtest.py:1948-1951`, `metrics.py:78-89`
- [ ] **Rebaseline the divergence monitor** against a same-execution-model backtest (next-open fills, real costs). `backtest/divergence.py:228-302`

## Acceptance criteria
- Headline backtest re-run on a point-in-time universe with next-bar fills; the new numbers become the baseline.
- Divergence monitor compares live against a like-for-like execution model.
- No fundamentals lookup returns data before its filing date (live or backtest).

## Dependencies
- Independent, but its output feeds the scale-up decision — highest-leverage P1.
