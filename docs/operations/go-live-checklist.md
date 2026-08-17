# Paper-to-Live Promotion Checklist

This document defines the mandatory gates that must be satisfied before the
algo-poc trading bot is promoted from **paper** trading mode to **live** mode.

## Promotion Date

| Field           | Value |
|-----------------|-------|
| Planned date    |       |
| Actual date     |       |
| Model version   |       |
| Config revision |       |

---

## Pre-Promotion Gates

All eight gates below must pass. Run the evaluator for an automated assessment
and record its output alongside manual verification:

```bash
# The config default is localhost:5432; the local stack listens on 55432.
export ALGO_DATABASE_URL=postgresql://algo:$POSTGRES_PASSWORD@localhost:55432/algo_poc
python -m scripts.ops.go_live_gate --paper-start 2026-07-30
python -m scripts.ops.go_live_gate --paper-start 2026-07-30 --json
```

**`--paper-start` is not optional in practice — pass the restarted clock date
recorded under Gate 1 below.** Without it the evaluator uses the earliest
equity snapshot, which on this book is **2026-07-10**, i.e. *before* the Path A
flatten-and-refund. Measured across that reset, gate 1 overstates the elapsed
days and gate 3 reports the re-baseline as a **68.38% drawdown** — a capital
event dressed up as a trading loss. With `--paper-start 2026-07-30` the same
book reports **0.33%**. Both numbers are arithmetically correct; only one is
about trading.

It exits `0` only when all eight gates pass, `2` if the database is unreachable
(checked before any gate runs, so a wrong URL cannot masquerade as an empty
book). Today several gates fail — the 60-day clock and model governance among
them — and that is the correct output, not a malfunction.

### Where each gate's number comes from

`PostgresGateDataSource` (`scripts/ops/gate_data_source.py`) maps every gate to
one query against the trading database. Two rules apply throughout:

- **A gate never passes by ignorance.** When the evidence cannot be measured —
  no fills yet, no backtest artifact, a silent alert recorder — the gate is
  reported as `evidence unavailable: <reason>` and **fails**. "We looked and it
  is bad" and "we could not look" are different answers and both block
  promotion, but only the first is a measurement.
- **Drills never move a number.** Every portfolio-scoped query excludes
  `_`-prefixed portfolios, so a synthetic stop-loss drill cannot contaminate
  drawdown or execution quality.

| Gate | Source |
|---|---|
| 1 Paper duration | earliest `equity_snapshots.date` for a non-excluded portfolio |
| 2 Risk stability | `system_halt` rows with `source='circuit_breaker'` in the window (a manual `kill` is an operator decision, not instability) |
| 3 Drawdown | `shared/evidence_store.max_drawdown_pct` — the same arithmetic the epoch report uses, not a second copy |
| 4 Execution quality | `execution_fills` collapsed to a per-order VWAP against `order_intents.limit_price`; failure rate over the orders that actually reached the broker |
| 5 Reliability | `alert_records` — see below |
| 6 Data integrity | newest `reconciliation_reports` row for this mode, **rejected if older than 7 days** — a stale `ok` is not a current one |
| 7 Model governance | `model_versions` |
| 8 Backtest regression | `aggregate.metrics` of the newest `output/backtest_multi_*.json` |

### Resolved: how gates 5 and 6 get durable evidence (KAN-42)

Both gates once asked questions nothing stored the answer to. Resolution:

- **Gate 6** needed no new mechanism. `scripts/reconcile_paper.py` already
  persists a `reconciliation_reports` row on every paper run, so the gate reads
  the newest one for its mode. No row at all is *unavailable*, never "ok".
- **Gate 5** did. Alerts were published to `stream:alerts`, fanned out, and
  forgotten. The notifications service now writes one `alert_records` row per
  alert **before dispatching**, so an alert no channel could deliver is still on
  the record. The database is deliberately off the delivery path: a failed write
  is logged and the alert still goes out.

  The gate counts criticals in the window with `resolved_at IS NULL`. It is
  guarded against the failure this whole readiness effort exists to remove: if
  the recorder wrote *nothing at all* in the window, a count of zero would mean
  "notifications was down" just as readily as "nothing went wrong", so the gate
  reports evidence unavailable instead of passing.

  **Nothing in the system publishes a routine alert on a schedule**, so a
  genuinely quiet fortnight reports unavailable. Until something does, an
  operator must prime the guard at least once every 14 days:

  ```bash
  python -m scripts.ops.send_test_alert            # low priority
  ```

  A low-priority alert proves the pipe without counting against the gate.

  Resolving an alert is a named human act, never automatic:

  ```bash
  python -m scripts.ops.resolve_alert --list
  python -m scripts.ops.resolve_alert --id 41 --resolved-by <name>
  ```

  Already-resolved alerts are refused rather than overwritten, so the trail
  records who called the incident closed first.

**Gate 7 has no approval field to read.** `model_versions` records
`is_active`, not approval, so the data source reports `none` / `inactive` /
`active` and never `approved` — inventing governance the repo does not have is
exactly the kind of silent pass the rest of this document is written against.
Gate 7 therefore fails until the ML decision (KAN-35) and the two-person
approval substitute (KAN-37) land.

### Gate 1 — Paper Trading Duration

- [ ] Minimum **60 calendar days** of continuous paper trading completed.
- Days elapsed: ______
- Paper start date: **2026-07-30** (clock RESTARTED — first clean
  entries-enabled fills after the Path A re-baseline; see note below. Supersedes
  the earlier 2026-06-24 service-stack start.)

#### Clock restart — 2026-07-30 (Path A re-baseline complete)

The Path A re-baseline (flatten + retire both books to empty, re-fund USD, enable
entries) completed 2026-07-30 with the first real fills recorded. Because the
durable book was reset to a clean, broker-reconciled baseline on that date, the
continuous-60-day clock restarts from **2026-07-30** — this is the first day the
live order path actually recorded fills against a reconciled book.

#### Documented exception — 2026-07-01 (IB Gateway outages)

The "continuous" requirement has two known breaks in the daily paper-run record,
both caused by the IB Gateway parking on a stuck login modal after a nightly
re-login (the port-7497 disconnect):

- **~2026-06-08 → 06-14** — daily 04:15 SGT run failed; restored by manual re-login.
- **~2026-06-22 → 06-25** — same failure mode; restored by `launchctl kickstart`.

Additionally, the **service-stack** paper run (data_ingestion → … → execution
against IB paper — the path that produces Gates 4–6 metrics) was first brought up
**2026-06-24**; prior history came from the `run_paper.py` *simulation*, which does
not exercise the live order path.

**Remediation in place:** `local.algo-gateway-watchdog` (kickstarts the Gateway
after two consecutive 7497 failures) now prevents the stuck-modal outage from
recurring unattended. See `deploy/launchd/` and the
[divergence monitor](divergence-monitor.md) for the daily continuity check.

**Decision required (two-person sign-off):** treat the continuous-60-day clock as
**restarting 2026-06-24** (service-stack start), rather than counting the gapped
simulation history. Record the accepted start date and rationale in the sign-off
section below.

### Gate 2 — Risk Stability

- [ ] **Zero** circuit-breaker events in the last 30 days.
- Events found: ______
- Lookback window: last 30 days

### Gate 3 — Drawdown Bound

- [ ] Paper max drawdown **<= 12%** (configurable).
- Observed max drawdown: ______%
- Threshold: ______%

### Gate 4 — Execution Quality

**Now ACCRUING (from 2026-07-30).** The live-order path is validated end-to-end:
recommendations → risk-approved → execution → real IB paper fills → durable
`execution_fills` (12 fills persisted on the first clean entries-enabled run,
the 07-30 US open). This required deploying the previously-missing
`portfolio-accounting` fill projector and fixing its SMART-vs-venue exchange
check; `execution_fills` had **0 rows ever** before this. Slippage / fail-rate
metrics accrue from here via the divergence monitor + reconciliation reports.

- [ ] Median slippage **<= 20 bps**.
- [ ] Failed-order rate **<= 1%**.
- Median slippage: ______ bps  (accruing since 2026-07-30)
- Failed-order rate: ______%  (accruing since 2026-07-30)

**Read the slippage number with its benchmark in mind.** Nothing in the repo
stores an arrival price, so slippage is measured against the *intent's limit* —
the only benchmark that exists. `execution.entry_buffer_pct: 0.3` places buy
limits ~30 bps above the reference, so a normal un-repriced fill reads as
roughly **−30 bps** and this arm of the gate can almost never fail. A negative
reading is the buffer, not evidence of excellent execution. Capturing an arrival
price so the metric can bite is a follow-up, not part of KAN-42.

The failed-order rate counts **broker rejections only**. Orders the system
declined to send itself — a buy refused while halted, a size that rounds to zero
whole shares — leave both sides of the ratio; the execution service does not
call those failures either, and gate 2 already counts the halt.

### Gate 5 — Reliability

- [ ] **Zero** unresolved critical alerts (Redis, PostgreSQL, IB connectivity)
  in the last 14 days.
- Unresolved alerts: ______
- Evidence: `alert_records`, written by the notifications service. A window with
  no recorded alert of any priority reports *unavailable*, not zero — see
  "Resolved: how gates 5 and 6 get durable evidence" above.

### Gate 6 — Data Integrity

- [x] Latest reconciliation checks pass with **no unresolved major
  discrepancies**.
- Reconciliation status: **ok — CLEARED 2026-07-30.** Path A re-baseline
  complete; the durable DB now matches the IB paper account exactly (12
  broker-owned positions, con_ids match). The FX-funding CASH position no longer
  trips reconciliation (`ib_account.py` filters `secType=CASH`).

### Gate 7 — Model Governance

- [ ] Current model version is **approved** and not in rollback or caution
  state.
- Model status: ______
- Version: ______

### Gate 8 — Backtest Regression

- [ ] Latest backtest run passes all metric thresholds:
  - Sharpe ratio >= baseline (default 1.0)
  - Max drawdown <= baseline (default 15%)
  - Win rate >= baseline (default 50%)
- Sharpe: ______
- Max drawdown: ______%
- Win rate: ______%

---

## Gate Approval

**Amended 2026-08-17 (KAN-37, direction doc D14).** This section previously
required sign-off from the operator *and a second reviewer*. algo-poc is
solo-operated: there is no second person, so that gate could only ever be
skipped or signed twice by the same hand — a control that cannot be satisfied
is a control that gets quietly dropped on the day it matters most. It is
replaced, **once**, by the four-part substitute below. All four parts are
mandatory; the substitute is not "one reviewer's worth" of scrutiny made
optional.

### The four-part solo substitute

1. **Written draft.** The gate decision is **drafted in writing** against the
   evidence digest — not against scrollback, memory, or a live database query.
   The draft states each of the 8 gates, its measured value, pass/fail, and the
   evidence artifact it was read from. A gate whose evidence cannot be cited
   has not passed.
2. **Independent adversarial review.** An **adversarial review** by a
   cross-model AI, given the draft and the evidence, tasked with challenging
   the decision rather than confirming it. Its challenges are recorded verbatim
   alongside the draft. Cross-model is the point: a second pass from the model
   that helped write the draft reproduces its blind spots.
3. **Cooling-off.** A mandatory **7-day cooling-off** period separates the
   draft from arming the account. Nothing is armed inside those seven days,
   however green the evidence looks. The period exists to break the link
   between a good week and an irreversible decision.
4. **Unresolved challenges block.** Any **unresolved challenge** from step 2
   blocks promotion. Resolution means a written answer with evidence, or a
   scope change — never a judgement that the challenge was unimportant.

### Record

| Field | Value |
|---|---|
| Draft written (date) | |
| Draft location | |
| Adversarial review model / date | |
| Challenges raised / resolved | |
| Cooling-off ends (draft + 7 days) | |
| Armed (date) | |

### Conditions

- The draft cites, per gate, the evidence artifact each number came from.
- No known issue is being deferred without a written, dated decision.
- The [rollback playbook](rollback-playbook.md) has been reviewed.
- Every live sleeve has written kill criteria with a **final** (not
  provisional) drawdown budget — [sleeve-kill-criteria.md](sleeve-kill-criteria.md).
  This is the universal D3.3 rule: no written kill criteria → no live
  promotion.

---

## Post-Promotion Verification

After switching `ALGO_MODE=live` and redeploying:

- [ ] Verify execution service connects to live IB port.
- [ ] Confirm first order routes correctly (manual observation).
- [ ] Verify Prometheus metrics flowing for live fills.
- [ ] Confirm notification channels deliver live alerts.
- [ ] Schedule first live reconciliation run.

---

## References

- [Rollback Playbook](rollback-playbook.md)
- [Operations README](README.md)
