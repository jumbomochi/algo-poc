# API Security Posture

This doc covers the parts of T9 (security & supply-chain hardening) that are
deployment-level rather than code: TLS, secrets storage, and how the pieces
that *are* in code (docs gating, auth lockout, live-mode guard) fit together.
See `docs/reviews/threads/T9-security-hardening.md` for the checklist this
implements.

## TLS enforcement

The FastAPI app (`services/api/app.py`) does not terminate TLS itself —
Uvicorn is run in plain HTTP inside the container (see
`services/api/Dockerfile`, `docker-compose.yml`). TLS is a deployment-level
concern:

- **Local/paper (current state):** the API is reached over
  `host.docker.internal` / localhost only. No TLS is in place because
  nothing crosses an untrusted network. This is acceptable only as long as
  the API is not exposed beyond localhost/loopback — do not port-forward it
  to the public internet without the reverse proxy below.
- **Any deployment reachable off a single trusted host (including the live
  cutover):** put a TLS-terminating reverse proxy in front of the API
  (nginx, Caddy, or the cloud provider's load balancer) and only allow the
  API container to accept connections from that proxy (bind to
  `127.0.0.1`/the docker bridge, not `0.0.0.0`). Do not add certificate
  handling inside `services/api/app.py` — that's a second, worse place for
  TLS bugs to live.

There is no code flag for this because there is nothing in-process to flip;
it's an operator responsibility. Track it in the go-live checklist
(`docs/operations/go-live-checklist.md`) before any live network exposure.

## Interactive docs are disabled in live mode

`create_app(mode=...)` (`services/api/app.py`) sets `docs_url`, `redoc_url`,
and `openapi_url` to `None` when the resolved mode is `"live"`. Swagger UI /
ReDoc / the raw OpenAPI schema expose the full route and payload surface —
useful during paper/backtest development, not something to hand to anyone
who can reach a live-money API.

Mode resolution (`services/api/auth.resolve_mode()`) reads the validated
`AppConfig.mode`, the same source of truth used for the API-key live-mode
guard — not a separate, driftable env-var check.

## Auth: X-API-Key lockout

`services/api/auth.py` tracks failed `X-API-Key` attempts per client
address (`_LockoutTracker`) and returns `429` after
`LOCKOUT_MAX_FAILURES` (5) failures within `LOCKOUT_WINDOW_SECONDS` (60s),
for `LOCKOUT_DURATION_SECONDS` (300s). This is in-process, per-instance
state — fine for the single-process deployment this repo documents. If the
API is ever scaled to multiple instances, this needs to move to a shared
store (Redis) or it stops being effective (each instance gets its own
budget of 5 failures).

## Secrets: `.env` today, a real secrets manager later

Today, all service credentials (`API_KEYS`, IB Gateway settings, Telegram
tokens, etc.) live in a single gitignored `.env` file read by docker
compose. Two hardening steps, in order of effort:

1. **File permissions (done — tooling, not enforcement):**
   `scripts/ops/check_env_permissions.py` checks that `.env` is `0600`
   (owner read/write only) and can fix it (`--fix`). This is not run
   automatically against the operator's real `.env` — run it yourself:

   ```bash
   python -m scripts.ops.check_env_permissions          # report only
   python -m scripts.ops.check_env_permissions --fix     # chmod 600
   ```

   Consider adding this as a pre-flight step alongside the other go-live
   checks in `docs/operations/go-live-checklist.md`.

2. **Move off plaintext `.env` (not done — tracked here for the next
   thread that touches secrets):** a real secrets manager (1Password CLI,
   AWS/GCP secrets manager, or at minimum the OS keychain via something
   like `keyring`) removes the "readable by anyone who can read the
   filesystem" failure mode entirely, and gives rotation/audit history for
   free. This is a bigger lift (every service's config loading would need
   to check the keychain before falling back to env vars) and is out of
   scope for this hardening pass — `.env` + tightened permissions is the
   interim state.

## Dependency supply chain

- `requirements.lock` / `requirements-dev.lock` are generated from
  `pyproject.toml` via `uv pip compile` (see the header comment in each
  file for the exact command). Regenerate after any `pyproject.toml`
  dependency change:

  ```bash
  uv pip compile pyproject.toml -o requirements.lock
  uv pip compile pyproject.toml --extra dev -o requirements-dev.lock
  ```

- `.github/workflows/security.yml` runs `pip-audit` against
  `requirements-dev.lock` on every push/PR and weekly on a schedule, and a
  `lockfile-freshness` job that fails CI if either lockfile has drifted
  from `pyproject.toml`.
- `.github/dependabot.yml` opens weekly PRs bumping pinned versions in
  `pyproject.toml` (pip ecosystem), the per-service Dockerfiles (docker
  ecosystem), and the GitHub Actions workflow itself.

Note: these are the first GitHub Actions workflows in this repo — there was
no prior CI. `security.yml` covers T9's supply-chain scope only; a general
test/lint CI workflow is not part of this thread.

## Message schema versioning

`shared/schemas/messages.py` adds `schema_version` to every stream message
(`StreamSerializable`), defaulting to `CURRENT_SCHEMA_VERSION`. Evolution
rule, additive-only:

- New optional fields with defaults: no version bump needed.
- Removing/renaming/retyping a field: bump `CURRENT_SCHEMA_VERSION`.
- A message whose `schema_version` is *higher* than what this codebase
  understands fails Pydantic validation (`ValidationError`) rather than
  being silently misparsed.

This intentionally only adds the field and the validation. Runner code
already catches `ValidationError` from `*.from_stream_dict()` and routes it
to `stream:<name>:dlq` (see `services/execution/runner.py`,
`services/risk_management/runner.py`, `services/portfolio_accounting/runner.py`)
— that dead-letter plumbing is T4's scope, not rebuilt here.

## Model integrity

`services/ml_model/registry.py` writes a `sha256` sidecar
(`<version>.joblib.sha256`) next to every saved model and verifies it
before `joblib.load` in `load_active()` — `joblib.load` deserializes
arbitrary Python objects, so a modified model file is a code-execution
risk, not just a "wrong predictions" risk. A missing or mismatched hash
raises `ModelIntegrityError` and the load is refused. This is intentionally
a plain sidecar file, not a new `ModelVersion` database column — no schema
migration needed, and it stays out of the way of the loader-consolidation
work planned for T8.
