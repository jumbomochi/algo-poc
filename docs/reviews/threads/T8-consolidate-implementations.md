# T8 — Consolidate dual implementations + model loader  [P2]

Part of the 2026-08-06 implementation review (`docs/operations/implementation-review-2026-08-06.md`, Theme 7 + § 9). Tracking issue linked via this PR's "Closes #…".

## Problem
`run_paper.py` (the live signal brain) and the Docker `signal_generation`/`ml_model` services are two parallel signal systems, and risk/execution safety logic is duplicated with the microservice versions partially dead. This ambiguity is *why* the unwired-safety bugs (T1/T2) hid — three reviewers couldn't tell which path was authoritative.

## Checklist
- [ ] **Pick a single source of truth per control**; delete or clearly demote the dormant path (`signal_generation`/`ml_model`) if `run_paper.py` is authoritative.
- [ ] **Fix the model-loader mismatch** — `registry.load_active` does `joblib.load` of a `.joblib`, but `retrain_model.py` writes LightGBM `.txt`; these can't interop. `registry.py:44,77`, `retrain_model.py:173`
- [ ] **Document the live topology** in-repo (`run_paper.py` → `stream:recommendations` → docker `risk_management`/`execution`/`portfolio_accounting`).

## Acceptance criteria
- Exactly one documented path per responsibility; no dead safety code masquerading as active.
- The live model loads through one loader end-to-end.
- A new contributor can read one doc and know what runs live.

## Dependencies
- Best done after T1/T2/T4 land (so the "keep" path is the hardened one).
