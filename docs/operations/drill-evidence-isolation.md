# Drill Evidence Isolation — the portfolio exclusion contract

Epoch v2 requires two drills per epoch, and one of them (the synthetic
stop-loss drill) places a **real paper order and takes a real fill**. Those
fills land in the same tables the go-live gate reads. Without isolation, a
drill that proves the safety machinery works would simultaneously corrupt the
record proving the *strategy* works. Direction doc **D15** fixes this with a
portfolio tag excluded from gate metrics and divergence input.

This document is the contract. **Every reader of portfolio-scoped data must
exclude synthetic portfolios**, and any new reader must be checked against the
table below before it is trusted as evidence.

## The mechanism

One prefix, one predicate:

| Definition | Location |
|---|---|
| `EXCLUDED_PORTFOLIO_PREFIX = "_"` | `shared/universe.py` |
| `is_excluded_portfolio(name) -> bool` | `shared/universe.py` |
| `DRILL_PORTFOLIO = "__drill__"` | `shared/universe.py` |

The `_` prefix predates the drill tag — it was introduced for the `_aggregate`
rollup row after `peak_nav` summed it alongside the sleeve rows, read **2× NAV**,
and circuit-breakered every buy. Reusing it rather than inventing a second
mechanism means the drill tag inherits three filter sites that already work and
are already tested.

Synthetic portfolios in use today:

| Name | Written by | Purpose |
|---|---|---|
| `_aggregate` | `scripts/run_paper.py` (`run_daily`) | Daily rollup of the graded book's NAV. Never has a `portfolio_config` row. |
| `__liquidation__` | `services/risk_management/runner.py` | Fallback portfolio on the kill-path exit intent. |
| `__drill__` | `scripts/run_paper.py --portfolio-tag` | Epoch drills. Has a `portfolio_config` row (it needs cash). |

## Readers and their exclusion status

| Reader | Reads | Excludes? | How |
|---|---|---|---|
| `shared/position_loader.py` — `load_portfolio_state` cash | `portfolio_config.cash` | ✅ | SQL `NOT portfolio LIKE '_%'` via `EXCLUDED_PORTFOLIO_PREFIX` |
| `shared/position_loader.py` — `load_portfolio_state` peak_nav | `equity_snapshots.equity` | ✅ | same predicate, applied before the per-date sum |
| `scripts/divergence_monitor.py` — per-sleeve scoring | `portfolio_config`, `equity_snapshots`, `trades` | ✅ | explicit `is_excluded_portfolio(name)` skip with a logged reason |
| `scripts/divergence_monitor.py` — aggregate report | live series + `trades` | ✅ | excluded names never enter `live_series_by_portfolio`, so they cannot reach `comparable` or the trade filter |
| `deploy/launchd/run_pipeline_report.sh` — daily equity table | `equity_snapshots` | ✅ | `WHERE portfolio NOT LIKE '\_%'` |
| `scripts/run_paper.py` — `_aggregate` rollup | `portfolio_config`, positions | ✅ | skips excluded names in the sum; suppressed entirely on a tagged run (see below) |
| **`scripts/ops/go_live_gate.py`** | — | ⚠️ **OUTSTANDING** | See below |

### ⚠️ Outstanding: `scripts/ops/go_live_gate.py`

`go_live_gate.py` has **no database access at all**. It declares a
`DataSourceProtocol` and there is no concrete implementation of it anywhere in
the repo, so there is nothing to add a filter to today.

**The requirement lands on whoever implements that data source:** every
portfolio-scoped query it issues — round trips, exposure sessions, drawdown
series, divergence verdicts — must exclude names for which
`is_excluded_portfolio` is True. A gate data source that omits this filter will
count drill trades as evidence and is not fit to gate capital.

This is deliberately recorded as unimplemented rather than silently assumed
handled.

## Running a tagged (drill) run

```bash
python scripts/run_paper.py --portfolio-tag __drill__ --portfolio-tag-capital 500
```

Behavior:

- **Validation direction is inverted on purpose.** A tag is accepted **only if**
  `is_excluded_portfolio(tag)` is True. `--portfolio-tag momentum` is refused
  with exit code 2 before the database is opened, because it would write real
  fills into a graded sleeve. A typo therefore cannot silently pollute the book.
- `--portfolio-tag-capital` is **required** and must be positive. The drill
  sleeve is funded explicitly rather than taking a slice of the graded split. An
  existing tag row is left alone — re-running a drill does not top its cash back
  up.
- **The six graded sleeves are not evaluated.** A tagged run builds exactly one
  portfolio (mirroring the `momentum` sleeve's parameters over its universe, so
  a drill can reliably open and then stop out of a position) and hydrates only
  that tag's positions, cash, and pending orders. The graded books are untouched
  *by construction*, not by filtering afterwards.
- **The `_aggregate` rollup row is not written.** A tagged run fetches only the
  drill sleeve's universe, so graded positions outside it would be marked at
  cost (`compute_equity` falls back to `avg_entry_price`) and the rollup would
  record an equity figure that was never true. Skipping the row is preferable to
  writing a wrong one.

## Adding a new reader

1. Does it aggregate or score anything keyed by `portfolio`? If yes, filter with
   `is_excluded_portfolio` (Python) or
   `~Model.portfolio.startswith(EXCLUDED_PORTFOLIO_PREFIX, autoescape=True)`
   (SQL). Do not re-type the literal `"_"`.
2. Add a test with a `__drill__` row present that asserts the row does not
   affect the result.
3. Add the reader to the table above.

## Related

- Direction doc **D15** (observations only; drills excluded from evidence)
- KAN-32 — running the drills (this story only provides the tag)
- KAN-33 — depends on the tag existing
