# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Build Commands

```bash
# Install project in editable mode with dev dependencies
pip install -e ".[dev]"

# First time only: Postgres/Redis require credentials (T3 message-bus
# lockdown) — docker compose refuses to start without them.
cp .env.example .env  # then fill in POSTGRES_PASSWORD / REDIS_PASSWORD

# Build and start all services via Docker
docker compose up

# Rebuild Docker images after code changes
docker compose build

# Start with observability stack (Prometheus + Grafana + Alertmanager).
# Needs GRAFANA_ADMIN_PASSWORD in .env. Alertmanager delivers Prometheus
# alerts to Telegram independently of the notifications service, so it also
# needs TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID and refuses to start without
# them rather than coming up as a silent monitor.
docker compose -f docker-compose.yml -f docker-compose.observability.yml up
```

## Test Commands

```bash
# Run the full test suite
pytest

# Run tests for a specific service
pytest tests/services/risk_management/ -v
pytest tests/services/data_ingestion/ -v

# Run backtest tests
pytest tests/backtest/ -v

# Run shared module tests
pytest tests/shared/ -v

# Run with coverage
pytest --cov=shared --cov=services
```

## Architecture

algo-poc is an automated US equities trading bot built as a set of Python microservices connected by Redis Streams.

### Data flow

```
data_ingestion -> signal_generation -> ml_model -> risk_management -> execution
                                                                         |
                                                                    notifications
                                                                         |
                                                                        api
```

### Infrastructure

- **PostgreSQL 16** — persistent storage for market data, positions, orders, ML models
- **Redis 7** — message bus (Redis Streams) for inter-service communication
- **Alembic** — database schema migrations (run via `alembic upgrade head`)

### Services

| Service | Description | Subscribes | Publishes |
|---|---|---|---|
| `data_ingestion` | Fetches market data, fundamentals, and events from IB/external sources | — | `stream:market_data`, `stream:fundamentals`, `stream:events` |
| `signal_generation` | Computes technical, fundamental, and event signals; detects staleness | `stream:market_data`, `stream:fundamentals`, `stream:events` | `stream:signals` |
| `ml_model` | Assembles features, trains LightGBM model, generates buy/hold/sell recommendations | `stream:signals` | `stream:recommendations` |
| `risk_management` | Entry controls, stop-loss, drawdown, kill switch, correlation monitoring | `stream:recommendations`, `stream:kill` | `stream:approved_orders`, `stream:alerts` |
| `execution` | Manages IB orders, handles fills, repricing, and kill liquidation | `stream:approved_orders`, `stream:kill` | `stream:fills`, `stream:alerts` |
| `notifications` | Routes alerts to Slack, email, and SMS channels by priority | `stream:alerts` | — |
| `api` | FastAPI REST API for monitoring, control, and backtest triggering | — (reads DB directly) | `stream:kill` (via kill endpoint) |

### Shared modules (`shared/`)

- `config.py` — YAML + env-var configuration loading
- `models/` — SQLAlchemy ORM models
- `schemas/` — Pydantic schemas for stream messages and API payloads
- `redis_client.py` — Redis Streams client with consumer groups and dead-letter queues
- `logging.py` — Structured JSON logging via structlog
- `market_calendar.py` — NYSE trading calendar helpers
- `observability.py` — Prometheus metrics helpers (counters, histograms, gauges)

## Configuration

### Config file

Edit `config/default.yaml` to change default settings. Key sections:

- `mode` — `paper`, `live`, or `backtest`
- `universe` — watchlist source and custom tickers
- `data_ingestion` — polling intervals, rate limits, backfill years
- `signals` — staleness thresholds
- `ml_model` — retraining cadence, target buckets, regime detection
- `risk` — position limits, stop-loss, drawdown thresholds, margin alerts
- `execution` — limit order buffers, reprice settings
- `ib` — Interactive Brokers connection settings
- `notifications` — channel enable/disable flags
- `database` / `redis` — connection URLs
- `observability` — Prometheus port, tracing toggle

### Environment variable overrides

Environment variables take precedence over `config/default.yaml`:

| Variable | Config path | Example |
|---|---|---|
| `ALGO_MODE` | `mode` | `paper`, `live`, `backtest` |
| `ALGO_DATABASE_URL` | `database.url` | `postgresql://algo:algo@localhost:5432/algo_poc` |
| `ALGO_REDIS_URL` | `redis.url` | `redis://localhost:6379/0` |

## Secrets — 1Password is the source of truth, `op` is how you read it

**Never ask the user to paste a secret, and never read one out of a file.**
The `op` CLI is installed and signed in; anything you need is already
retrievable:

```bash
op read 'op://Developer/JIRA_API_Token/password'     # the acli/JIRA token
op item list --vault Developer                       # titles only, no values
op read 'op://<vault>/<item>/<field>'
```

Vaults: `Private`, `Developer`, `Personal`, `Shared`, `Work`. Use
`op item get <title> --format json` to find a field name rather than guessing —
`JIRA_API_Token` is a LOGIN whose token is in `password`, not `credential`.

Three layers, and confusing them is the recurring error:

| where | who reads it | how |
|---|---|---|
| 1Password | humans, and agents on their behalf | `op read` |
| macOS login keychain, service `algo-poc` | the launchd jobs | `deploy/launchd/secrets.sh` |
| `.env` | **nobody** | it is a FIFO — see below |

- **The launchd jobs do NOT read 1Password.** They read the login keychain,
  because a launchd user agent cannot unlock 1Password and a service-account
  token would itself have to be stored somewhere (the reasoning is in
  `deploy/launchd/secrets.sh`'s header, KAN-16). The keychain is a *mirror*;
  1Password is still where the value lives. Re-mirror with
  `deploy/launchd/secrets.sh --import`, and check what is present — names and
  status only, never values — with `deploy/launchd/secrets.sh --check`.
- **`.env` is a named pipe, not a file.** 1Password Environments serves it, so
  `cat .env` blocks ~60s and returns nothing, and `[ -f .env ]` is **false**.
  That combination silently disabled every alert path for two days on
  2026-08-13/14. `secrets.sh` refuses a non-regular `.env` by name and in under
  a second; do not add a code path that reads it.
- **Rotating a secret means both places.** Update the 1Password item, then
  `secrets.sh --import`, or the jobs keep using the old value until the next
  04:15 tells you otherwise.

### Piping `op read` into a tool that prompts

The agent harness runs commands with **stdin closed**, so
`op read ... | acli auth login --token` fails with "failed to read token from
standard input" — the pipe is not the problem, the closed stdin is. Use a
short-lived file and remove it immediately:

```bash
f=$(mktemp); chmod 600 "$f"
op read 'op://Developer/JIRA_API_Token/password' > "$f"
acli jira auth login --site "huiliang.atlassian.net" \
  --email "$EMAIL" --token < "$f"
rm -f "$f"
```

Never echo a secret, never pass one in argv (it is visible to `ps`), and never
write one into a file that is not removed in the same command.

## Code Conventions

- All modules use `from __future__ import annotations`
- Tests use pytest with `asyncio_mode = "auto"`
- Services are structured as `services/<name>/runner.py` with a main runner class
- Stream message schemas live in `shared/schemas/messages.py`
- Each service Dockerfile builds from Python 3.12-slim and sets `ENTRYPOINT ["python", "-m", "services.<name>.runner"]`

## Branch Flow — develop is the integration branch

```
feature branch  ──PR──>  develop  ──PR──>  main
                          (verify)         (production)
```

- **Never commit or push directly to `main` or `develop`.** All work lands
  through a PR. This is a rule you follow, not one the server keeps — see
  "the protection does not bind you" below. Agents must treat it as absolute:
  never push to either branch, whatever the API allows.
- **`main` is production.** The launchd jobs, the paper book, and the live IB
  account all run from what is on `main`. Only promote to `main` from
  `develop`, and only after develop's CI is green on the merged tree.
- **Branch off `develop`, not `main`.** `develop` contains everything `main`
  has plus whatever is awaiting promotion; branching off `main` reintroduces
  conflicts that were already resolved.
- **Promotions to `main` are SQUASH merges, and that has a cost you pay
  deliberately.** A squash creates a commit that exists only on `main`, so
  `main` stops being an ancestor of `develop`. The next promotion PR is then
  unmergeable even when `git diff origin/main origin/develop` is clean, because
  the 3-way merge cannot tell that the two sides made the same changes. The fix
  is a **reconciliation PR** merging `origin/main` into `develop`:

  ```bash
  git merge-base --is-ancestor origin/main origin/develop   # fails => reconcile
  git worktree add .worktrees/reconcile -b chore/reconcile-main-<date> origin/develop
  cd .worktrees/reconcile && git merge origin/main
  ```

  Resolve conflicts in favour of `develop` — it already contains everything
  `main` has, so `main`'s side of any conflict is strictly older, not
  different. Then **verify the merged tree changes nothing**:

  ```bash
  git diff --cached origin/develop     # MUST be empty
  ```

  If it is not empty, stop: something on `main` never reached `develop` and
  resolving toward `develop` would silently drop it.

  Merge that PR with a **merge commit, never a squash** — squashing it would
  flatten the very ancestry link that stops the next promotion conflicting for
  the same reason. Needed twice so far (#157, #164).
- **CI runs on both PRs and branch pushes.** `.github/workflows/tests.yml` and
  `security.yml` build every PR and every push to `main`/`develop`. The
  post-merge build on `develop` is the one that catches two individually-green
  PRs that conflict semantically once both have landed.
- **Required checks on `develop`:** `pytest (full suite)`,
  `pip-audit (dependency vulnerability scan)`,
  `lockfile matches pyproject.toml`, `amtool check-config`. Renaming a job
  renames its check and blocks merges until the protection rule is updated to
  match.
- **The protection does not bind you.** `develop` is protected with
  `enforce_admins: false`, and the repo has no non-admin collaborators — so
  the owner, and any agent holding the owner's token, can still push directly
  and still merge red. This is deliberate: it preserves the escape hatch used
  for the 2026-08-14 KAN-16 secrets outage, where merging red was the right
  call. Treat the rules as binding anyway, and when you do override, record
  the reason in a PR comment. `main` has no protection at all.
  Flip with `gh api -X PUT repos/jumbomochi/algo-poc/branches/develop/protection/enforce_admins`.
- CI depth is unit-level by design (self-contained suite, sqlite in
  `tmp_path`, no service containers). Real Postgres, `alembic upgrade head`
  against it, and `docker compose build` are **not** covered — verify those by
  hand before promoting anything that touches migrations or images.
- **Merging the promotion PR does not deploy it.** The launchd jobs read a
  working tree on the host, so promotion has a second half. Run it **in the
  tree the launchd jobs read** — which `deploy/launchd/README.md` names, and
  which is not necessarily the tree you are developing in — and in this order:

  ```bash
  git merge --ff-only origin/main     # FIRST
  deploy/launchd/deploy.sh --dry-run
  deploy/launchd/deploy.sh
  ```

  `deploy.sh` run against a stale tree reports "everything in sync" and copies
  nothing — true and meaningless (2026-09-08). Note that the two halves of the
  tree behave differently: `secrets.sh`, `deadman.sh`, everything in
  `deploy/launchd/lib/`, and all of `scripts/` and `config/` are **sourced by
  path** and go live the moment the tree is pulled, while `run_*.sh` and
  `gateway_watchdog.sh` are **copies in `~/ibc`** that only move when
  `deploy.sh` runs. `deploy/launchd/README.md` carries the file list on each
  side.

## Destructive Actions — Human Confirmation Required

This repo runs a real trading system with state that cannot be recreated
(paper trading history, the live IB account). Agents must NEVER execute any of
the following on their own — no matter how sound the engineering rationale.
Instead, explain what should be run and why, and ask the user to run it
themselves in their own terminal:

- `scripts/run_paper.py --reset` (wipes all paper trading state)
- Any `DELETE`, `TRUNCATE`, `DROP`, or destructive `UPDATE` against the paper
  or live databases (`algo_poc` on any host/port)
- `docker compose down -v`, `docker volume rm`, or anything else that removes
  the postgres/redis volumes
- Deleting or overwriting files under `output/`, `~/ibc/logs/`, or
  `~/ibc/backups/`
- `launchctl bootout` / disabling the launchd jobs
- Placing, modifying, or cancelling orders on a LIVE (`U*`-prefixed) IB account

Bypassing an interactive confirmation prompt (e.g. `echo yes | ...`, `yes |`,
`--force`, or here-docs) counts as executing the destructive action and is
prohibited. Interactive prompts exist precisely so a human makes the call.
This rule was added after an agent piped `yes` into `run_paper.py --reset` on
2026-07-10 and wiped the paper book without authorization.

## Skill routing

When the user's request matches an available skill, invoke it via the Skill tool. When in doubt, invoke the skill.

Key routing rules:
- Product ideas/brainstorming → invoke /office-hours
- Strategy/scope → invoke /plan-ceo-review
- Architecture → invoke /plan-eng-review
- Design system/plan review → invoke /design-consultation or /plan-design-review
- Full review pipeline → invoke /autoplan
- Bugs/errors → invoke /investigate
- QA/testing site behavior → invoke /qa or /qa-only
- Code review/diff check → invoke /review
- Visual polish → invoke /design-review
- Ship/deploy/PR → invoke /ship or /land-and-deploy
- Save progress → invoke /context-save
- Resume context → invoke /context-restore
- Author a backlog-ready spec/issue → invoke /spec
