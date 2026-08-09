# T7 — Execution order-lifecycle robustness  [P2]

Part of the 2026-08-06 implementation review (`docs/operations/implementation-review-2026-08-06.md`, Theme 1/execution + § 9). Tracking issue linked via this PR's "Closes #…".

## Problem
Several execution-side lifecycle paths lose fills or wedge intents under reconnects, down-scaled orders, and duplicate IB events.

## Checklist
- [ ] **Re-register callbacks on reconnect** for every tracked trade (today only done at startup), so a mid-session reconnect doesn't drop fills/status. `ib_executor.py:234,277-307`
- [ ] **Guard `handle_ib_order_status`** against already-terminal intents; wrap `transition` in try/except + rollback; route `ensure_future` task failures to an alert. `execution/runner.py:562-570`, `ib_executor.py:365,374`
- [ ] **Terminalize an intent as FILLED** when the placed quantity fully fills even if `< requested_quantity` (persist the placed/adjusted qty) — avoids the permanent PARTIALLY_FILLED that leaks reservations and blocks all buys at reconcile. `projector.py:388-402`, `engine.py:173,207`
- [ ] **Wire the reprice/unfilled loop** into `run()` and advance the reprice bookkeeping — or delete the dead surface so it doesn't read as active. `order_manager.py:235-319`
- [ ] **Track exit orders in `open_orders`** so `cancel_all` can reach them; **assert `managedAccounts` matches the expected account** on every connect (paper and live). `order_manager.py:135-183,368-393`, `ib_executor.py:220-252`

## Acceptance criteria
- A simulated mid-session reconnect loses no fills.
- A risk-down-scaled buy terminalizes cleanly and does not disable entries at reconcile.
- Duplicate terminal IB statuses don't raise/leak an open transaction.

## Dependencies
- Touches `execution/runner.py` (coordinate with **T1**).
