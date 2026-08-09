# T2 — Runtime risk enforcement (periodic driver)  [P0]

Part of the 2026-08-06 implementation review (`docs/operations/implementation-review-2026-08-06.md`, Theme 2 + § 9). Tracking issue linked via this PR's "Closes #…".

## Problem
Several IPS § 6 limits exist in code and tests but are **never invoked on the live path** — the config knobs are inert.

## Checklist
- [ ] **Drive `run_stop_loss_check` on a periodic task** (reuse `passive_scan_interval_minutes`); refresh prices before evaluating. Gives an independent, intraday stop instead of relying only on the once-daily EOD sleeve exit. `risk/runner.py:931-975`
- [ ] **Drive `run_passive_scan`** — hard-ceiling auto-trim + margin-critical trim. `risk/runner.py:901-929`
- [ ] **Measure drawdown on marked book equity** (cash + MTM positions), not `deployable_capital` (pinned at the USD cap, so it reads ~0%). `risk/runner.py:674,684-693`, `capital.py:74-82`
- [ ] **Reconcile IPS § 6 with reality** — either wire the remaining limits or amend the IPS to state what is actually enforced.

## Acceptance criteria
- An intraday stop fires without waiting for the daily run.
- A position exceeding the hard ceiling is auto-trimmed to soft.
- The drawdown gauge tracks real equity and the 10% pause / 20% breaker engage on a real drawdown.
- IPS § 6 table matches enforced code.

## Dependencies
- Pairs with **T1** (breaker → liquidate needs a real drawdown reading).
