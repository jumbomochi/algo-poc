# T7 — Execution order-lifecycle robustness  [P2]

Part of the 2026-08-06 implementation review (`docs/operations/implementation-review-2026-08-06.md`, Theme 1/execution + § 9). Tracking issue linked via this PR's "Closes #…".

## Problem
Several execution-side lifecycle paths lose fills or wedge intents under reconnects, down-scaled orders, and duplicate IB events.

## Status (verified 2026-10-01, KAN-41)

Checked against `origin/main` `a3503fb`. A box is ticked only when shipped code and a test meet it; anything partial stays unticked with the residual named. Finding-level status and the residual register (R1–R7) are in `docs/operations/implementation-review-2026-08-06.md` §12.

## Checklist
- [x] **Re-register callbacks on reconnect** for every tracked trade (today only done at startup), so a mid-session reconnect doesn't drop fills/status. `ib_executor.py:234,277-307`
  — *Done:* `bc9b036` (callbacks for tracked trades survive a reconnect). A fill that completes *during* the outage is picked up only by the next daily execution sweep (KAN-87) — residual R10.
- [ ] **Guard `handle_ib_order_status`** against already-terminal intents; wrap `transition` in try/except + rollback; route `ensure_future` task failures to an alert. `execution/runner.py:562-570`, `ib_executor.py:365,374`
  — *Partial:* guard + rollback done (`daa3b0a`, `TestOrderStatusGuards::test_late_status_for_terminal_intent_is_ignored`); a failed callback task is logged (`ib_executor.py` `_spawn`), not alerted — residual R9.
- [x] **Terminalize an intent as FILLED** (*done:* `8a15bbd`, narrowed in `a097d65`) when the placed quantity fully fills even if `< requested_quantity` (persist the placed/adjusted qty) — avoids the permanent PARTIALLY_FILLED that leaks reservations and blocks all buys at reconcile. `projector.py:388-402`, `engine.py:173,207`
- [x] **Wire the reprice/unfilled loop** into `run()` and advance the reprice bookkeeping — or delete the dead surface so it doesn't read as active. `order_manager.py:235-319`
  — *Done:* `847ad47`, `5f09ec6`. It ages orders out rather than repricing (no quote feed — accepted deferral). `handle_partial_fill` / `min_viable_fill_pct` remain dead (R3).
- [x] **Track exit orders in `open_orders`** so `cancel_all` can reach them; **assert `managedAccounts` matches the expected account** on every connect (paper and live). `order_manager.py:135-183,368-393`, `ib_executor.py:220-252`
  — *Done:* `e4cce82`; exact account pin KAN-11 (`627c345`), active when `ib.account_id` / `ALGO_IB_ACCOUNT_ID` is set.

## Acceptance criteria
- [x] A simulated mid-session reconnect loses no fills. — Holds for orders tracked at reconnect (`test_reconnect_reregisters_callbacks_for_open_trades`); fills completed during the outage are late, not lost (R10).
- [x] A risk-down-scaled buy terminalizes cleanly and does not disable entries at reconcile.
- [x] Duplicate terminal IB statuses don't raise/leak an open transaction.

## Dependencies
- Touches `execution/runner.py` (coordinate with **T1**).
