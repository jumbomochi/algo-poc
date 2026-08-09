# T9 — Security & supply-chain hardening  [P2]

Part of the 2026-08-06 implementation review (`docs/operations/implementation-review-2026-08-06.md`, Theme 5 + § 9). Tracking issue linked via this PR's "Closes #…".

> Security thread — kept high-level here. Detailed rationale is in the operator's private security note, not this public repo.

## Checklist
- [ ] **Integrity-check model loads** — record a content hash/signature at save time and verify before `joblib.load` (untrusted-deserialization risk). `registry.py:77`
- [ ] **Live-mode guard from validated config** — derive it from `AppConfig.mode`, not a raw env var, so it can't be bypassed at the live cutover. `api/auth.py:23-61`
- [ ] **Secret handling** — tighten `.env` permissions; move toward an OS keychain / secrets manager.
- [ ] **Supply chain** — commit a dependency lockfile (`uv`/`pip-compile`) and add `pip-audit`/Dependabot.
- [ ] **Message schema versioning** — add a `schema_version` field + an additive-only evolution rule; treat validation failures as DLQ-worthy (see T4). `schemas/messages.py`
- [ ] **API hardening** — rate-limit / lock out `X-API-Key` failures, enforce TLS, disable interactive docs outside dev. `api/app.py`, `api/auth.py`

## Acceptance criteria
- Model loads are integrity-checked.
- The live guard cannot fall back to development credentials.
- Dependencies are pinned and scanned; stream messages are versioned.

## Dependencies
- `schema_version` coordinates with **T4** (DLQ-on-validation-failure).
