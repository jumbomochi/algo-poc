# Rollback Playbook — Live to Paper

This playbook defines the triggers and time-bound procedure for rolling back
the algo-poc trading bot from **live** mode to **paper** mode. Any team member
with operator access may initiate a rollback.

---

## Rollback Triggers

**Any single trigger below warrants immediate rollback to paper mode.**

| # | Trigger | Detection Source |
|---|---------|-----------------|
| 1 | Kill switch activation or circuit-breaker event in live mode | Risk service alerts, Prometheus `kill_switch_activated` metric |
| 2 | IB reconciliation major discrepancy not resolved within SLA (default 1 hour) | Reconciliation service, `stream:alerts` |
| 3 | Critical observability outage impacting execution or risk services | Prometheus alertmanager, service health checks |
| 4 | Slippage or fill-quality breach sustained for **3 consecutive sessions** | Execution quality dashboard, `median_slippage_bps` metric |

---

## Rollback Procedure

Execute each step in order. Target completion: **under 15 minutes** for steps
1-3.

### Step 1 — Halt Trading (T+0)

1. Set trading state to **HALTED**.
2. Publish a kill event to `stream:kill`:
   ```bash
   # Via API
   curl -X POST http://localhost:8000/api/v1/kill \
     -H "Authorization: Bearer $OPERATOR_TOKEN" \
     -d '{"reason": "rollback: <brief description>"}'
   ```
3. Verify the kill switch is active:
   ```bash
   curl http://localhost:8000/api/v1/status
   # Confirm: "kill_switch_active": true
   ```

### Step 2 — Switch to Paper Mode (T+2 min)

1. Update the deployment configuration:
   ```bash
   export ALGO_MODE=paper
   ```
2. Redeploy all services:
   ```bash
   docker compose down && docker compose up -d
   ```
3. Verify mode change in service logs:
   ```bash
   docker compose logs --tail=20 | grep "mode.*paper"
   ```

### Step 3 — Verify Disconnection (T+5 min)

1. Confirm the execution service is **disconnected** from the live IB port:
   ```bash
   docker compose logs execution | grep -i "disconnect\|paper"
   ```
2. Verify no live orders are pending:
   ```bash
   curl http://localhost:8000/api/v1/orders?status=pending
   # Confirm: empty list or all cancelled
   ```
3. Confirm Prometheus metrics show paper mode:
   ```bash
   curl -s http://localhost:9090/api/v1/query?query=algo_mode \
     | grep '"paper"'
   ```

### Step 4 — Reconciliation and Triage (T+10 min)

1. Run a full reconciliation:
   ```bash
   python -m scripts.ops.reconcile --mode=post_rollback
   ```
2. Document the state at time of rollback:
   - Open positions at rollback time
   - Pending/partial fills
   - Any discrepancies between IB and internal state
3. Begin incident triage:
   - Identify the trigger that caused the rollback
   - Gather relevant logs, metrics, and alert history
   - Assign an incident owner

### Step 5 — Resume Paper Trading (T+15 min)

1. Deactivate the kill switch:
   ```bash
   curl -X POST http://localhost:8000/api/v1/kill/deactivate \
     -H "Authorization: Bearer $OPERATOR_TOKEN"
   ```
2. Verify paper trading resumes:
   ```bash
   docker compose logs --tail=10 execution | grep "paper.*active\|processing"
   ```
3. Confirm data ingestion and signal generation are running normally.

**Do not resume paper trading until:**
- [ ] Incident action items are documented.
- [ ] Root cause is identified or investigation is assigned.
- [ ] Reconciliation shows no unresolved discrepancies.

---

## Post-Rollback Requirements

Before any future live promotion attempt:

1. All incident action items from this rollback must be resolved.
2. A fresh 60-day paper trading period must be completed (or a shorter period
   approved by two-person sign-off with documented justification).
3. All eight promotion gates in the
   [Go-Live Checklist](go-live-checklist.md) must pass again.

---

## Communication Template

Use this template when notifying stakeholders of a rollback:

```
Subject: [ALGO-POC] Rollback to Paper Mode — <DATE>

Summary: The algo-poc trading bot has been rolled back from live to paper
mode due to: <TRIGGER DESCRIPTION>.

Timeline:
- <TIME> — Trigger detected
- <TIME> — Kill switch activated
- <TIME> — Mode switched to paper
- <TIME> — Disconnection verified
- <TIME> — Reconciliation complete

Impact: <DESCRIPTION OF ANY POSITIONS/ORDERS AFFECTED>

Next steps: <ACTION ITEMS>

Incident owner: <NAME>
```

---

## References

- [Go-Live Checklist](go-live-checklist.md)
- [Operations README](README.md)
