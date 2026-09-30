# T9 — Security & supply-chain hardening  [P2]

Part of the 2026-08-06 implementation review (`docs/operations/implementation-review-2026-08-06.md`, Theme 5 + § 9). Tracking issue linked via this PR's "Closes #…".

> Security thread — kept high-level here. Detailed rationale is in the operator's private security note, not this public repo.

## Status (verified 2026-10-01, KAN-41)

Checked against `origin/main` `a3503fb`. A box is ticked only when shipped code and a test meet it; anything partial stays unticked with the residual named. Finding-level status and the residual register (R1–R7) are in `docs/operations/implementation-review-2026-08-06.md` §12.

## Checklist
- [x] **Integrity-check model loads** — record a content hash/signature at save time and verify before `joblib.load` (untrusted-deserialization risk). `registry.py:77`
- [x] **Live-mode guard from validated config** — derive it from `AppConfig.mode`, not a raw env var, so it can't be bypassed at the live cutover. `api/auth.py:23-61`
- [x] **Secret handling** — tighten `.env` permissions; move toward an OS keychain / secrets manager.
  — *Done:* the launchd jobs read the macOS login keychain (KAN-16, `0eee862`); `.env` is read by nothing.
- [x] **Supply chain** — commit a dependency lockfile (`uv`/`pip-compile`) and add `pip-audit`/Dependabot.
  — *Done:* `250a9df`; deterministic lockfile check KAN-36; guardrail KAN-57; pip-audit is a required check.
- [x] **Message schema versioning** — add a `schema_version` field + an additive-only evolution rule; treat validation failures as DLQ-worthy (see T4). `schemas/messages.py`
- [ ] **API hardening** — rate-limit / lock out `X-API-Key` failures, enforce TLS, disable interactive docs outside dev. `api/app.py`, `api/auth.py`
  — *Partial:* key lockout and docs-off-outside-dev done (`250a9df`); TLS is documented as a deployment concern (`api-security.md`), not enforced by the app.

## Acceptance criteria
- [x] Model loads are integrity-checked.
- [x] The live guard cannot fall back to development credentials.
- [x] Dependencies are pinned and scanned; stream messages are versioned.

## Dependencies
- `schema_version` coordinates with **T4** (DLQ-on-validation-failure).
