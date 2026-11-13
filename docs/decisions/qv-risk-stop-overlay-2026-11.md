# quality_value and the account trailing stop: pre-registration (2026-11-13)

This record is committed **before any backtest with the overlay exists**.
Results are appended in a later commit, under "Results", and nothing above
that heading changes after the first overlay run.

## Why
Live runs two exit rules on quality_value that the backtest has only one of:

- **The sleeve's own trailing stop**, 12% from the peak, which arms only once
  the peak is above entry (`make_quality_value_signals_fn`,
  `scripts/run_backtest.py`). Every one of the 515 exits in the 11-10 record
  is this stop.
- **The risk service's account-wide trailing stop**, 15% from
  `highest_price_since_entry` (`risk.stop_loss_trailing_pct` in
  `config/default.yaml`, `RiskEngine.check_stop_loss`,
  `RiskManagementRunner.run_stop_loss_check`). It applies to every sleeve and
  sends a market sell. **No backtest, shadow or D10 verdict models it.**

So every quality_value figure on record assumes the sleeve keeps holding a
position the live book sells. NVDA shows the gap: bought 2026-11-04 at
204.17, it never traded above entry, so the sleeve's stop could not arm. The
account stop sold it at −17% (filled 2026-11-11), and the sleeve bought it
back the next session (2026-11-12, order 1583) because it was still in the
top 15. The sleeve does not track stop-outs, so that sell-then-rebuy is
what live does every time.

An exploratory event study on the 11-10 bars (not committed; described here
because it is why this record exists) found the overlay would have fired 27
times in ten years. All 27 were positions that never traded above entry, as
they must be: a position that rose first meets the sleeve's 12% stop before
the account's 15%. After the trigger, the stopped names did *not* keep
falling relative to the rest of the sleeve: +2.6% over 20 sessions and +6.5%
over 60. Neither is significant (t 1.3 and 1.6; 15 distinct months). That
study looked at trigger levels of 12, 15, 20 and 25%. It says nothing about
portfolio drawdown, which is the question this record answers.

## The variants (fixed, none swept)
One new backtest flag, `--risk-stop-pct`, off by default. When set, the
runner applies the live rule after each session's close. It takes each
holding's high since entry (entry price included, raised by every close, as
`update_peak_prices` does) and the session's close. It calls
`RiskEngine.check_stop_loss` with them, the function live calls. On a
trigger it queues a market exit for the next open, with
`exit_reason: "risk_stop"`. A sell the sleeve already queued keeps the
sleeve's reason, the way live suppresses a stop while a sell is working.

| | Overlay | What it is |
|---|---|---|
| **A** | none | The record: the backtest as it stands. Already on record, not a new trial |
| **B** | 15% | What live runs today (`stop_loss_trailing_pct: 15.0`) |
| **C** | 30% | A catastrophe-only stop, 2.5× the sleeve's own 12%. Fixed here without having seen a 30% result; the event study above did not examine it |

**Fidelity limits, stated in advance:**
- **The high is per sleeve.** Live aggregates the high across sleeves by
  ticker, and a trigger exits every sleeve holding that ticker.
- **The stop runs on daily closes.** Live checks every 30 minutes, but its
  marks are the positions' `current_price`. Only the 05:15 run moves that,
  apart from fill and recommendation prices, so the trigger is effectively
  close-based in both.
- **A stopped position can come straight back.** The sleeve may re-buy it the
  session after the exit fills, exactly as live did with NVDA.

## Evaluation protocol
- **Data.** All three runs use the bars of
  `output/backtest_multi_20261110_113936.json` in the deploy clone
  (`--bars-from-json`). They share one snapshot of the deploy clone's
  `data/cache` (via `ALGO_DATA_CACHE_DIR`), whose sha256s are recorded with
  the results. Flags are the refresh's own: `--years 10 --capital 100000
  --universe-snapshots data/universe/sp500_membership.json
  --allow-degraded-data`, all six sleeves, so quality_value's capital matches
  the record. The only difference between runs is `--risk-stop-pct`. A is run
  again from the same snapshot as the control. Its gap to the 11-10 record
  (newer fundamentals filings) is reported.
- **Reported for quality_value under A, B and C:**
  - Sharpe, total return and max drawdown
  - the trade count, and the exits by reason
  - the number of `risk_stop` exits
  - re-buys of the same ticker within 5 sessions of a `risk_stop` exit
  - the commission paid
  - D10 (DSR at n = the sleeve-selection count plus this entry's 2)
- **Search size.** `research/trial_registry.json` gains one entry with
  `n_trials` 2 (B and C), Sharpes `null` until scored.

## Decision rule (quality_value's overlay setting)
1. **B is the comparison point,** because it is what live does now.
2. **An alternative X in {A, C} qualifies** if its Sharpe is at least B's
   **and** its max drawdown is no more than 2.0 points worse than B's.
3. **No alternative qualifies:** keep 15% for quality_value.
4. **C qualifies:** recommend C over A even if A also qualifies. These bars
   exclude delisted names (8.75% of membership-days), which are exactly the
   falling knives that never came back. A wide stop guards against them, and
   this data cannot price that protection.
5. **Only A qualifies:** recommend A, and say what protection is given up.
6. **D10 is reported, not decisive.** Choosing an overlay is a
   risk-management setting, not an edge claim. Quality_value's D10 verdict of
   record (FAIL) is not changed by this record.
7. **The other five sleeves** run under the same overlay, because it is
   account-wide. Their figures are diagnostics, not candidates. A per-sleeve
   change for any of them needs its own record.
8. **Changing live is a separate change and the operator's call.** The risk
   service has one account-wide setting today, and a per-sleeve threshold is
   a code change in `services/risk_management`. **Nothing changes in paper or
   live in this work.** The flag is backtest-only and `run_paper.py` is
   untouched.

## Holdout
None is registered. The overlay fires about 2.7 times a year on this sleeve,
so a 60-session forward window would hold zero or one event and could not
judge it.

## Results (2026-11-13)

**Inputs (as pre-registered):**
- **Bars:** `output/backtest_multi_20261110_113936.json`, sha256 `46701c26…3112`.
- **Caches:** a snapshot of the deploy clone's `data/cache` taken 2026-11-13 10:15 SGT:

  | File | sha256 |
  |---|---|
  | `fundamentals.json` | `7fc692f1…b0cb` |
  | `earnings.json` | `ab5e2f8a…df07` |
  | `earnings_calendar.json` | `acabe344…c8e` |

- **Membership:** `data/universe/sp500_membership.json`, sha256 `41742abe…3344`.
- **Flags:** the refresh's own, all six sleeves.

**Control:** run A reproduces the 11-10 record exactly for five of the six
sleeves, quality_value included (0.892 / 182.8% / 19.7% / 515 trades).
earnings_drift differs (0.420 against 0.376) because its earnings cache has
been refreshed daily since 11-10. It is the same in all three runs.

### quality_value
| | Sharpe | Return | Max DD | Trades | `risk_stop` exits (P&L) | Re-bought ≤ 5 sessions | Commission | DSR (n=10) | D10 |
|---|---|---|---|---|---|---|---|---|---|
| A, none (record) | 0.892 | 182.8% | 19.7% | 515 | 0 | — | $1,033 | 0.94 | FAIL |
| **B, 15% (live today)** | **0.813** | **160.3%** | **20.8%** | **572** | **34 (−$4,806)** | **28** | **$1,150** | **0.82** | **FAIL** |
| C, 30% | 0.876 | 175.2% | 19.4% | 525 | 5 (−$1,484) | 3 | $1,055 | 0.93 | FAIL |

- **The 15% stop costs quality_value without buying protection.** Sharpe
  falls 0.08 and return 22 points, and max drawdown is *worse* (20.8% against
  19.7%).
- **The sleeve buys straight back.** 28 of the 34 stopped positions were
  re-bought within five sessions, the NVDA pattern. Most stops therefore sold
  near a low and bought back near it, paying two commissions and the gap.
- **Holdout (gate-invalid, 113 sessions from 2026-06-01):** 0.26 for A, 0.19
  for C, 0.04 for B. The direction matches the full sample.
- **D10:** all three FAIL. Quality_value's verdict of record is unchanged, as
  pre-registered.

### Verdict under the decision rule
B's figures are 0.813 Sharpe and 20.8% max drawdown, so an alternative needs
Sharpe ≥ 0.813 and max drawdown ≤ 22.8%.
- **A qualifies:** 0.892, 19.7%.
- **C qualifies:** 0.876, 19.4%.

**Rule 4 applies: recommend C, a 30% account stop for quality_value.** It
keeps almost all of A's figures, and it keeps a guard against the delisted
falling knives these bars cannot contain. Changing live is a separate change
and the operator's call (rule 8).

### Other sleeves (diagnostics, not candidates; rule 7)
| Sleeve | A Sharpe / DD | B (15%) Sharpe / DD | C (30%) Sharpe / DD | `risk_stop` exits B / C |
|---|---|---|---|---|
| momentum | 0.524 / 29.8% | identical | identical | 0 / 0 |
| thematic_momentum | 0.543 / 48.6% | identical | identical | 0 / 0 |
| sector_rotation | 0.535 / 23.6% | 0.499 / 23.0% | 0.447 / 24.0% | 9 / 3 |
| earnings_drift | 0.420 / 23.0% | 0.418 / 25.9% | 0.413 / 25.0% | 51 / 6 |
| tail_risk_hedge | 0.120 / 15.9% | −0.183 / 17.4% | 0.113 / 16.3% | 22 / 2 |
| **All six (aggregate)** | 0.674 / 17.0% | 0.641 / 16.2% | 0.654 / 16.7% | |

- **momentum and thematic_momentum never reach 15% from their high.** Their
  own stops (8% max loss, the MA cross) always fire first.
- **Where the 15% stop does fire, it lowers every sleeve's Sharpe.**
  - **tail_risk_hedge:** the stop sells hedge instruments in the drawdowns
    they exist for, then the sleeve buys them back (20 of 22 within five
    sessions). Sharpe falls from 0.12 to −0.18.
  - **sector_rotation and earnings_drift:** both lose Sharpe, and
    earnings_drift's max drawdown is 2.9 points worse.
- **For the whole account,** the 15% stop trims max drawdown by 0.8 points,
  at the cost of 0.03 Sharpe and 7 points of return.
- **None of this licenses a change for those sleeves.** Each needs its own
  record. It does say the 15% account stop was set without a backtest, and
  that the per-sleeve setting rule 8 calls for has uses beyond quality_value.

### Limits
- **The bars exclude delisted names** (coverage BLOCKED at 8.75% excluded).
  These are the cases a stop exists for, so every "stop costs" figure leans
  towards holding. That is why rule 4 prefers C.
- **The run is gate-invalid.** D10 was evaluated against scratch copies of
  the holdout registry, with their recorded evaluations cleared, so no split
  was spent.
- **The exploratory event study preceded this record.** It is disclosed
  above and in the trial registry. 30% was not among the levels it looked at.
