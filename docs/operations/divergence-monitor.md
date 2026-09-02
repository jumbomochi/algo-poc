# Divergence Monitor

**Purpose:** Daily comparison of live paper-trading equity to the most recent
backtest's expectations, per portfolio and aggregate. Flags divergence before
drawdowns or operational issues compound.

**Script:** `scripts/divergence_monitor.py`
**Math layer:** `backtest/divergence.py` (pure functions, no I/O)
**Tests:** `tests/backtest/test_divergence.py` + `tests/scripts/test_divergence_monitor.py`

---

## The baseline has to be like-for-like

The monitor only grades live against a backtest that live could actually have
matched. It reads the baseline's declared execution model from the results
JSON's `config` block and requires **next-open fills**, a **per-order commission
floor**, and a **point-in-time universe**. Anything the config does not declare
is treated as the unsafe value.

A backtest that filled same-bar (entries at the decision day's low, exits at
that day's open) is unachievable, so live trails it by construction; grading
against one either excuses real drift or invents drift that is only the
baseline's optimism. When the baseline fails the check, every report comes back
`NO_DATA` with a note naming each unmet requirement, the header is tagged
`[NOT LIKE-FOR-LIKE]`, the arithmetic is still printed so the gap is visible,
and the process **exits 3** so the daily job cannot log it as OK.

Fix: regenerate the baseline per
[backtest-baseline.md](backtest-baseline.md). A results JSON with no
`fill_model` key predates the 2026-08-06 rebaseline and is treated as same-bar.

---

## What it catches

| Symptom | Manifests as | Action |
|---|---|---|
| Fills consistently worse than the baseline's slippage assumption | `Slip bps` column above 1.5× the baseline's rate | Investigate IB routing, order timing, liquidity in thinly-traded ETFs |
| A signal not firing live the same way it fired in backtest | Live return diverges, daily correlation drops below ~0.7 | Diff the signal output between live and backtest for the same bars |
| Order rejections or stuck positions | Trade count diverges, live equity flat while backtest moves | Check `services/execution` logs, order status in IB |
| Universe drift (live trading a ticker no longer in the backtest universe) | Portfolio in DB but absent from backtest JSON | Re-run backtest, update CAPITAL_ALLOCATIONS, or accept and exclude |
| Commission realization exceeding the baseline's `max($1/order, $0.005/share)` | Realized commission > 1.5× assumed | Review IB commission tier, check for high-frequency churn |

## Usage

```bash
# Daily run, auto-pick latest backtest, write JSON to output/divergence_YYYYMMDD.json
python scripts/divergence_monitor.py

# Tighter window (good for fast-moving markets)
python scripts/divergence_monitor.py --window 14 --threshold 0.15

# Single sleeve focus
python scripts/divergence_monitor.py --portfolio momentum

# Wire to Prometheus via node_exporter textfile collector
python scripts/divergence_monitor.py \
    --prometheus-textfile /var/lib/node_exporter/textfile/divergence.prom
```

### CLI reference

| Flag | Default | Meaning |
|---|---|---|
| `--backtest` | latest `output/backtest_multi_*.json` | Source of expected equity series. The nightly job passes the pin instead — see below |
| `--pinned` | off | Treat `--backtest` as the baseline of record: no recency fallback, and exit 3 on a missing pin or a sleeve-shape mismatch |
| `--window` | 30 | Trading days in the rolling comparison window |
| `--threshold` | 0.20 | Relative divergence warning threshold (20%) |
| `--portfolio` | (all) | Limit to one named portfolio |
| `--output` | `output/divergence_<date>.json` | JSON report path |
| `--no-output` | — | Skip writing JSON |
| `--prometheus-textfile` | — | Path to write `.prom` for node_exporter |
| `--db-url` | from `config/default.yaml` | PostgreSQL connection string |
| `--redis-url` | from `config/default.yaml` | Only used to alert if the verdicts cannot be stored |

### Evidence store rows

Every run writes one `divergence_daily` row per scored sleeve (KAN-27) — the
durable copy the epoch clock, the breach-streak trigger and the weekly digest
read. The console table, the JSON report and the `.prom` file are all either
transient or overwritten, so they are reports, not evidence.

- **Dated by the session, not the clock.** `session_date` is the last aligned
  session in the window, so a Saturday catch-up run records Friday's session.
- **Keyed `(sleeve, session_date, baseline_id)`.** A re-run against the same
  baseline updates the row in place; a re-run after a rebaseline inserts new
  rows, because a verdict is only interpretable against the baseline that
  produced it.
- **An ad-hoc run cannot overwrite the canonical verdict.** The examples above
  re-score the same session at a different `--window`/`--threshold`, which lands
  on the same key (`window_end` does not move with `--window`). A stored verdict
  scored under different pins is a different observation, so the monitor prints
  its reason and leaves the row alone — exploring a threshold must not be able
  to clear a firing breach streak. Re-run with the canonical pins (the defaults,
  which is what the 04:45 job uses) to update it.
- **`NO_DATA` is recorded, absence is not.** A recorded `NO_DATA` means the
  monitor ran and could not judge; a missing row on an NYSE trading day means
  the monitor did not run. Both pause the epoch clock, and the store has to be
  able to tell them apart. The one case that writes nothing is a window with no
  aligned session at all — there is no session to date the row by.
- **`AGGREGATE` is never stored.** It is a derived roll-up (D15); the digest
  recomputes it from the per-sleeve rows.
- **A write failure never changes the exit code.** The wrapper branches on the
  verdict, and a store outage must not mask a BREACH. The failure is logged and
  raised as a high-priority `stream:alerts` alert instead — which is why
  `run_divergence.sh` exports `ALGO_REDIS_URL`. That credential is optional by
  design: an alert-path dependency must not be able to abort drift detection.

### The rolling shadow (`--shadow`)

A pinned artifact cannot score sessions later than its own last bar. The
monitor intersects live dates with the baseline's, so the comparison window is
capped there — measured across `output/divergence_20260822..20260829.json`, six
consecutive nightly runs all reported `window_start=2026-07-10
window_end=2026-08-14` and rewrote the same evidence row. **The monitor had not
looked at a new session since 2026-08-14**, and a breach streak could never
exceed 1 because only one `session_date` ever existed per baseline.

`--shadow output/shadow_<YYYYMMDD>.json` replaces the feed. The 04:15 paper run
replays each sleeve's own signal function over the bars it just fetched, seeded
at live's NAV `--window` sessions back, and writes the resulting curve. The
monitor grades against that, so `window_end` is the current session.

- **Mutually exclusive with `--pinned`** (exit 2). The shadow is the model
  replayed against live's own bars; the pin is a frozen artifact.
- **No execution-model check.** Every requirement of one is satisfied by
  construction: the runner decides on a session's close and fills the next
  (next-open), `CostModel()` carries the $1.00 per-order floor, and no
  membership calendar is used, so there is no point-in-time universe question
  and no membership-days to price. The 11.28% exclusion behind D18 is a
  property of the 10-year artifact, not of a 30-session window over live's
  current universe.
- **No baseline-age check.** A shadow is rebuilt nightly, so "how old is the
  file" is meaningless. Its freshness question is *which session was it
  produced for*, answered per sleeve against the session being graded.
- **Comparability is per sleeve**, not per artifact: stale shadow, sleeve
  absent from the replay, or fewer than two overlapping sessions. Each refusal
  names its reason in the report's notes and the arithmetic is still printed,
  so the gap stays visible.
- **The evidence `baseline_id` is the shadow's model fingerprint**, never its
  filename. `shadow_<date>.json` changes nightly; using the name would put every
  session under its own baseline and no streak could ever fire.

A **missing** shadow is not an empty book: it means the 04:15 run did not
produce one, which is the blind signal `evidence_store.blindness` derives from
absence.

### Exit codes

| Code | Meaning | Cron / launchd action |
|---|---|---|
| 0 | All portfolios OK or WARNING (or genuinely no overlapping history yet) | None |
| 1 | At least one portfolio BREACH | Alert (Slack/email) |
| 2 | Hard error (DB unreachable, backtest missing, invalid args) | Page on-call |
| 3 | **Nothing could be graded** — the monitor is blind and no drift detection is running. On the shadow feed this usually means the 04:15 paper run produced no shadow series | Alert; check `~/ibc/logs/paper_*.log` — the fault is in the paper run |
| 5 | **Some sleeves graded, some not** — a degradation, not an outage. Drift detection IS running for the graded half | Alert; the message names which sleeves were skipped and why |
| 4 | Baseline **stale** — the verdicts are real, but the artifact they were scored against is older than `--max-baseline-age-days` | Alert; the fault is upstream in the weekly refresh, not in divergence |

Precedence is worst-outage-first: **1 > 2 > 3 > 5 > 4**. A breach outranks code 3
if both somehow apply — in practice they cannot co-occur, since a
non-comparable baseline forces every status to `NO_DATA` and there is nothing
left to breach, which is exactly why code 3 must not be 0. Code 3 outranks
code 4 for the same kind of reason: *blind* means no drift detection is running
at all, *stale* means it is running against old expectations.

### The baseline is pinned, not picked (code 3)

Production passes `--backtest <pin> --pinned`, where the pin comes from
`divergence.baseline_pin` in `config/default.yaml` via
`scripts/ops/baseline_pin.py` (KAN-51). Two failures land on exit 3 with a named
token on stdout — `BASELINE_PIN_MISSING` and `BASELINE_SHAPE_MISMATCH` — and
neither writes a report or an evidence-store row, since a session that was never
scored must not appear in the store as a `NO_DATA` observation. There is no
fallback to filename recency, deliberately. The full contract, and the re-pin
procedure, are in
[backtest-baseline.md](backtest-baseline.md#the-pinned-baseline-of-record).

### Baseline staleness (code 4)

The weekly refresh (`deploy/launchd/run_backtest_refresh.sh`, Tue 05:00 SGT)
keeps the baseline current. Between **2026-07-28 and 2026-08-18 it did not
succeed once**: one Tuesday the host was booted after the calendar slot and
launchd never re-fired the job, the next the IB Gateway was unreachable. Only
the second told anyone. Throughout, this monitor went on comparing live equity
to a three-week-old baseline and printing numbers, because nothing checked how
old the thing it was reading actually was.

So the age is now checked **here, where the artifact is consumed**, rather than
in the job that produces it — a producer that never runs cannot report that it
never ran, and this check is identical under every cause of the gap, including
ones nobody has enumerated.

- **Threshold:** `--max-baseline-age-days`, default **14** — two missed weekly
  refreshes. One miss is a bad week; two is a broken job. `0` disables the
  check, for an ad-hoc run deliberately scored against an old baseline.
- **Where the age comes from:** the artifact's filename stamp
  (`backtest_multi_YYYYMMDD_HHMMSS.json`), falling back to its mtime for a
  hand-named file passed via `--backtest`. The filename is preferred because it
  records when the backtest was *run*: restoring a backup, rsyncing `output/`
  or a plain `cp -r` rewrites the mtime and would make a stale baseline look
  fresh, which is the one direction this check must never err in.
- **What you see:** a `BASELINE_STALE:` line on stdout (so it lands in
  `~/ibc/logs/divergence_YYYYMMDD.log`), a `baseline_staleness` block in the
  JSON report, an `algo_poc_divergence_baseline_age_days` gauge in the
  Prometheus textfile, and exit 4 — which is what actually reaches a human,
  since `run_divergence.sh` sends its Telegram from the exit code.
- **The fix is upstream.** Check `~/ibc/logs/backtest_refresh_*.log` and the
  refresh's dead-man check; do not clear the warning by re-running the backtest
  without `--universe-snapshots` (see [backtest-baseline.md](backtest-baseline.md)).

---

## Status classification

Each portfolio is tagged on a **two-axis test** — divergence is concerning if
*either* the relative or absolute figure exceeds its threshold.

| Status | Relative divergence | Absolute divergence (pp) | Glyph |
|---|---|---|---|
| `OK` | ≤ threshold (default 20%) | ≤ 2.5 pp | ✓ |
| `WARNING` | > threshold | > 2.5 pp | ⚠ |
| `BREACH` | > 2 × threshold (40%) | > 5 pp | ✗ |
| `NO_DATA` | no overlapping dates | — | · |

Using both axes prevents two failure modes:
- A tiny backtest baseline that makes the relative metric blow up on noise
- Both returns being large but a fixed pp gap being meaningful

---

## What the metrics mean

**`Live`** — total return of paper-trading equity over the window, end / start − 1.

**`Backtest`** — total return of the same-window daily series from the
backtest JSON (`portfolio_values[1:]` aligned to `dates[i]` — the first
`portfolio_values` element is pre-day-0 initial capital and is dropped).

**`Δ pp`** — absolute return divergence in decimal: `live - backtest`.
`+0.02` = +2 pp = live outperformed by 2 pp.

**`Δ rel`** — relative divergence: `(live - backtest) / |backtest|`. Lets us
flag drift when both returns are small.

**`Corr`** — Pearson correlation of *daily returns* (not equity levels) over
the window. Should be close to 1.0 when live tracks backtest. If it drops
below ~0.7, signals are firing differently between live and backtest.
Returns `None` (renders as `—`) when the series is constant or too short.

**`Slip bps`** — average realized slippage per fill, weighted by notional
(`|quantity × exit_price|`). Compared against the baseline's own declared
slippage (printed in the header); consistent values above 1.5× it warrant
investigation.

**`Trades`** — count of closed trades whose `exit_date` falls within the
window. Compare to expected trade frequency per sleeve.

---

## Daily wiring (deployed)

The script runs once per day via launchd, after the paper-trading run has
written that day's `equity_snapshots` row. The deployed job is:

- **Label:** `local.algo-divergence-monitor` (loaded; runs 04:45 SGT, Tue–Sat)
- **Wrapper:** `~/ibc/run_divergence.sh` — handles the exit-code routing below
- **Plist:** `~/Library/LaunchAgents/local.algo-divergence-monitor.plist`
- **Version-controlled copies + install/reload steps:** `deploy/launchd/`
- **Logs:** `~/ibc/logs/divergence_YYYYMMDD.log`
- **Prometheus textfile:** `~/ibc/metrics/divergence.prom` (node_exporter not yet
  installed — repoint at its textfile collector once it is)

**Order on this SGT-timezone host (US market closes at 04:00 SGT next day):**

| Time (SGT) | Job | Why |
|---|---|---|
| 04:15 | `scripts/run_paper.py` | Daily signal run, persists state to DB |
| 04:45 | `scripts/divergence_monitor.py --prometheus-textfile ...` | Reads the snapshots just written |
| 05:00 Tue | Backtest refresh (weekly, `local.algo-backtest-refresh`) | Updates the baseline that divergence is measured against. Tuesday, not Monday: IBKR's hist-data farm is routinely down from Saturday night through Monday pre-market. |

**Alert wiring:** the script exits non-zero on BREACH (1), on a non-comparable
baseline (3) and on a stale one (4). Wrap the cron line in:

```bash
python scripts/divergence_monitor.py || notify-slack "divergence breach or blind monitor"
```

Or use the JSON output as a Grafana data source for richer alerting.

---

## Limitations (deliberate, v1)

1. **No counterfactual replay.** The "backtest" series is what the most
   recent full backtest produced on the same dates — not a re-run of the
   signal functions against the live bars. This means a signal change made
   *after* the last full backtest won't show up here until the next backtest
   refresh. Mitigation: re-run the backtest weekly (and after any signal
   change).
2. **No auto-disable on persistent breach.** The script flags; it does not
   enforce. Disabling a sleeve on N consecutive breaches is a candidate for
   a future layer (the kill switch already exists at the risk-engine level;
   wiring divergence-driven disable should go through that channel, not
   bypass it).
3. **`portfolio_values` end-of-day alignment.** The backtest stores
   `len(dates) + 1` values where the first is pre-day-0 initial capital.
   We drop it so `portfolio_values[i+1]` aligns with `dates[i]` (end-of-day).
   Verify in `scripts/visualize_backtest.py` if changing this convention.
4. **Slippage from `Trade.slippage` column.** This column must be populated
   by the execution layer (`services/execution`) on each fill. If it's
   defaulting to 0, the `Slip bps` column will be misleadingly low. Verify
   by spot-checking a recent fill against the IB execution report.

---

## Troubleshooting

**`ERROR: No backtest JSON found.`**
Run `python scripts/run_backtest.py` (or `--bars-from-json` if IB Gateway
is down) to produce a fresh `output/backtest_multi_*.json`.

**`ERROR: No paper trading state in DB.`**
Run `python scripts/run_paper.py --init` to create the schema rows.

**`ERROR: Could not load paper state from DB ... password authentication failed`**
Check `config/default.yaml` `database.url` or the `ALGO_DATABASE_URL` env
var. Then verify migrations are current: `alembic upgrade head`.

**Status shows `NO_DATA` for every portfolio.**
The live equity dates and backtest dates have no overlap. Most common
cause: backtest ends weeks before live starts, or vice-versa. Re-run the
backtest to bring its end-date current.

**One sleeve appears in DB but not in backtest.**
The script logs `⚠ Skipping '<name>': not present in backtest` and continues
with the others. This is the expected behavior when a sleeve was dropped
(e.g. `mean_reversion`, `short_term_mr` after 2026-05-26). Clear it from
the DB via `paper_state.py` if you don't want to see the message.

**Daily correlation suddenly drops.**
Most likely a signal function changed, or a data feed flipped (e.g. an IB
ticker re-listed). Spot-check by running the backtest on the live-only
bars: `scripts/run_backtest.py --bars-from-json <a JSON containing the
live window>` and compare to live trades for the same dates.
