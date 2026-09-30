# T3 — Message-bus lockdown  [P0]

Part of the 2026-08-06 implementation review (`docs/operations/implementation-review-2026-08-06.md`, Theme 5 + § 9). Tracking issue linked via this PR's "Closes #…".

> Security thread — kept high-level here. Detailed rationale is in the operator's private security note, not this public repo.

## Problem
The message bus (Redis) and database ship with open defaults in the **committed** compose; only a gitignored local override hardens them, so a fresh clone or redeploy starts unhardened.

## Status (verified 2026-10-01, KAN-41)

Checked against `origin/main` `a3503fb`. A box is ticked only when shipped code and a test meet it; anything partial stays unticked with the residual named. Finding-level status and the residual register (R1–R7) are in `docs/operations/implementation-review-2026-08-06.md` §12.

## Checklist
- [x] **Set Redis auth** (`requirepass` / ACL; per-service credentials where practical). `docker-compose.yml`, `redis_client.py`
  — *Done:* `c7ba601` `--requirepass` (one shared credential; per-service is the stretch item).
- [x] **Set a strong, generated Postgres password** (not the default). `docker-compose.yml`
- [x] **Bind host ports to `127.0.0.1` in the committed compose** — not only in the gitignored override. `docker-compose.yml:8-10,21-22,139-140`
- [x] **Ship a checked-in `docker-compose.override.yml.example`** documenting the loopback requirement.
- [ ] (Stretch) Add per-service Redis ACLs (publish-only vs read-only) and an integrity check on the money streams (`approved_orders`, `kill`).
  — *Deferred* (finding 5.2); tracked in `TODOS.md`.

## Acceptance criteria
- [x] A fresh clone starts hardened (auth required, loopback-bound) with no manual override. — `tests/deploy/test_message_bus_lockdown.py`
- [x] Redis and Postgres both require credentials.
- [x] No production data services are reachable off-host by default.

## Dependencies
- None. Independent of the other threads.
