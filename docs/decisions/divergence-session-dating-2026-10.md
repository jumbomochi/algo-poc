# Divergence session dating: live is graded one session off its shadow (KAN-103)

**Proposed 2026-10-05. Awaiting the operator's decision. This PR changes no
behaviour.** Measured on `main` at `ba7595f` against the paper book as of the
2026-10-03 run. The database was read in a `default_transaction_read_only`
session. Artifacts under `output/` were read and never written.

Rubric: is the evidence the capital ladder will gate on measuring the thing it
claims to measure?

## Decision (recommended)

**Option C: record the session as a fact at write time, in a new column, and
switch the readers to it.** `equity_snapshots` gets a nullable
`session_date`, the US session whose closing prices marked the book. It sits
next to `date`, which keeps its current meaning as the SGT run date.
`run_paper.py` stamps the new column from the bars it actually priced, and
cross-checks it against `valuation_at`. A dry-run-by-default script backfills
the column on history, and the operator runs its `--apply`. In a second PR, the
divergence, shadow and evidence readers switch from `date` to `session_date`,
and the shadow fingerprint gains a dating version. From then on, verdicts land
under a new `baseline_id`, and the old rows stay where they are as history.

Nothing is relabelled. No row is deleted, and no recorded verdict is rewritten.

Two adjacent findings came out of this investigation and need their own tickets:

1. **From 2026-11-03 the 04:15 SGT run happens before the US close.** US
   daylight saving ends on 2026-11-01, so NYSE then closes at **05:00 SGT**.
   The paper run (04:15) and the divergence monitor (04:45) would fire 45 and
   15 minutes before the bell, until 2027-03-12. IB's `useRTH` daily request
   is then expected to return the session's in-progress bar. That would put
   signals, marks and limit orders all on a partial session.
   [Edge cases](#edge-cases) has the details. This is more urgent than KAN-103
   itself.
2. **The rolling shadow starts every window in cash.** It is seeded at live's
   NAV, not live's positions (`backtest/shadow_series.py` module docstring).
   The `earnings_drift` and `quality_value` shadows never trade at all in the
   10-03 window. Their "OK" therefore compares a live book against cash. This
   is a separate fidelity problem that dating cannot fix, and it is the most
   likely remaining driver of `momentum`'s gap
   ([below](#what-alignment-does-not-fix)).

## The defect

`scripts/run_paper.py:850` sets `today = date.today()` on an SGT host, and
`:1028`/`:1044` record every equity snapshot under it. The marks come from
`bars[-1]["close"]` (`:853-856`), the last daily bar IB returned. At 04:15 SGT
that bar is the US session that closed at 04:00 SGT. The snapshot dated Tuesday
therefore holds Monday's close. Live dates run Tue–Sat, and each one names the
day after the session it valued. `valuation_at` (`:841-842`, from
`capital.fx_captured_at`) already records the instant of valuation, and it
agrees with this reading on every row that carries it (see the mapping below).

The shadow is dated correctly. `backtest/shadow_series.py:93-98` keys the
replay by bar date, which is the US session. It then clips that curve to
live's keys (`:156`). `scripts/divergence_monitor.py:410-416` loads live by
`date` unchanged, and `backtest/divergence.py:278-293` intersects the two
series. The result is that live "Tue" (Monday's close) is graded against the
shadow's Tuesday close:

| SGT run | live key | live value is the close of | shadow value at that key is the close of |
|---|---|---|---|
| Tue 04:15 | Tue | Mon | Tue (arrives with Wed's run) |
| Wed 04:15 | Wed | Tue | Wed |
| Sat 04:15 | Sat | Fri | (no Saturday bar, never compared) |
| — | (no Monday key) | — | Mon (never compared) |

Every compared pair is one session apart. US Friday's live value and the US
Monday bar are never compared. `DivergenceDaily.session_date` is
`report.window_end` (`divergence_monitor.py:777`), the last shared key, so it
is never a Monday.

`shared/evidence_store.py:423` `blindness()` expects a row on every NYSE
session, so every Monday reads as BLIND. Earlier work came close to this
without naming it. `backtest/sleeve_comparability.py` already notes that the
snapshot date "carries the run's SGT wall-clock date" and stopped using it for
freshness (2026-09-03). `divergence_monitor.py:1241` stopped using `max(live)`
for the same reason. In both cases the comparison itself stayed one session off.

### Proof that this offset is real and not cosmetic

The near-zero correlations were "consistent with misalignment, unproven". A lag
scan proves the offset for every sleeve whose daily moves can be told apart.
Each pair of consecutive live points (keyed by the session they valued) is
paired with the model's return over the same span, shifted by *k* sessions.
The model is the 10-year baseline `backtest_multi_20260929_102017.json`. It
is a warm book with a bar on every session, Mondays included, so neither the
shadow's clipping nor its cold start can affect it.
`k = 0` is aligned, and `k = +1` is what the monitor does today. Window
2026-08-18..09-28, n = 23–25:

| sleeve | k = −1 | **k = 0 (aligned)** | **k = +1 (today)** | k = +2 |
|---|---|---|---|---|
| `tail_risk_hedge` | +0.02 | **+0.84** | **+0.08** | +0.07 |
| `thematic_momentum` | −0.01 | **+0.67** | **−0.04** | −0.13 |
| AGGREGATE | +0.04 | **+0.41** | **−0.18** | +0.10 |
| `sector_rotation` | −0.01 | +0.06 | +0.11 | −0.12 |
| `momentum` | +0.12 | +0.05 | +0.10 | −0.00 |
| `earnings_drift`, `quality_value` | undefined: one side is constant over the window | | | |

Where a sleeve tracks its model at all, the correlation peaks at the aligned
offset and collapses at the offset the monitor uses. `momentum` and
`sector_rotation` show no structure at any lag. Their live books do not move
like the model, and that question is separate from dating.

## Quantification

**Method.** `docs/decisions/assets/kan-103/regrade.py` regrades all 21 shadow
artifacts (`shadow_20260902..20261003.json`) twice under today's rules:
boundary `divergence.live_history_from: 2026-08-01`, window 30, threshold 0.20,
the two-axis `classify_status`, and the pure functions of
`backtest/divergence.py`. The first pass keys live by `date`, as today. The
second keys live by the session it valued: the last NYSE session closed by
`valuation_at`, or for the 22 run dates written before `valuation_at` existed,
the last session before the SGT date. The two rules agree on all 30 run dates
that carry `valuation_at`. Where two run dates valued the same session, the
later row is used; their values are identical.

**Replication check.** For every run after the KAN-83 boundary landed (09-15
onward), the misaligned pass reproduces the recorded `divergence_*.json`
figures exactly. For example, on 10-03 `momentum` measures −86.9% / −5.41 pp /
+0.027, and `sector_rotation` measures −2.30 pp / −0.171. Runs before 09-15
were recorded without a boundary, from windows starting in July, so their
recorded rows differ from both passes here. That includes the +160.5% of 09-12,
which comes from the frozen pre-rebaseline book.

**One limit.** The shadow artifacts are clipped to live's SGT keys, so they
hold no Monday values. They also lack the day after a missed run (10-01,
09-09). The aligned pass therefore compares 4 of 5 sessions a week, and its
Fri→Tue returns span two sessions on both sides. After the fix, the shadow will
carry Mondays.

### The current window (10-03 run, recorded as session 10-02)

| sleeve | today: rel | abs | corr | status | **aligned: rel** | **abs** | **corr** | **status** |
|---|---|---|---|---|---|---|---|---|
| `momentum` | −87% | −5.41 pp | +0.03 | BREACH | **−60%** | **−3.76 pp** | **+0.20** | **BREACH** |
| `thematic_momentum` | — | −1.19 pp | +0.05 | OK | — | +0.04 pp | **+0.60** | OK |
| `sector_rotation` | — | −2.30 pp | −0.17 | OK | — | −2.35 pp | **+0.19** | OK |
| `tail_risk_hedge` | — | +0.26 pp | +0.46 | OK | — | −0.06 pp | **+0.79** | OK |
| `quality_value` | — | +1.64 pp | — | OK | — | +1.69 pp | — | OK |
| `earnings_drift` | — | 0.00 pp | — | OK | — | 0.00 pp | — | OK |
| AGGREGATE | — | −1.49 pp | +0.25 | OK | — | −0.97 pp | **+0.46** | OK |

Days compared: 24 today, 21 aligned. A relative figure of "—" means the
baseline moved less than the 2.5 pp floor (`MIN_RELATIVE_BASE`).

### History: 21 runs × 7 rows

Across all 147 verdicts, alignment changes **7 statuses**:

| sleeve | changes (run date: today → aligned) |
|---|---|
| `momentum` | 09-25: BREACH → OK (−3.30 → −2.40 pp, just inside the 2.5 pp warn line) |
| `thematic_momentum` | 09-03: BREACH → OK · 09-23: OK → BREACH · 09-25: BREACH → OK · 09-30: WARNING → OK |
| `sector_rotation` | 09-11: OK → WARNING · 10-02: WARNING → OK |
| `tail_risk_hedge`, `quality_value`, `earnings_drift`, AGGREGATE | none |

The verdicts move less than the correlations, because a 30-session window
return is dominated by its two endpoints, and a one-day shift only nudges each
endpoint. What the offset destroys is the daily co-movement. `thematic_momentum`
goes from −0.11..+0.18 to **+0.47..+0.77** on every run. `tail_risk_hedge`
goes from +0.08..+0.47 to +0.06..**+0.79**. AGGREGATE goes from +0.07..+0.28 to
+0.13..**+0.87**.

### Does `momentum`'s BREACH survive? Yes.

| run (SGT) | recorded session | today | aligned |
|---|---|---|---|
| 09-23 | 09-22 | BREACH −139% / −4.04 pp | BREACH −143% / −4.15 pp |
| 09-24 | 09-23 | BREACH −143% / −4.47 pp | BREACH −88% / −2.76 pp |
| 09-25 | 09-24 | BREACH −97% / −3.30 pp | **OK** −71% / −2.40 pp |
| 09-26 | 09-25 | BREACH −87% / −4.77 pp | BREACH −72% / −3.93 pp |
| 09-29 | 09-25 (re-scored) | BREACH −82% / −4.50 pp | BREACH −63% / −3.46 pp |
| 09-30 | 09-29 | BREACH −74% / −3.32 pp | BREACH −74% / −3.32 pp |
| 10-02 | 09-30 | BREACH −69% / −3.13 pp | BREACH −65% / −2.91 pp |
| 10-03 | 10-02 | BREACH −87% / −5.41 pp | BREACH −60% / −3.76 pp |

Alignment shrinks the gap by about a third and breaks the run once. The
recorded streak is 7 sessions (09-22..10-02; Mondays and 10-01 are paused). The
aligned streak as of 10-02 would be about 3, plus whichever of Monday 09-28 and
Thursday 10-01 turned out to be BREACH. The Monday values cannot be
reconstructed from the clipped artifacts. Neither streak reaches the
10-session trigger, and no epoch is open, so nothing has fired.

The July–August "BREACH since July" (`docs/operations/sleeve-kill-criteria.md`
worked example) was graded against weekly-refreshed pinned baselines. Its
windows reached back across the 07-25 bulk close and 08-01 re-baseline that
KAN-83 later excluded, and those baselines were later declared not
like-for-like (NO_DATA from 08-11). Those artifacts
(`backtest_multi_20260721/0728/0804`) are no longer on disk, and that history
is inadmissible under the 08-01 boundary anyway. It is not regraded here.

### What alignment does not fix

Even aligned, live `momentum` returns +2.47% over the 10-03 window while its
shadow returns +6.23%. Live correlates +0.20 with the shadow and +0.05 with the
10-year baseline. The live momentum book does not move like either model.
There are two candidates, neither examined here:

- **The shadow's cold start.** It enters the window in cash and builds a fresh
  top-N book, while live carries what it already holds.
- **Live book composition.** The missed and restored fills of KAN-85/94/95/96
  changed what live actually holds.

The same cold start makes the `quality_value` and `earnings_drift` verdicts
vacuous: their shadows are flat cash for all 24 sessions of the 10-03 artifact,
while live `quality_value` holds about $2.8k of positions. A follow-up
ticket should own both.

### Does KAN-33's D10 FAIL survive? Yes. It never depended on this.

The D10 verdicts of record (`docs/operations/incumbent-edge-evaluation.md`,
KAN-55, 2026-08-28) come from `scripts/run_sleeve_evaluation.py`. That script
computes deflated Sharpe at `n_trials = 8`, the `incumbent_sleeves_2026`
holdout and the stability sweeps, all from **10-year backtest return series**.
It reads neither `equity_snapshots` nor `divergence_daily`. The document says so
itself: *"This evaluation judges the backtest, not the live book."* The FAIL,
and KAN-33's hold that rests on it, are untouched. The ticket's sentence that
groups D10 with the divergence verdicts is wrong. **No re-judge is warranted on
KAN-103 grounds.**

### The 2026-10-05 digest

The digest reported "BLIND on 3 of 6 sessions (2026-09-28, 2026-10-01,
2026-10-05)". Each of the three is wrong for a different reason:

| reported | why | truth |
|---|---|---|
| 09-28 | a Monday, and a Monday can never be a `window_end` | valued by the 09-29 run, so it would be graded after the fix |
| 10-01 | the missed SGT 10-01 run, filed under its SGT date | the unvalued US session is **09-30**. 10-01 was valued by the 08:43 catch-up on 10-02 |
| 10-05 | `evidence_digest.py:1011` sets `as_of` to the UTC date, and at Mon 08:00 SGT that is Monday, but the US Monday session has not opened (Sun 20:00 ET). `blindness(end=as_of)` counts it | in flight: not a gap |

The answer after the fix would be **BLIND on 1 of 5 (09-30)**. The same
inversion applies to the 09-09 gap, where the true unvalued session is
**09-08**. The misaligned record grades 09-08 with live's Friday-09-04 value.

## Consumer map

Line numbers are on `ba7595f`. "Run date" means the SGT wall-clock date of
the 04:15 job. "Session" means a US NYSE session.

| where | reads / writes | date semantics it assumes | effect of the offset today | under Option C |
|---|---|---|---|---|
| `scripts/run_paper.py:850`, `:1028-1029`, `:1044-1046` | **writes** `equity_snapshots.date` | run date (`date.today()`) | source of the defect | also writes `session_date` from the bars priced |
| `run_paper.py:841-842` | writes `valuation_at` = `capital.fx_captured_at` | an instant (UTC) | correct; the only recorded clue to the session | used as the cross-check |
| `run_paper.py:869`, `:1954`, `:1101` | `signal["date"]`, intent `run_date`, intent key | run date | correct: an intent is placed on the run date | unchanged |
| `run_paper.py:126-144` `live_equity_by_sleeve` | reads history to seed the shadow | treats `date` as a session | shadow seeded with Monday's close at "Tuesday", window start one session late | read `session_date`, latest row per session |
| `run_paper.py:191-206` `produce_shadow_artifact` | writes `session_date` / `produced_on` in the artifact | the comment says "the session this shadow speaks for" | `session_date` = max shared key, i.e. a run date | correct as written once live keys are sessions |
| `scripts/paper_state.py:390` `record_equity_snapshot` | upsert on `(portfolio, date)` | run date | — | gains a `session_date` kwarg; the upsert key is unchanged |
| `paper_state.py:588` `get_equity_history` | returns `date` (+ currency columns) | callers treat it as a session | feeds both offsets | also returns `session_date` |
| `paper_state.py:556`, `:579` `get_trades` | `exit_date = executed_at.date()` (UTC) | US session (fills happen in RTH, same UTC date) | `filter_trades_to_window` (`backtest/divergence.py:433`) bounds a US-dated trade list by SGT-labelled window ends: edge trades can fall a day outside | consistent |
| `backtest/shadow_series.py:141-158` | window from live's keys; replay keyed by bar date; clipped to live keys | live keys are sessions | Mondays dropped; seed one session stale | correct as written |
| `scripts/divergence_monitor.py:410-416`, `:419-441` | live per-sleeve and aggregate series | `date` is a session | the one-session offset | read `session_date`, deduped, NULLs excluded |
| `divergence_monitor.py:1202` → `backtest/divergence.py:239-253` | `restrict_live_history` (boundary 2026-08-01) | session | off by one at the boundary | compares sessions |
| `divergence_monitor.py:1225-1248` | `SleeveComparability.graded_session` | max shared key | an SGT label | a session |
| `divergence_monitor.py:726-822` | **writes** `divergence_daily.session_date = window_end` | "the session the verdict covers" | never a Monday; a missed run's hole lands on the wrong date | correct as written |
| `shared/evidence_store.py:328` `breach_streak` | walks NYSE sessions | rows exist per session | Mondays read as pause, so 10 verdicts take about 2.5 weeks | correct as written |
| `evidence_store.py:423` `blindness` | NYSE sessions vs rows | same | every Monday BLIND; real gaps mis-dated | correct as written |
| `evidence_store.py:573` `equity_series` | `GROUP BY EquitySnapshot.date` | session | exposure `day in scored_sessions` (`:788-795`) never counts Saturday rows and credits Monday's exposure to Tuesday; drawdown value unaffected | group by `session_date`, latest row per portfolio |
| `evidence_store.py:647` `epoch_progress` (`:676`, `:690`, `:731`) | start = `started_at.date()`; `effective_as_of`; overdue list | sessions | every Monday "unexplained"; clock paused one day in five | clamp `as_of` to the last *closed* session |
| `scripts/ops/evidence_digest.py:503`, `:530`, `:561`, `:632`, `:779`, `:806`, `:1011` | blind / partial / sleeve / equity / pins / no-verdict sources | sessions; `as_of` = UTC date | Monday BLIND ×2 per week, including an unopened one | sources inherit the fix; clamp `as_of` |
| `shared/absent_sessions.py:89`, `:98` | register 08-13 and 08-18 as "sessions" | session | they are **run dates**; the sessions are 08-12 and 08-17 | re-date in the reader PR |
| `scripts/ops/gate_data_source.py:147` (gate 1) | `min(EquitySnapshot.date)` → calendar span | calendar | ≤ 1 day | unchanged (a calendar-day measure) |
| `gate_data_source.py:196` (gate 3) | `equity_series` → `max_drawdown_pct` | order only | none (duplicates are identical) | inherits `equity_series` |
| `shared/position_loader.py:95-110` | `peak_nav` = max over `GROUP BY date` | none: a maximum | none | unchanged |
| `scripts/ops/backfill_restored_equity.py` | rewrites rows with `date >= effective_from` | run date (the write-off morning) | consistent with itself | unchanged; keeps using `date` |
| `scripts/ops/restore_missed_entries.py:931` | names a `date` range in its note | run date | — | unchanged |
| `scripts/reconcile_paper.py` | no snapshot dates | — | — | — |
| `deploy/launchd/run_pipeline_report.sh:195` | daily report shows last 7 `date`s | display | readers see run dates | optionally add the `session_date` column |
| `backtest/sleeve_comparability.py` | `run_date` vs `produced_on` (both wall clock) | wall clock | correct (fixed 2026-09-03) | unchanged |
| `docs/operations/dead-man-switches.md`, `sleeve-kill-criteria.md` | prose | "04:45 SGT scores the previous US session" | the docs were right and the code was not | no change |

Unaffected: the ML shadow (`run_paper.py:1976`, records keyed by bar session),
`scripts/sentiment_eval.py` (its own `session_date`), `research/` and
`run_sleeve_evaluation.py` (backtest-only).

## Edge cases

Every case below was checked against the 52 recorded run dates and against
`exchange_calendars` XNYS. **Neither launchd nor `run_paper.py` has an NYSE
holiday guard.** Paper runs fire Tue–Sat SGT (`local.algo-paper-trading.plist`)
whatever the US calendar says.

- **US Monday holiday.** Labor Day 2026-09-07: the Tue 09-08 06:27 SGT run
  happened, with `valuation_at` 09-07 18:20 ET, and re-valued Friday 09-04. Its
  equity is identical to Saturday 09-05's row in every sleeve. Today that row
  is graded as US Tuesday 09-08: a flat live day against a real shadow day,
  i.e. manufactured drift. Under Option C both rows carry
  `session_date = 09-04`, and readers take the later one. The Tuesday monitor
  re-scores Friday idempotently, which is correct, because no new session
  exists. **A relabel of `date` would collide** on
  `ix_equity_portfolio_date` and force deleting a row. Coming cases: MLK
  (2027-01-18) and Presidents' Day (02-15).
- **Holiday on Tue–Fri.** Thanksgiving Thursday 2026-11-26, then Christmas and
  New Year (Fridays) and Good Friday 2027-03-26. The next SGT run re-values the
  previous session: same duplicate, same handling. Today, the SGT Friday 11-27
  row would be graded against the shadow's 11-27 half-day bar.
- **Half days** (2026-11-27 and 12-24, 13:00 ET = 02:00 SGT). The 04:15 run
  values the early close. Stamping by bar date needs nothing special. The
  `valuation_at` cross-check must use `session_close`, which
  `exchange_calendars` returns early-close aware.
- **Catch-up runs at odd SGT hours.** Examples: 10-02 08:43 (20:35 ET, after
  Thursday's close, values Thu 10-01), 09-04 17:42 (05:36 ET, before Friday's
  open, values Thu 09-03), 09-10 08:18, 08-26 12:37, 08-21 11:20. All of them
  value the previous session, so stamping is unambiguous. The run date, which
  is what gets mislabelled today, is the only thing that moves.
- **Sunday or Saturday catch-ups.** 08-09 Sunday 22:30 duplicated Saturday
  08-08's Friday valuation (identical values). The handling is the same as for
  a holiday.
- **Missed runs.** Since the 08-01 boundary, eight US sessions were never
  valued: 08-03, 08-05, 08-12, 08-17, 08-31, 09-08, 09-18 and 09-30. The
  matching SGT runs on 08-04, 08-06, 08-13, 08-18, 09-01, 09-09, 09-19 and
  10-01 did not happen. Today each hole is filed one day late, under the
  missing run's date. The session that truly has no live value carries a
  verdict built from the previous session's live value.
- **Runs inside US hours (partial bars).** This has never happened: no
  `created_at` falls in 21:30–04:00 SGT. **It starts on 2026-11-03** (see
  the decision section). From US Monday 2026-11-02 to Friday 2027-03-12, NYSE
  closes at 05:00 SGT, so the 04:15 paper run is 45 minutes early and the 04:45
  monitor is 15 minutes early. The stamp must refuse to name a session that had
  not closed by `valuation_at`: it writes `session_date = NULL`, and the row is
  ungradeable and visible as NO_DATA, rather than guessed. Moving the launchd
  slots, or having `run_paper.py` drop an in-progress bar, is the real fix and
  belongs to its own ticket. The weekly backtest refresh (Tue 05:00 SGT) would
  then run exactly at the close.
- **Stale bars.** If IB's historical farm has not published the last session
  by 04:15, `bars[-1]` is the session before it, and the book is marked a
  session older than `valuation_at` implies. A `valuation_at` rule would
  mis-date such a row, and the bar date gets it right. This is why the stamp
  takes the bar date and uses `valuation_at` only as a cross-check. A mismatch
  is logged. Per-ticker staleness, where one ticker lags the rest, is not
  resolved by a single stamp, and it is a marking problem in its own right.

## Options

| | A: relabel `date` + stamp going forward | B: read-time mapping at the chokepoints | **C: new `session_date` column + reader switch** |
|---|---|---|---|
| History | an UPDATE of the key column on every row. 2 collisions (09-05/09-08, 08-08/08-09) need a **DELETE**. Operator-run, destructive | nothing written | fills a new NULL column only. Idempotent, dry-run by default, operator-run |
| What `date` means | changes meaning in place, so `backfill_restored_equity` / `restore_missed_entries` semantics and every past note citing a run date silently shift | unchanged | unchanged (run date). `session_date` is the new fact |
| Truth source | inferred once, then stored | inferred on every read, from `valuation_at` or the SGT date | **recorded from the bars actually priced**, with `valuation_at` as cross-check |
| Stale-bar / partial-bar runs | mis-dated | mis-dated, and undetectable at read time | detected at write: NULL plus a log line |
| Holiday duplicates | collide | deduped at every reader | deduped at every reader (latest row per session) |
| Artifacts in `output/` | disagree with the DB forever | agree with the DB (both use run dates) | agree: `date` is still there |
| Chokepoints to change | the same readers still need `GROUP BY` fixes after collisions | ≥ 4 readers each carrying the mapping, which can drift | the same 4 readers, reading a column. The mapping lives in one helper used by the stamp and the backfill |
| Migration | none (data only) | none | one additive column |

**B is a reasonable stopgap.** Today the inference rule agrees with
`valuation_at` on every row, so it would produce the same numbers as C on
history. It still has two costs. The inference runs at every read site, where
it can drift, and it cannot see the two failure modes that start in November
(partial bars and stale bars). **A** is rejected: it cannot be done without a
DELETE, it rewrites the meaning of a column that other tools rely on, and it
leaves every `output/` artifact disagreeing with the database.

## What happens to …

- **Existing `divergence_daily` rows (102 under `shadow:53c741ea3eeb8f18`, plus
  12 pinned-baseline NO_DATA rows).** They are left exactly as they are.
  `divergence_daily` is observation-only (D15), and these rows are an accurate
  record of what the monitor said. The reader PR adds a dating version to
  `backtest/shadow_artifact.py::shadow_id_for`, the same way `whole_shares`
  entered the fingerprint (KAN-60), so new verdicts file under a new
  `baseline_id`. `breach_streak` already treats rows under another baseline as
  history, so the misaligned and aligned verdicts cannot mix in one streak.
  The cost is that `momentum`'s recorded 7-session streak restarts. If the gap
  is real, it rebuilds within about 10 sessions. The regrade says it will.
- **The first digest after cutover.** `_blind_pins` (`evidence_digest.py:779`)
  takes the newest baseline in the window. Sessions earlier that week, filed
  under the old id, will read BLIND once. This is noted here so that nobody
  chases it.
- **The absent register (KAN-67).** The register is re-dated in the reader PR:
  `2026-08-13` becomes `2026-08-12` and `2026-08-18` becomes `2026-08-17`, with
  the same causes and references and a note citing KAN-103. Left alone, both
  entries go inert, because `blindness` lists an entry only if its date is
  unobserved and both dates *are* valued. The real holes would then read as
  unexplained. The KAN-67 decision itself (accept, do not backfill) is
  unchanged, and *"two of the 9 NYSE sessions in 08-11..08-21 are absent"*
  stays true. Whether to register the other unvalued sessions (08-31, 09-08,
  09-18, 09-30) is a separate call. Today they correctly read as unexplained,
  which is the register's purpose.
- **Epoch and gate evidence.** `gate_epochs`, `gate_epoch_events` and
  `drill_outcomes` are all empty (verified). No epoch has scored a session, so
  there is no evidence to migrate, as long as the fix lands **before epoch v2
  starts**. If an epoch started first, its clock would pause every Monday, it
  would carry about one "unexplained" session a week, and its streaks would
  need about 2.5 weeks to reach 10. Gates 1 and 3 do not move.
- **Artifacts in `output/`.** They are left untouched. `shadow_*.json` and
  `divergence_*.json` up to the cutover carry SGT-keyed live dates, and this
  document is their errata.
- **KAN-33's hold.** It stands. D10 never read this comparison.

## Implementation plan (Option C)

**PR 1: additive, no reader changes.**

1. Alembic migration: `equity_snapshots.session_date DATE NULL` plus a
   non-unique index `(portfolio, session_date)`. It is not unique, because
   holiday and catch-up duplicates are legitimate.
2. `shared/models/equity_snapshot.py`: the column.
3. New `shared/session_dating.py` (pure):
   - `stamp_session(bar_session, valuation_at, calendar) -> date | None`
     returns `bar_session` if that session had closed by `valuation_at`, and
     `None` if it had not (partial bar). It logs when `bar_session` is older
     than the last closed session (stale bars).
   - `infer_session(run_date, valuation_at, calendar) -> date` holds the
     backfill rule used in this document.
4. `scripts/run_paper.py` `run_daily_cycle`: `marks_session = max(_bar_date(b[-1]["date"]) for b in bars_by_ticker.values() if b)`,
   stamped through the helper and passed to both `record_equity_snapshot`
   calls.
5. `scripts/paper_state.py`: `record_equity_snapshot(..., session_date=None)`
   is written on insert and on update. `get_equity_history` returns it.
6. `scripts/ops/backfill_snapshot_sessions.py`: **dry-run by default.** It
   prints the 52-date mapping, the two duplicate pairs and any disagreement
   between the two rules. `--apply` sets `session_date` only `WHERE
   session_date IS NULL`, in one transaction. **The operator runs `--apply`.**
7. Tests: `tests/shared/test_session_dating.py` covers:
   - a Tue 04:15 SGT run stamps Monday;
   - the Tuesday after Labor Day stamps Friday;
   - a Saturday run stamps Friday;
   - the Sunday catch-up stamps Friday;
   - the 08:43 SGT catch-up stamps the previous session;
   - a half day valued after 13:00 ET stamps that session;
   - a 2026-11-03 04:15 SGT run with a 11-02 bar stamps `None`;
   - a stale bar stamps the bar session.

   Also: a paper_state round-trip, and backfill tests (dry-run writes nothing;
   `--apply` touches only NULLs; it is idempotent; duplicates are reported).

**Operator step.** `alembic upgrade head`, then
`scripts/ops/backfill_snapshot_sessions.py`, reviewing the dry-run output,
then the same command with `--apply`. Afterwards, check one 04:15 run's stamped
value against the backfill rule before PR 2 merges.

**PR 2: the readers flip together.**

1. `run_paper.live_equity_by_sleeve` and `divergence_monitor.load_live_equity_series`
   / `load_live_aggregate_series` key by `session_date`, take the latest
   `created_at` per session, and skip NULLs.
2. `backtest/shadow_artifact.py::shadow_id_for` gets a dating version in the
   fingerprint, so the new `baseline_id` restarts streaks by construction.
3. `shared/evidence_store.equity_series` groups by `session_date` (latest row
   per portfolio per session) and excludes NULLs. `epoch_progress` clamps its
   end to the last session closed by "now".
4. `scripts/ops/evidence_digest.py`: `as_of` defaults to the last *closed* NYSE
   session, not the UTC date. `equity_source` reads `session_date`.
5. `shared/absent_sessions.py`: re-date 08-12 and 08-17, with a KAN-103 note in
   `DECISION`.
6. Docs: `docs/operations/divergence-monitor.md` ("Dated by the session") and a
   pointer here from `shared/absent_sessions.py`.
7. Tests:
   - A Tuesday run writes a **Monday** `divergence_daily` row, end to end on
     sqlite with a shadow fixture.
   - A week of aligned rows gives `blindness` no BLIND Monday.
   - A Labor-Day duplicate dedupes, and the Tuesday re-score updates Friday.
   - A NULL `session_date` row is excluded and the sleeve reads NO_DATA.
   - The shadow's day-0 equals live's value **for the same session**.
   - The digest `as_of` on a Monday 08:00 SGT excludes the unopened session.
   - The register's dates are NYSE sessions with no valued snapshot.

Both PRs touch only path-sourced code (`scripts/`, `shared/`, `backtest/`), so
they go live when the deploy clone is pulled. PR 1 also carries a migration,
which is outside CI's coverage (see CLAUDE.md), so verify it by hand against a
real Postgres before release.

## Reproducing the numbers

`docs/decisions/assets/kan-103/regrade.py` and `lagscan.py` are read-only and
write only their own output files. They take a SELECT export of
`equity_snapshots` (the query is in `regrade.py`'s docstring) and read the
`shadow_*.json` artifacts and one `backtest_multi_*.json` in place.
