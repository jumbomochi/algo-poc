# Split artefacts in IB's daily history (KAN-120, 2026-10-17)

KAN-119's scan for daily moves above 40% found two that were not market events.
Both were split artefacts in the bars IB served. This record confirms them,
shows there are no others in the ten-year window, and repairs them for the
backtest only (`backtest/bar_corrections.py`).

## Result

Rerun from the bars of `output/backtest_multi_20261013_114528.json`, with and
without the repairs. Same code, same caches; only the two series differ.

| Sleeve | Sharpe | Total return | Max drawdown |
|---|---|---|---|
| momentum | 0.587 → **0.570** | 162.9% → 157.7% | 24.8% → 25.6% |
| aggregate | 0.690 → 0.682 | 82.1% → 80.9% | 12.9% → 13.0% |

No other sleeve changes. Momentum loses 54 trades and gains others through path
dependence. **The artefacts flattered momentum**, and the D10 verdict does not
move (momentum already fails it).

How the artefacts reached a trade: momentum ranks on a 126-session return. A
halved base, or a jump inside the window, inflates the rank for up to six
months:

| Trade | 126-session return on IB's bars | Repaired |
|---|---|---|
| momentum CMCSA, entry 2017-08-21 (base 2017-02-17, the halved close) | +115.2% | +7.6% |
| momentum RJF, entry 2022-01-25 (window spans 2021-09-22) | +69.0% | +12.7% |

No sleeve held either name across either date, so neither seam was booked
directly as a return.

## The two artefacts

Both are still served by IB as of 2026-10-17, under `TRADES` and
`ADJUSTED_LAST` alike, from 1-month and 1-year requests alike
(`assets/kan-120/probe.py`). They are IB's data, not a cache fault.

**CMCSA (conId 267748), 2017-02-17.** One bar is broken:

| Date | Open | High | Low | Close |
|---|---|---|---|---|
| 2017-02-16 | 35.81 | 35.81 | 35.45 | 35.59 |
| 2017-02-17 | 35.57 | 35.62 | **17.64** | **17.64** |
| 2017-02-21 | 35.53 | 35.63 | 35.05 | 35.50 |

The low and close are exactly half the session's level, on the last session
before the 2:1 split went ex (2017-02-21). The repair doubles them, giving a
close of 35.28. The true low is lost: IB's low equals its halved close. The
repair sets the low to the repaired close, which slightly understates that
day's range.

**RJF (conId 11656), before 2021-09-22.** The 2021-09-21 close of 57.27 becomes
an open of 86.55 on 09-22 (×1.511). That is the ex-date of the 3:2 split, on a
day with a 1.4% intraday range, and the jump never reverses. The volumes show
which side is wrong. Before 09-22 every volume is a multiple of 0.25, that is
an integer count ×2.25 (1.5 applied twice). From 09-22 every volume is an
integer. So every bar before the split is on a 1/2.25 basis instead of 1/1.5.
The repair multiplies those prices by 1.5 and divides those volumes by 1.5.
The bars from 09-22, which are on the true basis and continue into today's
prices, stay as they are.

## No others

`assets/kan-120/scan.py` sweeps every ticker in the artifact for two shapes,
both at a split ratio:

- **Whole-session level shifts:** ratios 5:4, 4:3, 3:2, 2:1 and up, plus their
  inverses, on a quiet day.
- **Single-field outliers:** one field of a bar out by a ratio of 3:2 or more.

Two classes of hit are exactly CMCSA and RJF:
- level shifts at 3:2 or more;
- single-field outliers inside a ticker's membership that are not market
  events.

The other membership-day hits are real:
- the March 2020 lows;
- GL 2024-04-11 (short report);
- SBNY's collapse;
- MRNA 2026-08-19, at 50× normal volume, with the price holding for days after;
- PARA, already excluded by KAN-119.

The 5:4 and 4:3 level shifts are earnings or deal gaps (META 2022-02-03,
PANW 2017-03-01, LULU, KSS, BHF and others), not splits.

## The repair

`BACKTEST_BAR_CORRECTIONS` has two entries:
- `RepairBar` (CMCSA): divides the named fields back up by the factor;
- `ScaleBefore` (RJF): puts every bar before the ex-date on the post-split
  basis.

Each applies only while its glitch is still present: the bar's fields at the
factor from its open, or the series still jumping by the factor at the seam.
So it is idempotent across cache reloads. It is also a no-op if IB fixes its
data, or if a window does not reach the seam.

The fetch (`fetch_bars_from_ib(corrections=...)`) and the `--bars-from-json`
cache path both apply the repairs. Replaying an artifact written before this
change therefore does not reproduce its own figures. Momentum, for example,
gives 0.570 rather than 0.587 on the 10-13 artifact. The cache path prints a
`NOTE` naming the repaired tickers when that happens. The 0.25-multiple volumes
are as IB serves them, as floats (`assets/kan-120/probe.py`). Artifacts store
volumes as ints, so the evidence is not visible in them. The paper run calls the same fetch without
them. No module under `shared/` or `services/`, nor `run_paper.py` or
`reconcile_paper.py`, references them, and a test pins this.

Other readers of an artifact's `bars` read what the artifact holds:
`run_stability_sweep.py`, `sentiment_eval.py`, `run_factor_evaluation.py` and
`validate_replacement_policy.py`. They inherit the repairs from the next
refresh's artifact onwards. They do not repair an older one.

## Not done

- **Re-pinning.** `baseline_pin` and `rung0_baseline_pin` are unchanged. The
  next scheduled refresh (Tue 06:30 SGT) records repaired bars in its own
  artifact. Pinning it is the operator's call, as for KAN-119.
- **Live bars** (`ohlcv_daily`, from 2026-08-18) are out of scope. Neither
  split falls inside them.
