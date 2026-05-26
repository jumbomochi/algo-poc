# Divergence Monitor

**Purpose:** Daily comparison of live paper-trading equity to the most recent
backtest's expectations, per portfolio and aggregate. Flags divergence before
drawdowns or operational issues compound.

**Script:** `scripts/divergence_monitor.py`
**Math layer:** `backtest/divergence.py` (pure functions, no I/O)
**Tests:** `tests/backtest/test_divergence.py` (37 unit) + `tests/scripts/test_divergence_monitor.py` (8 integration)

---

## What it catches

| Symptom | Manifests as | Action |
|---|---|---|
| Fills consistently worse than the 10 bps slippage assumed | `Slip bps` column much higher than 10 | Investigate IB routing, order timing, liquidity in thinly-traded ETFs |
| A signal not firing live the same way it fired in backtest | Live return diverges, daily correlation drops below ~0.7 | Diff the signal output between live and backtest for the same bars |
| Order rejections or stuck positions | Trade count diverges, live equity flat while backtest moves | Check `services/execution` logs, order status in IB |
| Universe drift (live trading a ticker no longer in the backtest universe) | Portfolio in DB but absent from backtest JSON | Re-run backtest, update CAPITAL_ALLOCATIONS, or accept and exclude |
| Commission realization exceeding the $0.005/share assumed | Realized commission > 1.5× assumed | Review IB commission tier, check for high-frequency churn |

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
| `--backtest` | latest `output/backtest_multi_*.json` | Source of expected equity series |
| `--window` | 30 | Trading days in the rolling comparison window |
| `--threshold` | 0.20 | Relative divergence warning threshold (20%) |
| `--portfolio` | (all) | Limit to one named portfolio |
| `--output` | `output/divergence_<date>.json` | JSON report path |
| `--no-output` | — | Skip writing JSON |
| `--prometheus-textfile` | — | Path to write `.prom` for node_exporter |
| `--db-url` | from `config/default.yaml` | PostgreSQL connection string |

### Exit codes

| Code | Meaning | Cron / launchd action |
|---|---|---|
| 0 | All portfolios OK or WARNING | None |
| 1 | At least one portfolio BREACH | Alert (Slack/email) |
| 2 | Hard error (DB unreachable, backtest missing, invalid args) | Page on-call |

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
(`|quantity × exit_price|`). The backtest assumes 10 bps; consistent values
above ~15 bps warrant investigation.

**`Trades`** — count of closed trades whose `exit_date` falls within the
window. Compare to expected trade frequency per sleeve.

---

## Recommended daily wiring

The script is designed for one cron / launchd invocation per day, after the
paper-trading run has written that day's `equity_snapshots` row.

**Suggested order on a typical SGT-timezone host (US market closes at 04:00 SGT next day):**

| Time (SGT) | Job | Why |
|---|---|---|
| 04:15 | `scripts/run_paper.py` | Daily signal run, persists state to DB |
| 04:45 | `scripts/divergence_monitor.py --prometheus-textfile ...` | Reads the snapshots just written |
| 05:00 | Backtest refresh on Mondays only (weekly) | Updates the baseline that divergence is measured against |

**Alert wiring:** the script exits non-zero on BREACH. Wrap the cron line in:

```bash
python scripts/divergence_monitor.py || notify-slack "divergence breach"
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
