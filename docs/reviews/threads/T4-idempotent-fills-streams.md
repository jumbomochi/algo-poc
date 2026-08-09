# T4 — Idempotent fills & stream hygiene  [P1]

Part of the 2026-08-06 implementation review (`docs/operations/implementation-review-2026-08-06.md`, Theme 3 + § 9). Tracking issue linked via this PR's "Closes #…".

## Problem
The DB order/fill path is idempotent and robust, but the **in-memory risk book** and several consumer loops are not — at-least-once delivery can double-count or silently drop messages.

## Checklist
- [ ] **Make risk's book idempotent to fill replay** — dedup by `execution_id`, or drive the in-memory book from the DB projection instead of replaying raw fills. `risk/runner.py:219-262` (replay via `drain_pending:189-195`)
- [ ] **Use `commission_trading` (USD)**, not native `fill.commission`, in `process_fill`. `risk/runner.py:247,255`
- [ ] **Steady-state poison handling** — on a non-retryable error, DLQ + ack + alert (match the startup-drain behaviour). `risk/runner.py:1038`, `execution/runner.py:664`, `ml_model/runner.py:160`, `signal_generation/runner.py:272`
- [ ] **Add `drain_pending` on startup** for `ml_model` + `signal_generation`; in `ml_model`, ack only after signals are durably aggregated. `ml_model/runner.py:136,159`
- [ ] **Monitor `:dlq` depth** (> 0 → alert) and fix the notifications DLQ path to **ack after send**. `redis_client.py:118-126`, `notifications/runner.py:84-86`

## Acceptance criteria
- Replaying a fill does not move NAV/cash/positions.
- A poison message is dead-lettered + alerted, not silently parked in the PEL.
- `:dlq` depth is monitored; notifications no longer leak its PEL.

## Dependencies
- Overlaps observability (**T6**) for the dlq-depth alert.
