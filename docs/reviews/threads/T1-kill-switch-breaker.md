# T1 — Kill-switch & circuit-breaker state machine  [P0]

Part of the 2026-08-06 implementation review (`docs/operations/implementation-review-2026-08-06.md`, Theme 1 + § 9). Tracking issue linked via this PR's "Closes #…".

## Problem
The emergency-stop path has correctness gaps that surface exactly under crisis conditions — restart, IB disconnect, and message replay.

## Status (verified 2026-10-01, KAN-41)

Checked against `origin/main` `a3503fb`. A box is ticked only when shipped code and a test meet it; anything partial stays unticked with the residual named. Finding-level status and the residual register (R1–R7) are in `docs/operations/implementation-review-2026-08-06.md` §12.

## Checklist
- [x] **Latch + persist kill state**; reload on startup so a restart after a kill stays halted (fail-closed), cleared only by an explicit human action. `kill_switch.py:18`, `risk/runner.py:176-211,849-899`
  — *Done:* `2521251`, `7b1d576`, `8894142`; admin-only `DELETE /api/v1/kill` (`40c3f3d`).
- [x] **Fire liquidation on the 20% circuit breaker** — today the decision only rejects a buy. `engine.py:280-289` → `risk/runner.py:358-378`
  — *Done:* `40c3f3d`; `TestCircuitBreakerLiquidation::test_breaker_liquidates_and_halts`.
- [x] **Make kill liquidation idempotent** — deterministic exit ids routed through the OrderLedger; reconcile against in-flight exits before resubmitting. `execution/runner.py:596-601,130-144`
  — *Done:* `6af0219`; oversell guard KAN-10; `test_replayed_kill_does_not_double_submit`.
- [x] **Reload authoritative open positions (DB/broker) at kill time** before emitting exits. `risk/runner.py:867`
  — *Done:* `6af0219`, KAN-6 (reads the DB ledger; the KAN-10 guard checks the broker position).
- [x] **Always publish a kill alert**, and guard each ticker's liquidation so one failure (e.g. IB disconnected) doesn't abort the rest. `execution/runner.py:588-620`
  — *Done:* `6af0219`, `1d0d042`; `TestKillHandling::test_kill_per_ticker_failure_continues_and_alerts`.

## Acceptance criteria
- [x] A restart after a kill remains halted until an explicit human clear.
- [x] A replayed kill message does not double-submit exits (no accidental short).
- [x] A simulated IB-disconnect kill still emits a critical alert and attempts every position.
- [x] A circuit-breaker breach triggers liquidation, not just a buy-pause.

## Dependencies
- Touches `execution/runner.py` (coordinate with **T7**).
- Land **T2**'s drawdown-on-equity fix so the breaker reads real drawdown.
