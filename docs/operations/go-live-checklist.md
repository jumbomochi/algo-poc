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

All eight gates below must pass. Run `scripts/ops/go_live_gate.py` for an
automated assessment; record the results alongside manual verification.

### Gate 1 — Paper Trading Duration

- [ ] Minimum **60 calendar days** of continuous paper trading completed.
- Days elapsed: ______
- Paper start date: ______

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

- [ ] Median slippage **<= 20 bps**.
- [ ] Failed-order rate **<= 1%**.
- Median slippage: ______ bps
- Failed-order rate: ______%

### Gate 5 — Reliability

- [ ] **Zero** unresolved critical alerts (Redis, PostgreSQL, IB connectivity)
  in the last 14 days.
- Unresolved alerts: ______

### Gate 6 — Data Integrity

- [ ] Latest reconciliation checks pass with **no unresolved major
  discrepancies**.
- Reconciliation status: ______

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

## Two-Person Approval

Promotion requires sign-off from **both** the operator and a reviewer.

| Role     | Name | Date | Signature |
|----------|------|------|-----------|
| Operator |      |      |           |
| Reviewer |      |      |           |

### Conditions

- Both parties have independently reviewed the gate results above.
- Both parties confirm that no known issues are being deferred.
- The rollback playbook has been reviewed and is understood by both parties.

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
