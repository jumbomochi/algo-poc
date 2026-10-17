# Shadow seeding: open every window on the model's own book (KAN-105)

**Decided 2026-10-12.** The operator approved option A as recommended, with
no epoch clamp. Implemented in the KAN-105 PR. Measured on `main` at `d6ffdfe`
and on the KAN-105 branch, against the paper book as of the 2026-10-10 run.
The database was read in `default_transaction_read_only` sessions. Artifacts
under `output/` were read and never written. IB was not contacted.

Rubric: when the divergence monitor says live tracked (or did not track) the
model over a window, is the "model" side the model?

## Decision

**Option A: a global warm-up of N = 160 sessions, then normalise the curve.**

- `backtest/shadow_series.py` never calls a sleeve's `signals_fn` for a bar
  dated before `warm_from`. `warm_from` is the session N sessions before the
  window's first session, on the bars' own calendar (the union of bar dates,
  which is the set of sessions the runner replays). The first call still
  receives the full history in `bars`, so every lookback is warm.
- The replay trades from `warm_from`, seeded at live's NAV on the window's
  first session.
- The returned curve, from the window's first session on, is multiplied by
  `k = seed_nav / nav(window_start)`, so `curve[window_start] == seed_nav`.
  The book itself is not rebased.
- KAN-113 capital flows strictly inside the window are scaled by `1/k`, so a
  credit is the same fraction of the shadow's book as of live's. The curve is
  then flow-adjusted as before.
- Bars that cannot hold the warm-up plus 200 sessions of indicator history
  raise `ShadowWarmupTruncated`. They are never silently degraded to a
  shorter warm-up. The failure is contained to the sleeve it hit, named in
  the artifact's `replay_failed`, and recorded NO_DATA by the monitor.
- The paper run's fetch is derived from N: 30 + N + 200 sessions, which is
  2 years at N = 160 (it was 1).
- `divergence.shadow_warmup_sessions: 160` in `config/default.yaml`. The
  seeding version (`gated:curve-normalised`) and N enter the shadow's
  fingerprint, so every verdict from the first run after deploy files under a
  new `baseline_id`.
- No epoch clamp. The warm-up may reach back before the 2026-08-01 restart.

## The defect

`replay_window` used to feed the bars before the window to `signals_fn` and
let `BacktestRunner.run(trade_start_date=window_start)` refuse the buys. The
intent, stated in the module docstring, was that "bars before the window feed
the indicators but never trade".

That intent does not survive contact with the sleeves. Every signal function in
`scripts/run_backtest.py` keeps a private `tracked` book and records a lot in
it **when it emits** a buy, not when the buy fills. So during the warm-up each
sleeve emitted its whole top-N, recorded every name as held, and had every buy
refused. At the window's first session the signal function believed it held a
full book. The replay held cash. Inside the window it bought only names that
newly entered its top-N, or names whose phantom lot had since hit a stop.

Measured on the 10-09 run (window 08-21..10-08):

| sleeve | phantom buys / tickers before ws | shadow invested at ws | first entry (session of 30) |
|---|---|---|---|
| momentum | 33 / 15 | 0% | 2 |
| sector_rotation | 19 / 11 (every ETF it can buy) | 0% | **18** |
| thematic_momentum | 74 / 22 | 0% | 2 |
| quality_value* | 66 / 21 | 0% | 6 |
| earnings_drift* | 31 / 14 | 0% | 8 |
| tail_risk_hedge | 15 / 3 | 0% | 11 |

\* Not replayed in production (KAN-109 data-degraded); measured structurally.

The shadow was therefore not a cold start. It was a phantom start, and the
verdict depended on which names happened to enter the top-N after the window
opened. Momentum's shadow return moved 5.1 pp between two consecutive windows
(10-07 to 10-08) while live moved 1.6 pp.

## Options

None of the options re-scores live's positions: all of them run the production
signal functions with no live `portfolio_context`.

| | 0. Gate only (clean cold start) | **A. Warm-up N, normalise** (chosen) | B. Seed the model's target book at ws | C. One continuous curve |
|---|---|---|---|---|
| What | Do not call `signals_fn` before ws; trade from ws | Gate before ws − N; trade from ws − N; grade from ws; scale the curve so curve[ws] = live[ws] | Start clean at ws − 1 so the target fills at ws's open | Replay from a fixed anchor, slice each window |
| Invested at ws as the model would be (AC1) | No: 0%, and the ramp takes 20–130 sessions | Yes, to within the residual below | Partly, 18–50%. A fresh top-N is not the model's path-dependent book | Yes |
| Momentum BREACHes, 11 windows | 0 | 0 | 0 | 0 |
| Complexity | Tiny | Small | Medium: per-sleeve target rules | High: persisted state or a dependency on the weekly 10y refresh |
| Data | 1y fetch | **2y fetch** (+3–4 min on the 05:15 run) | 1y | 2y+ or bar persistence |
| whole_shares | Fine | Fine with **curve** normalisation | Fine | Fine |

Option 0 is the minimal fix and the runner-up. It removes the momentum
artifact on its own but does not meet AC1: the shadow is still 0% invested at
ws while live's book is warm.

## Why N = 160

One global N. A per-sleeve N would save nothing measurable (the whole replay
takes about 0.1 s per sleeve on two years of bars) and would add six
parameters to the fingerprint.

Re-measured with the final implementation (`assets/kan-105/regrade.py
--warmups 0,20,60,120,160,250,max`). The table shows the mean |window shadow
return(N) − window shadow return(longest warm-up the bars allow)| over the 12
admissible window ends 09-23..10-09, in pp, with the BREACH count in brackets.
"max" is about 290 sessions.

| sleeve | N = 0 | 20 | 60 | 120 | **160** | 250 |
|---|---|---|---|---|---|---|
| momentum | 0.93 (0) | 1.17 (2) | 1.18 (1) | 0.18 (0) | **0.09 (0)** | 0.02 (0) |
| sector_rotation | 1.48 (1) | 1.22 (0) | 1.17 (3) | 0.65 (0) | **0.68 (0)** | 0.80 (0) |
| thematic_momentum | 2.48 (0) | 1.74 (5) | 1.20 (8) | 0.78 (8) | **0.76 (7)** | 0.05 (6) |
| quality_value* | 2.81 (0) | 2.33 (0) | 2.16 (0) | 0.80 (0) | **0.23 (0)** | 0.08 (0) |
| earnings_drift* | 0.40 (0) | 0.01 (0) | 0.01 (0) | 0.01 (0) | **0.01 (0)** | 0.01 (0) |
| tail_risk_hedge | 0.15 (0) | 0.01 (0) | 0.03 (0) | 0.04 (0) | **0.03 (0)** | 0.01 (0) |

- At 160, momentum, quality_value, earnings_drift and tail_risk_hedge have
  converged to the longest warm-up. N = 120 is the defensible minimum
  (momentum 0.18, QV 0.80).
- The investigation's ramp study (holding periods and path convergence of
  ~49 cold starts per sleeve over 2016–2026) puts 160 at or above the p90
  path-convergence time of five of six sleeves: thematic ~70, momentum ~105,
  QV ~155 sessions. The exception is sector_rotation.
- **sector_rotation never fully converges.** It exits only on an 8% trailing
  stop, so a fresh top-3 and its warm book can stay different for 250
  sessions. About 0.5–0.9 pp of path dependence remains at any practical N,
  and its verdicts carry that noise. Its correlation stays low (+0.04..+0.21)
  under every seeding.
- thematic_momentum still moves 0.76 pp between 160 and the longest warm-up,
  but its verdicts barely do (7 BREACHes at 160, 6 at max). N = 250 would
  converge it, but 30 + 250 + 200 = 480 sessions leaves about 20 sessions of
  margin in a 2-year fetch. That is too thin to rely on through holidays and
  missing bars, so it would need a 3-year fetch or bar persistence.
- At N = 160 the run needs 390 sessions and a 2-year fetch holds about 500:
  about 110 sessions of margin.

## No epoch clamp

A variant of A starts the warm-up no earlier than the first session after
`divergence.live_history_from` (2026-08-01), when live restarted from a flat
book. The operator decided against it:

- The question the gate asks is "does live hold what the model would". Live's
  slow ramp after the restart was real under-investment, and a clamp would
  hide it for about 7 months, until ws − 160 passes 08-03.
- Across the 11 windows the clamp turns thematic's 9 BREACHes into 1 but adds
  3 sector_rotation BREACHes (+2.7..+3.3 pp), as measured by the
  investigation's harness. It moves the noise rather than removing it.
- It is wrong after any epoch restart that does not flatten live, such as a
  D13 parameter change.

## Normalise the curve, not the book

The returned curve is scaled. The replay's book keeps whatever NAV the warm-up
produced.

- Grading uses returns only, so a scaled curve is enough. It is also
  necessary: the monitor's AGGREGATE sums sleeve levels, so each shadow must
  open the window at live's level.
- Rebasing the book (resetting positions and cash to live's NAV at ws)
  distorts under whole shares. Flooring 1–10-share quality_value lots to the
  rebased NAV cut its exposure at ws from 63% to 33% and moved its window
  return by 1.2 pp.
- **Reconciliation with the investigation.** The investigation's 11-window
  summary and N-sensitivity tables were computed with a book rebase, and it
  stated that rebase and normalisation differ by at most 0.25 pp. That holds
  for sector_rotation, QV, ED and TRH (≤ 0.02 pp here). It does not hold for
  momentum (0.14–1.24 pp, mean 0.85) or thematic_momentum (0.25–0.64 pp). The
  warm path is identical under both (invested at ws matches to the percent on
  every window). The difference comes after ws: the signal functions size
  entries in fixed dollars from the sleeve's capital, so a new entry is a
  different fraction of a book worth `nav(ws)` than of one rebased to live's
  NAV. The final implementation reproduces the investigation's "A160
  normalise" rows exactly (10-08 window: momentum +1.60% / −0.64 pp, thematic
  +3.31% / −2.10 pp, and so on). The verdicts do not change materially:
  momentum still has 0 BREACHes and a mean abs gap of 0.47 pp instead of
  1.10 pp. Thematic has 7 BREACHes of 12 (9 of 11 under rebase).

## Capital flows: one pass, not two

The investigation specified a first pass to learn `nav(ws)`, needed only when
a flow falls inside the window. **That is not safe with the real sleeves.**
Their signal functions are stateful, so a second pass over the same function
starts with the first pass's `tracked` book: the phantom defect again.

`BacktestRunner.run` instead takes `cash_flow_basis=(anchor_session,
basis_nav)`. It learns its own NAV at the anchor session's close and scales
every later flow by `nav(anchor) / basis_nav` before applying it. The result
is identical to the two-pass definition built from fresh signal functions
(`tests/backtest/test_shadow_capital_flows.py::test_the_one_pass_replay_equals_the_two_pass_definition`).
The runner refuses a flow that would apply on or before the anchor, because
its scale does not exist yet. `replay_window` therefore replays only flows
strictly after the anchor, which is the first bar session on or after ws.
Flows on or before ws are already inside live's NAV there, and the
normalisation absorbs them.

When the bars lack live's first window session, the anchor is the next bar
session. A flow dated in (ws, anchor] is then absorbed too. That is correct
for what the monitor grades:

- The monitor intersects the two curves, so the first session it compares is
  the anchor.
- Live's value there already contains the flow. The shadow's value there is
  the normalised opening level.
- Every return the monitor grades runs from the anchor onward, and a level
  shift before the first compared session moves none of them.

What the shadow does not reproduce, in that one case, is the flow's share of
idle cash. That is a sizing approximation, limited to bars that are missing
live's first window session. Replaying such a flow instead raised in the
runner. Before `build_shadow_replays` isolated sleeves, that one raise blanked
every sleeve's feed (found in review;
`test_a_flow_between_live_s_first_session_and_the_bars_first_is_absorbed`).
`capital_adjustments` is empty today, so no current window is affected.

## Data: the 2-year fetch

- The fetch needs 30 (window) + 160 (warm-up) + 200 (the breadth regime's
  200-session MA, which also covers momentum's 126) = 390 sessions.
  `scripts/run_paper.py` derives `--years` from
  `divergence.shadow_warmup_sessions`
  (`backtest.shadow_series.shadow_fetch_years`), so a larger N cannot leave
  the fetch short. An explicit `--years` still wins. A tagged (drill) run
  produces no shadow and keeps 1 year.
- Cost: one more IB historical request per ticker (140), about +3–4 min on
  the 05:15 run. The 05:45 divergence job keeps about 20 min of slack.
- **Live decisions are unchanged.** Every lookback the live signals use is a
  trailing window inside one year. On the 52 sessions where both fetches
  define a regime, the regime is identical, and rankings only look back.
  `tests/scripts/test_run_paper_fetch_years.py::test_the_second_year_changes_no_live_decision`
  pins this on the production signal functions: the same last year of bars,
  with and without a year before it, produces the identical regime and the
  identical signal from every sleeve.

## When the bars are too short: raise, contained to the sleeve

`replay_window` raises `ShadowWarmupTruncated` when fewer than N + 200
sessions precede the window. A truncated warm-up must never be graded: it
would be a different model filed under the same `baseline_id`.

The calendar is the union of all bars and is shared, but each sleeve's
`window_start` comes from its own live history. Truncation can therefore hit
one sleeve and not another, for example a sleeve whose book stopped recording
early. So the failure is contained per sleeve:

- `build_shadow_replays` catches any replay failure, logs it, and returns
  that sleeve with no curve and the reason. The other sleeves are replayed as
  usual. This was added in review: one raise used to propagate and blank all
  six feeds.
- The artifact names the failure in `replay_failed` (`{sleeve:
  "<ExceptionType>: <message>"}`), and the paper log prints a WARNING per
  sleeve.
- The monitor records that sleeve NO_DATA, with the reason as the first note
  and dated by the shadow's session. It is persisted as "ran, could not
  judge" in the same way as a KAN-109 sleeve. The run exits 5 (partially
  graded, alert naming the sleeve and why), or 3 if nothing is left to grade.
- `data_degraded` was not reused for this. Its note says something specific
  (KAN-109: no new entries were placed), which would be a false reason here.

## Re-grade

`assets/kan-105/regrade.py` on bars from the 2026-10-06 weekly refresh
artifact, extended with `ohlcv_daily` through 10-09, and live from
`equity_snapshots`. "old" is the pre-KAN-105 replay, reproduced inline; it
matches the production artifact `shadow_20261010.json` to 3 decimals on every
replayed sleeve. "A160" is this PR's `replay_window_detail` on 2 years of bars.

### Latest reproducible window: 10-10 run, 08-24..10-09

| sleeve | live | live invested ws / end / mean | old: shadow / gap / corr / status / invested@ws | **A160: shadow / gap / corr / status / invested@ws** |
|---|---|---|---|---|
| momentum | +0.76% | 16% / 77% / 30% | +1.03% / −0.27 pp / +0.42 / OK / 0% | **+2.17% / −1.41 pp / +0.38 / OK / 51%** |
| sector_rotation | −1.16% | 45% / 85% / 44% | −0.76% / −0.40 / +0.19 / OK / 0% | **−3.23% / +2.06 / +0.09 / OK / 79%** |
| thematic_momentum | +5.25% | 41% / 100% / 53% | +4.14% / +1.11 / +0.66 / OK / 0% | **+7.70% / −2.44 / +0.71 / OK / 89%** |
| quality_value* | +1.62% | 12% / 18% / 14% | −0.14% / +1.75 / +0.31 / OK / 0% | **+0.98% / +0.64 / +0.61 / OK / 75%** |
| earnings_drift* | 0.00% | 0% / 0% / 0% | −0.14% / +0.14 / — / OK / 0% | **−0.11% / +0.11 / — / OK / 23%** |
| tail_risk_hedge | −0.86% | 12% / 23% / 10% | −0.77% / −0.09 / +0.88 / OK / 0% | **−0.86% / +0.00 / +0.85 / OK / 13%** |

### All admissible windows (ends 09-23..10-09; status string ordered by window end, `.` = OK)

Earlier windows straddle the 07-25 bulk close and the 08-01 restart.

| sleeve | old: statuses / BREACH / mean abs gap / day-to-day shadow change mean, max | **A160** |
|---|---|---|
| momentum | `..B.BBBBBB..` / **7** / 2.67 pp / 1.30, **5.11** pp | `............` / **0** / 0.47 pp / 0.84, 1.50 pp |
| sector_rotation | `...WWWW.....` / 0 / 1.98 pp / 0.23, 0.60 pp | `............` / 0 / 0.90 pp / 0.45, 1.00 pp |
| thematic_momentum | `.....W......` / 0 / 1.19 pp / 0.87, 2.08 pp | `BBBWWWBBBB..` / **7** / 3.77 pp / 2.23, 4.39 pp |
| quality_value* | `............` / 0 / 1.47 pp / 0.14, 0.33 pp | `.....W......` / 0 / 1.21 pp / 0.60, 1.43 pp |
| earnings_drift* | `............` / 0 / 0.27 pp | `............` / 0 / 0.29 pp |
| tail_risk_hedge | `............` / 0 / 0.07 pp | `............` / 0 / 0.13 pp |

## What the operator should expect

- **Momentum's recorded BREACH streak restarts.** It was an artifact of the
  phantom book: 7 of 11 windows BREACH under the old replay, none under any
  clean seeding. The new seeding version enters the fingerprint, so every
  verdict from the first run after deploy files under a new `baseline_id`
  (`shadow:04efec1adca02ead` becomes `shadow:89693c0029cad3e6` at today's
  parameters). `breach_streak` treats the old rows as history. Nothing is
  deleted or relabelled. Momentum's remaining gap and its flat correlation
  (+0.27..+0.42 under every seeding) are live's own. Live momentum was 16%
  invested until 09-17, and its book does not move like the model's
  (KAN-103, KAN-85/94/95/96).
- **The first Monday evidence digest after deploy may list that week's
  earlier sessions as BLIND.** With no epoch open,
  `scripts/ops/evidence_digest.py` (`_blind_pins`) judges blindness against
  the newest `baseline_id` it finds in the week. The sessions filed before the
  switch sit under the old id, so they read as having no verdict. This is
  cosmetic for that one digest. The rows exist, and the next full week is
  clean.
- **thematic_momentum is expected to BREACH under the new baseline.** This is
  real under-investment. Live sat at about 42% invested from the restart to
  mid-September, while the warmed model was 70–90% invested through a rally.
  The old phantom shadow was under-invested too, so it graded OK. Expect
  thematic BREACHes until the windows covering that period roll off. The
  10-08 and 10-09 windows are already OK. No epoch is open, so nothing fires
  on the streak.
- **sector_rotation carries 0.5–0.9 pp of path dependence** at any practical
  N (see above). Read its small gaps as noise.
- **quality_value and earnings_drift stay KAN-109-degraded.** They are not
  replayed in production, and the monitor records them NO_DATA with the cache
  reason. Their rows above are structural checks on stale or partial caches,
  not strategy evidence. When KAN-109 clears, the warm-up needs about
  9 months of point-in-time fundamentals and 160 sessions of earnings history
  (Alpha Vantage has the history; the fundamentals vendor must cover it).
- The first run after deploy prints "Shadow invested at window start: ..." in
  the paper log. A sleeve near 0% there, other than one the model genuinely
  holds in cash (earnings_drift with no events, tail_risk_hedge outside its
  regime), is the symptom this change fixed.

## Follow-ups (not in this change)

1. **Phantom lots exist inside every backtest too.** 20–49% of buy signals
   never become a lot (an unfilled day-limit entry, a risk or cash refusal),
   and each stays "held" in the signal function until a stop fires, so the
   backtest never retries the entry. Live retries, because it rebuilds from
   `portfolio_context` daily. This affects the warmed shadow, the weekly 10y
   refresh and the D10 series behind KAN-33. The principled fix is to drive
   the replay's signal functions from a `PortfolioContext` built from the
   runner's own positions, as live does.
2. **Data oddity: resolved, no data error (KAN-117, 2026-10-17).**
   `equity_snapshots` shows live momentum about 16% invested and
   sector_rotation about 45% invested from 08-05 to 09-17. Yet `positions` has
   no momentum or sector_rotation row opened between 07-25 and 09-18.
   - **Why the rows are missing.** A sell that closes a lot fully deletes its
     `positions` row (`scripts/paper_state.py`). The lots behind those
     snapshots were opened on 07-30 and later sold: momentum CSCO 12 and UNH 6;
     sector_rotation PANW 9, LLY 2 and HUM 4. They survive only in `trades`,
     with `entry_date` 2026-07-30.
   - **The snapshots are right.** Rebuilding each sleeve from the open
     `positions` rows plus `trades` (`entry_date` up to `executed_at`), priced
     at `ohlcv_daily` closes, reproduces `market_value` to the dollar on every
     day where both have prices. That is 16.1% and 45.7% on 08-18, so the
     under-investment the warm shadow grades is real.
   - **Where they disagree, a fill or exit was booked late.** A snapshot values
     the book as it stands at 05:15, and a later repair back-dates
     `executed_at` or `opened_at`. This applies to:
     - the sector_rotation PANW and LLY exits, reconstructed from IB
       statements;
     - TLT's absent-expiry exit;
     - the 09-18 entries;
     - XLC and ARKW.
   - **Size of the error.** It was largest for PANW, at 1.6% of the sleeve's
     equity, and it reverses in one step on the day the repair is booked.
     Snapshots are as-of records, so they are not rewritten.
3. **Bar persistence.** `ohlcv_daily` already holds the universe from 08-18.
   Once it has about 400 sessions, the shadow could read its warm-up from
   there instead of the 2-year IB fetch, and N = 250 would become affordable.

## Reproducing

From the repo root. The password is loaded from the keychain mirror and the
URL is built inline for the one command that needs it, so it is never
exported to the shell or echoed. The re-grade itself needs no database:

```bash
source deploy/launchd/secrets.sh; algo_load_secrets POSTGRES_PASSWORD; \
ALGO_DATABASE_URL="postgresql://algo:${POSTGRES_PASSWORD}@localhost:55432/algo_poc" \
PYTHONPATH=. python docs/decisions/assets/kan-105/extract_inputs.py --out /tmp/kan105
PYTHONPATH=. python docs/decisions/assets/kan-105/regrade.py --inputs /tmp/kan105 --warmups 160,max
PYTHONPATH=. python docs/decisions/assets/kan-105/regrade.py --inputs /tmp/kan105 --warmups 0,20,60,120,160,250,max
```

`extract_inputs.py` reads the refresh artifact and runs SELECTs only, in a
read-only transaction, and refuses to write under any `output/` tree.
`regrade.py` reads those inputs and the fundamentals and earnings caches, and
writes only `regrade.json` beside its inputs.
