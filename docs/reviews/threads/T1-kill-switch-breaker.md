# T1 — Kill-switch & circuit-breaker state machine  [P0]

Part of the 2026-08-06 implementation review (`docs/operations/implementation-review-2026-08-06.md`, Theme 1 + § 9). Tracking issue linked via this PR's "Closes #…".

## Problem
The emergency-stop path has correctness gaps that surface exactly under crisis conditions — restart, IB disconnect, and message replay.

## Checklist
- [ ] **Latch + persist kill state**; reload on startup so a restart after a kill stays halted (fail-closed), cleared only by an explicit human action. `kill_switch.py:18`, `risk/runner.py:176-211,849-899`
- [ ] **Fire liquidation on the 20% circuit breaker** — today the decision only rejects a buy. `engine.py:280-289` → `risk/runner.py:358-378`
- [ ] **Make kill liquidation idempotent** — deterministic exit ids routed through the OrderLedger; reconcile against in-flight exits before resubmitting. `execution/runner.py:596-601,130-144`
- [ ] **Reload authoritative open positions (DB/broker) at kill time** before emitting exits. `risk/runner.py:867`
- [ ] **Always publish a kill alert**, and guard each ticker's liquidation so one failure (e.g. IB disconnected) doesn't abort the rest. `execution/runner.py:588-620`

## Acceptance criteria
- A restart after a kill remains halted until an explicit human clear.
- A replayed kill message does not double-submit exits (no accidental short).
- A simulated IB-disconnect kill still emits a critical alert and attempts every position.
- A circuit-breaker breach triggers liquidation, not just a buy-pause.

## Dependencies
- Touches `execution/runner.py` (coordinate with **T7**).
- Land **T2**'s drawdown-on-equity fix so the breaker reads real drawdown.
