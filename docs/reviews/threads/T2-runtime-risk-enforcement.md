# T2 — Runtime risk enforcement (periodic driver)  [P0]

Part of the 2026-08-06 implementation review (`docs/operations/implementation-review-2026-08-06.md`, Theme 2 + § 9). Tracking issue linked via this PR's "Closes #…".

## Problem
Several IPS § 6 limits exist in code and tests but are **never invoked on the live path** — the config knobs are inert.

## Status (verified 2026-10-01, KAN-41)

Checked against `origin/main` `a3503fb`. A box is ticked only when shipped code and a test meet it; anything partial stays unticked with the residual named. Finding-level status and the residual register (R1–R7) are in `docs/operations/implementation-review-2026-08-06.md` §12.

## Checklist
- [x] **Drive `run_stop_loss_check` on a periodic task** (reuse `passive_scan_interval_minutes`); refresh prices before evaluating. Gives an independent, intraday stop instead of relying only on the once-daily EOD sleeve exit. `risk/runner.py:931-975`
  — *Done:* `9716eb0`, ledger-routed by KAN-7. The "refresh" reloads DB daily closes, so the stop is periodic but not intraday — residual R1.
- [ ] **Drive `run_passive_scan`** — hard-ceiling auto-trim + margin-critical trim. `risk/runner.py:901-929`
  — *Partial:* driven and the hard-ceiling trim works (`9716eb0`, `test_hard_ceiling_breach_auto_trims_to_soft`); margin utilisation is still hard-coded `0.0`, so the margin trim cannot fire — accepted deferral R2.
- [x] **Measure drawdown on marked book equity** (cash + MTM positions), not `deployable_capital` (pinned at the USD cap, so it reads ~0%). `risk/runner.py:674,684-693`, `capital.py:74-82`
  — *Done:* `9716eb0`, USD equity columns KAN-44; `TestDrawdownOnBookEquity::test_book_equity_engages_circuit_breaker`.
- [ ] **Reconcile IPS § 6 with reality** — either wire the remaining limits or amend the IPS to state what is actually enforced.
  — *Open (owner: operator — the IPS is a governance document).* § 6 was amended once, but `investment-policy-statement.md:248` still says stop-loss/trim exits are "not yet routed through the ledger" (KAN-7 did that), `:263,:267` still read "emit (T2); executes with T1", and it does not mention the broker stops (KAN-19/20) or that they are off.

## Acceptance criteria
- [ ] An intraday stop fires without waiting for the daily run. — *Not met in production:* marks are daily closes and `broker_stops_enabled: false` until KAN-33 (R1).
- [x] A position exceeding the hard ceiling is auto-trimmed to soft.
- [x] The drawdown gauge tracks real equity and the 10% pause / 20% breaker engage on a real drawdown.
- [ ] IPS § 6 table matches enforced code. — *Open;* see the last checklist item.

## Dependencies
- Pairs with **T1** (breaker → liquidate needs a real drawdown reading).
