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

`services/api/auth.py` tracks failed `X-API-Key` attempts and returns `429`
after `LOCKOUT_MAX_FAILURES` (5) failures within `LOCKOUT_WINDOW_SECONDS`
(60s), for `LOCKOUT_DURATION_SECONDS` (300s). Availability of the kill
switch (admin-gated by this same key) outranks brute-force resistance for a
real-money system, which shapes three deliberate choices in
`get_current_user()`:

- **Validity is checked before lockout state.** A request presenting a
  genuinely valid key always succeeds, regardless of how many unrelated
  failures came from the same address — a valid key can never be 429'd.
- **A missing `X-API-Key` header is never counted as a failure.** It isn't
  a guess; only a request presenting a specific *wrong* key accumulates
  toward lockout.
- **The lockout bucket is `(client address, wrong-key prefix)`, not address
  alone.** One attacker guessing many different wrong keys from one address
  only ever fills the bucket for each specific wrong guess — it can't lock
  out a different client sharing that address (e.g. behind the reverse
  proxy above) who happens to present a different key.

`_LockoutTracker` is bounded (`LOCKOUT_MAX_TRACKED_KEYS`, default 10,000):
stale buckets (no failures left in the window, not currently locked) are
swept on every failure, and the tracker evicts the least-recently-active
bucket if the cap is exceeded — an attacker can't grow this structure
without bound by hitting many distinct addresses/keys.

This is in-process, per-instance state — fine for the single-process
deployment this repo documents. If the API is ever scaled to multiple
instances, this needs to move to a shared store (Redis) or it stops being
effective (each instance gets its own budget).

**Proxy IPs**: `request.client.host` (used to build the lockout bucket) is
the *direct* TCP peer — behind the reverse proxy this doc recommends for
TLS, that's the proxy's own address, so every real client would share one
bucket unless uvicorn is told to trust the proxy's `X-Forwarded-For`
header. `services/api/runner.py` reads `API_FORWARDED_ALLOW_IPS` from the
environment and passes it to uvicorn's `forwarded_allow_ips` /
`proxy_headers`; set it to the proxy's actual address once one is in front
of the API. Leaving it unset (the default) means uvicorn does not trust
forwarded headers at all — safe but means every client behind a proxy
shares one lockout bucket until this is configured.

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

- `.github/workflows/security.yml` runs `pip-audit` against **both**
  `requirements.lock` (what every service container actually installs —
  see below) and `requirements-dev.lock` (what CI/local dev installs) on
  every push/PR and weekly on a schedule, plus a `lockfile-freshness` job
  that fails CI if either lockfile has drifted from `pyproject.toml`.
- `.github/dependabot.yml` opens weekly PRs bumping pinned versions in
  `pyproject.toml` (pip ecosystem), the per-service Dockerfiles (docker
  ecosystem), and the GitHub Actions workflow itself.
- **All 9 Dockerfiles (root `Dockerfile` + one per service) install from
  `requirements.lock`, not floating `pyproject.toml` ranges:**
  `COPY pyproject.toml requirements.lock ./` followed by
  `pip install --no-deps -r requirements.lock && pip install --no-deps .`
  — every dependency comes from the pinned lockfile (no resolver-driven
  version drift between builds), and the final `pip install --no-deps .`
  only registers the local `algo-poc` package/entry points without
  re-resolving anything. Without this, `pip-audit` could certify
  `requirements.lock` clean while the containers that actually run in
  production still float on whatever `pyproject.toml`'s `>=` ranges
  resolve to at build time — auditing a file nothing installs from.

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

`services/ml_model/registry.py` records a `sha256` of every saved model on
its `ModelVersion.content_hash` DB column (migration
`e7a1c4d92f3b_add_model_version_content_hash`) and verifies it before
`joblib.load` in `load_active()` — `joblib.load` deserializes arbitrary
Python objects, so a modified model file is a code-execution risk, not
just a "wrong predictions" risk. A missing or mismatched hash raises
`ModelIntegrityError` and the load is refused.

The reference hash deliberately lives in Postgres, **not** a filesystem
sidecar next to the model file. An earlier version of this check stored
the hash as `<version>.joblib.sha256` in the same directory as the model —
that fails against the threat it names: an attacker (or process) with
filesystem write access to `model_dir` can rewrite the sidecar in the same
operation as the model file, since both live in the same trust domain
(same directory, same process, same permissions). The DB row requires
separate Postgres credentials to forge, which is the actual security
boundary this check needs. The migration is additive-only (one nullable
column) and does not touch the loader-consolidation work planned for T8.

### Model integrity — rollout sequence

**This migration is not safe to deploy by itself.** After
`alembic upgrade head`, every existing `ModelVersion` row — including
whichever one is currently active — has `content_hash = NULL`.
`ModelRegistry.load_active()` fails closed on a NULL `content_hash` by
design (a missing integrity record is refused, not silently trusted), so
the very next `load_active()` call after the migration lands raises
`ModelIntegrityError` and the ml_model service cannot get a model until an
operator backfills the column.

Required sequence, in order:

1. `alembic upgrade head` — adds the `content_hash` column (this migration,
   `e7a1c4d92f3b`).
2. `python -m scripts.ops.backfill_model_hashes` — **dry run first**, review
   the report (it lists every row it would write a hash for, and separately
   flags any row whose model file is no longer on disk — those are skipped,
   not guessed at). Then:
   `python -m scripts.ops.backfill_model_hashes --apply` to actually persist
   the computed hashes. `--db-url` overrides the default
   (`AppConfig.database.url` from `config/default.yaml`) if needed.
3. **Verify** before considering the rollout complete: restart the
   ml_model service (or call `ModelRegistry.load_active()` directly) and
   confirm it loads the active model without raising `ModelIntegrityError`.

This is an operator tool — nothing in this repo runs it automatically, and
it must never be pointed at a real database by an agent. It is tested only
against ephemeral sqlite files (`tests/ops/test_backfill_model_hashes.py`).
