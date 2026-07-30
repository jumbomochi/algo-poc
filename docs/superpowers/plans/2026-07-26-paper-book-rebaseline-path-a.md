# Paper Book Re-baseline (Path A: Flatten & Re-baseline) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring the durable paper book and the live IB paper account to a clean, mutually-reconciled *empty* baseline, then re-enable paper entries so the durable ledger rebuilds from real fills — clearing go-live **Gate 6 (data integrity / reconciliation)** and unblocking **Gate 4 (execution quality)**.

**Architecture:** The durable `positions` table holds 33 legacy `run_paper.py` *simulation* rows with `account_id = NULL` and `con_id = NULL`; the IB paper account (`DUN551088`) holds ~3 orphan positions with no local fill/intent provenance (`execution_fills` and `order_intents` are empty). These are unrelated books, so they cannot be *reconciled* — only *re-baselined*. Path A retires the legacy DB rows (new guarded tool) and flattens the IB positions (operator), leaving both books empty and `reconciliation: ok`. Entries are then re-enabled behind the **already-existing** fail-closed reconciliation guard.

**Tech Stack:** Python 3.14, SQLAlchemy 2.x, Alembic, pytest (in-memory SQLite), ib_insync, PostgreSQL 16 (dockerized, `localhost:55432`).

## Global Constraints

- **The agent NEVER executes destructive actions.** Closing/updating paper positions, placing/flattening IB orders, applying repairs, or flipping to live are **operator-run, human-gated** steps (see `CLAUDE.md` "Destructive Actions"). The agent writes and unit-tests tooling; the human runs it against the paper DB / broker.
- **No confirmation-prompt bypassing** (`yes |`, `echo yes |`, `--force`, here-docs into prompts) — prohibited; counts as executing the destructive action.
- **Additive & guarded only.** Tooling defaults to dry-run; every apply requires an interactive TTY and an exact typed confirmation, mirroring `scripts/reconcile_paper.py`.
- **Live account (`U*`) is never touched.** Paper (`DU*`) only. Mode stays `paper` throughout.
- **DB env vars are mandatory for every command:** `ALGO_DATABASE_URL="postgresql://algo:algo@localhost:55432/algo_poc"` and `ALGO_REDIS_URL="redis://localhost:56379/0"`; use `.venv/bin/python` (never conda).
- **A verified DB backup (`~/ibc/logs/db_backup_YYYYMMDD.log` == `Backup OK`) must exist before any apply.**
- Reconciliation severity is **binary**: `severity = "major" if discrepancies else "ok"`; `entries_allowed = (severity == "ok" and not discrepancies)`. There is no partial state.

---

### Task 1: Capture the authoritative pre-state (read-only)

**Files:**
- No source changes. Read-only DB queries + one read-only IB snapshot + a report-only reconciliation.

**Interfaces:**
- Consumes: `scripts.run_paper.read_broker_snapshot(...)`, `scripts.reconcile_paper.reconcile_snapshot(session, snapshot)` (report-only; persists an audit report, mutates no positions).
- Produces: a recorded pre-state (counts + the exact IB positions) used to size the confirmation in Task 3.

- [ ] **Step 1: Confirm a fresh backup exists**

Run: `ls -t ~/ibc/logs/db_backup_*.log | head -1 | xargs grep -l "Backup OK"`
Expected: today's backup log path prints. If not, the operator runs the backup job first.

- [ ] **Step 2: Record the legacy DB rows (read-only)**

Run:
```bash
PGPASSWORD=algo psql -h localhost -p 55432 -U algo -d algo_poc -c \
"SELECT count(*) AS legacy_open, count(*) FILTER (WHERE account_id IS NULL) AS unowned, count(*) FILTER (WHERE con_id IS NULL) AS no_conid FROM positions WHERE status='open';"
```
Expected (current): `legacy_open = 33`, `unowned = 33`, `no_conid = 33`. Record the exact number — it is the confirmation token in Task 3.

- [ ] **Step 3: Record the IB paper positions (read-only, requires Gateway up on 7497)**

Run (only when `nc -z 127.0.0.1 7497` succeeds):
```bash
ALGO_DATABASE_URL="postgresql://algo:algo@localhost:55432/algo_poc" .venv/bin/python - <<'PY'
import asyncio
from scripts.run_paper import read_broker_snapshot
async def main():
    s = await read_broker_snapshot(host="127.0.0.1", port=7497, client_id=97,
        mode="paper", expected_base_currency="SGD", trading_currency="USD")
    print(f"positions={len(s.positions)} open_orders={len(s.open_orders)}")
    for cid, p in s.positions.items():
        print(f"  con_id={cid} {p.symbol} qty={p.quantity} avg={p.average_cost} {p.currency}")
asyncio.run(main())
PY
```
Expected: ~3 positions listed with tickers/quantities, and `open_orders=0` (if not zero, the operator cancels resting orders before flattening in Task 3). **Record these — they are the flatten target.**

---

### Task 2: Build the guarded legacy-position retirement tool

**Files:**
- Create: `scripts/ops/retire_legacy_positions.py`
- Test: `tests/ops/test_retire_legacy_positions.py`

**Why new tooling (not the existing repair):** `scripts/reconcile_paper.py:242-244` **refuses** any repair whose target overlaps an unowned legacy position (`RepairRefusedError("repair target overlaps an unowned legacy position")`). Retiring the legacy rows is therefore out of scope for the repair command by design and needs a dedicated, one-purpose tool.

**Interfaces:**
- Produces: `retire_legacy_positions(session, *, apply, confirm) -> RetireSummary`, where
  `RetireSummary = dataclass(count: int, rows: list[tuple[str, str, float]])` (ticker, portfolio, quantity).
  - Selects only `Position` rows with `account_id IS NULL AND status == "open"`.
  - `apply=False` (default): mutates nothing; returns the summary.
  - `apply=True`: requires `confirm == str(count)` (exact typed count) else raises `RetireRefusedError`; within one transaction sets `status="closed"` and `closed_at=datetime.now(timezone.utc)` for each selected row, leaving `account_id`/`con_id` untouched (audit trail), then commits once.
  - Never selects or mutates any row with a non-null `account_id`.
- Consumes: `shared.models.Position`, `shared.config.AppConfig` (for the DB URL when run as a CLI).

- [ ] **Step 1: Write the failing tests**

Create `tests/ops/test_retire_legacy_positions.py`:
```python
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from shared.models import Base, Position
from scripts.ops.retire_legacy_positions import (
    RetireRefusedError,
    retire_legacy_positions,
)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as value:
        yield value


def _pos(session, *, ticker, portfolio, account_id, con_id, status="open"):
    p = Position(
        account_id=account_id, ticker=ticker, portfolio=portfolio, con_id=con_id,
        quantity=1.0, avg_entry_price=1.0, current_price=1.0, peak_price=1.0,
        highest_price_since_entry=1.0, opened_at=datetime.now(timezone.utc),
        status=status,
    )
    session.add(p)
    session.flush()
    return p


def test_dry_run_reports_unowned_open_and_mutates_nothing(session):
    _pos(session, ticker="AMD", portfolio="momentum", account_id=None, con_id=None)
    _pos(session, ticker="NVDA", portfolio="quality_value", account_id=None, con_id=None)
    _pos(session, ticker="AAPL", portfolio="momentum", account_id="DUN551088", con_id=265598)

    summary = retire_legacy_positions(session, apply=False, confirm=None)

    assert summary.count == 2
    assert {r[0] for r in summary.rows} == {"AMD", "NVDA"}
    open_rows = session.scalars(select(Position).where(Position.status == "open")).all()
    assert len(open_rows) == 3  # nothing closed


def test_apply_closes_only_unowned_open_rows(session):
    _pos(session, ticker="AMD", portfolio="momentum", account_id=None, con_id=None)
    _pos(session, ticker="NVDA", portfolio="quality_value", account_id=None, con_id=None)
    owned = _pos(session, ticker="AAPL", portfolio="momentum", account_id="DUN551088", con_id=265598)

    summary = retire_legacy_positions(session, apply=True, confirm="2")

    assert summary.count == 2
    closed = session.scalars(select(Position).where(Position.status == "closed")).all()
    assert {c.ticker for c in closed} == {"AMD", "NVDA"}
    for c in closed:
        assert c.closed_at is not None
        assert c.account_id is None  # audit trail preserved
    session.refresh(owned)
    assert owned.status == "open"  # owned row untouched


def test_apply_refuses_on_wrong_confirmation_and_does_not_mutate(session):
    _pos(session, ticker="AMD", portfolio="momentum", account_id=None, con_id=None)

    with pytest.raises(RetireRefusedError):
        retire_legacy_positions(session, apply=True, confirm="99")

    assert session.scalars(select(Position).where(Position.status == "open")).all()


def test_apply_is_idempotent(session):
    _pos(session, ticker="AMD", portfolio="momentum", account_id=None, con_id=None)
    retire_legacy_positions(session, apply=True, confirm="1")

    summary = retire_legacy_positions(session, apply=True, confirm="0")

    assert summary.count == 0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/ops/test_retire_legacy_positions.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'scripts.ops.retire_legacy_positions'`.

- [ ] **Step 3: Implement the tool**

Create `scripts/ops/retire_legacy_positions.py`:
```python
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.config import load_config
from shared.models import Position


class RetireRefusedError(RuntimeError):
    """Raised when a safety guard blocks the retirement apply."""


@dataclass(frozen=True)
class RetireSummary:
    count: int
    rows: list[tuple[str, str, float]]


def _select_legacy(session: Session) -> list[Position]:
    return list(session.scalars(
        select(Position).where(
            Position.account_id.is_(None),
            Position.status == "open",
        )
    ))


def retire_legacy_positions(
    session: Session, *, apply: bool, confirm: str | None
) -> RetireSummary:
    """Close unowned (`account_id IS NULL`) open legacy positions.

    Dry-run by default. On apply, requires ``confirm == str(count)`` and closes
    the rows in a single committed transaction, leaving account_id/con_id intact
    as an audit trail. Owned positions are never selected or mutated.
    """
    legacy = _select_legacy(session)
    summary = RetireSummary(
        count=len(legacy),
        rows=[(p.ticker, p.portfolio, float(p.quantity)) for p in legacy],
    )
    if not apply:
        return summary
    if any(p.account_id is not None for p in legacy):  # defensive
        raise RetireRefusedError("selection unexpectedly includes an owned position")
    if confirm != str(summary.count):
        raise RetireRefusedError(
            f"exact confirmation required: expected '{summary.count}', got {confirm!r}"
        )
    now = datetime.now(timezone.utc)
    for p in legacy:
        p.status = "closed"
        p.closed_at = now
    session.commit()
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Retire unowned legacy paper positions (Path A re-baseline)."
    )
    parser.add_argument("--apply", action="store_true",
                        help="Close the rows (default: dry-run report only).")
    args = parser.parse_args(argv)

    config = load_config("config/default.yaml")  # applies ALGO_DATABASE_URL env override
    from sqlalchemy import create_engine
    engine = create_engine(config.database.url)
    with Session(engine) as session:
        summary = retire_legacy_positions(session, apply=False, confirm=None)
        print(f"Legacy unowned open positions: {summary.count}")
        for ticker, portfolio, qty in summary.rows:
            print(f"  {ticker:8} {portfolio:18} qty={qty}")
        if not args.apply:
            print("\nDry-run only. Re-run with --apply to close these rows.")
            return 0
        if summary.count == 0:
            print("Nothing to retire.")
            return 0
        if not sys.stdin.isatty():
            raise RetireRefusedError("--apply requires an interactive TTY")
        answer = input(f"\nType {summary.count} to permanently close these rows: ")
        retire_legacy_positions(session, apply=True, confirm=answer.strip())
        print(f"Retired {summary.count} legacy positions.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Create the test package marker if missing**

Run: `test -f tests/ops/__init__.py || : ` — if `tests/ops/` does not exist, create `tests/ops/__init__.py` (empty file).

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/ops/test_retire_legacy_positions.py -q`
Expected: PASS (4 passed).

- [ ] **Step 6: Lint and commit**

Run: `uvx ruff check scripts/ops/retire_legacy_positions.py tests/ops/test_retire_legacy_positions.py`
Expected: no new violations.
```bash
git add scripts/ops/retire_legacy_positions.py tests/ops/test_retire_legacy_positions.py tests/ops/__init__.py
git commit -m "feat: add guarded legacy-position retirement tool (Path A re-baseline)"
```

---

### Task 3: Execute the re-baseline — OPERATOR ACTION (human-gated)

**The agent does not run any step in this task.** It is the deliberate, destructive re-baseline. Each command is run by the operator in their own terminal.

- [ ] **Step 1: Confirm backup** — `grep "Backup OK" $(ls -t ~/ibc/logs/db_backup_*.log | head -1)`. Abort if not present.

- [ ] **Step 2: Dry-run the retirement**
```bash
ALGO_DATABASE_URL="postgresql://algo:algo@localhost:55432/algo_poc" \
  .venv/bin/python -m scripts.ops.retire_legacy_positions
```
Review the printed list against the Task 1 count (expect 33).

- [ ] **Step 3: Apply the retirement** (interactive; type the exact count when prompted)
```bash
ALGO_DATABASE_URL="postgresql://algo:algo@localhost:55432/algo_poc" \
  .venv/bin/python -m scripts.ops.retire_legacy_positions --apply
```
Expected: `Retired 33 legacy positions.` and `SELECT count(*) FROM positions WHERE status='open';` returns `0`.

- [ ] **Step 4: Flatten the IB paper positions** (operator, in the IB Gateway/TWS GUI — simplest and safest for a one-off): cancel any resting orders, then market-sell the ~3 positions recorded in Task 1 so the account holds cash only. (Do **not** script this; it is a one-time manual flatten on the paper account.)

- [ ] **Step 5: Verify the account is flat** — re-run the Task 1 Step 3 read-only snapshot. Expected: `positions=0 open_orders=0`.

---

### Task 4: Verify a clean reconciliation with entries still disabled — OPERATOR + read-only verify

- [ ] **Step 1: Run the standard wrapper (entries still disabled in config)**
```bash
~/ibc/run_paper.sh
```
- [ ] **Step 2: Confirm the result** — inspect `~/ibc/logs/paper_trading_$(date +%Y%m%d).log`.
Expected line: `... reconciliation: ok; entries: disabled`. Both books empty → no discrepancies → `severity=ok`.
- [ ] **Step 3: Confirm a currency-complete equity/capital snapshot committed** for today, and that no buy intent advanced (0 recommendations if entries disabled).

**Do not proceed to Task 5 unless reconciliation reports `ok`.**

---

### Task 5: Re-enable paper entries (config) behind the existing guard

**Files:**
- Modify: `config/default.yaml` (the `capital.paper` block)
- Test: `tests/shared/test_config.py`

**Safety:** entries are already fail-closed on reconciliation — `run_paper.py:1097-1099` forces `entries_disabled = True` when `preparation.reconciliation.entries_allowed` is false, and the buy loop (`run_paper.py:553-560`) blocks buys the same way. `entries_allowed = (severity == "ok" and not discrepancies)`. So flipping the flag cannot fire buys while any discrepancy exists.

- [ ] **Step 1: Write the failing test** (locks the flag + the guard contract)

Add to `tests/shared/test_config.py`:
```python
def test_paper_entries_enabled_and_guarded_by_reconciliation():
    from shared.config import load_config
    from services.execution.reconciliation import ReconciliationResult

    cfg = load_config("config/default.yaml")
    assert cfg.capital.paper.entries_enabled is True

    major = ReconciliationResult(
        matched=0,
        discrepancies=[{"type": "missing_in_db", "auto_correct": False}],
        severity="major",
        account_id="DUN551088",
    )
    assert major.entries_allowed is False  # major recon still blocks entries
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest tests/shared/test_config.py::test_paper_entries_enabled_and_guarded_by_reconciliation -q`
Expected: FAIL (`entries_enabled is True` assertion fails — currently `false`).

- [ ] **Step 3: Flip the config flag**

In `config/default.yaml`, under `capital: paper:`, change `entries_enabled: false` to `entries_enabled: true`. Leave `deployment_fraction` and `max_deployable_usd` as-is for now (see Task 6 note).

- [ ] **Step 4: Run it to verify it passes**

Run: `.venv/bin/python -m pytest tests/shared/test_config.py::test_paper_entries_enabled_and_guarded_by_reconciliation -q`
Expected: PASS.

- [ ] **Step 5: Full suite + lint + commit**

Run: `.venv/bin/python -m pytest -q` (Expected: all pass) then `uvx ruff check config >/dev/null 2>&1 || true`.
```bash
git add config/default.yaml tests/shared/test_config.py
git commit -m "feat: re-enable paper entries (guarded by fail-closed reconciliation)"
```

---

### Task 6: First entries-enabled monitored run + confirm fills persist — OPERATOR ACTION

> **Two gates discovered 2026-07-28** when the first attempt was a no-op (`entries: disabled` despite `entries_enabled: true`). Both must be cleared before entries fire:
> 1. **CLI opt-in:** `--entries-disabled` is `BooleanOptionalAction` with `default=True`, and the wrappers run `run_paper.py --publish` *without* `--no-entries-disabled`. Since `entries_disabled = args.entries_disabled OR not config.entries_enabled`, you must pass `--no-entries-disabled` in addition to the config flag.
> 2. **Stale services:** the docker stack has been "Up 2 weeks" — it predates the durable-ledger and dual-currency merges, so the running execution/risk containers would mishandle the SGD account. Rebuild before entries-on.
>
> Deployment scale is already contained via `capital.paper.max_deployable_usd: 20000` (committed `e0d3add`) — supersedes the earlier "consider lowering deployment_fraction" note.

- [x] **Step 1: Rebuild + restart the stale services** (operator; recreates the paper services — not `down -v`, volumes safe):
```bash
docker compose build execution risk-management
docker compose up -d execution risk-management
docker image inspect algo-poc-execution:latest algo-poc-risk-management:latest  # fresh timestamps
```

> **Done 2026-07-29.** Two things surfaced beyond the written step:
> 1. **Docker daemon was wedged** by the prior night's host disk-full event (the `Docker.raw` sparse file couldn't grow → VM I/O errors → daemon hung since 22:55). Recovery: `osascript -e 'quit app "Docker"'`, then `pkill -9 -f com.docker.backend`/`com.docker.virtualization` (VM data on volumes is safe across a hard VM stop), then `open -a Docker`. Server 28.3.2 responded after ~45s. Host now has ample free space; VM internal disk had 792G free (never the constraint).
> 2. **`migrate` was also stale and must be rebuilt too.** `up -d execution risk-management` pulls in the `migrate` service via `depends_on`; the 2-week-old migrate image predated migration `b17c8e4a6d92` (which the DB is stamped at), so it aborted with `Can't locate revision 'b17c8e4a6d92'` and **blocked every dependent service from starting**. Fix: `docker compose build migrate` then re-run `up -d`. `b17c8e4a6d92` is the current *head*, so the re-run is a pure no-op upgrade (exit 0, **zero DDL applied**). Add `migrate` to this step's build list.
>
> The full stack was subsequently rebuilt for consistency (`docker compose build api notifications data-ingestion signal-generation ml-model` + `up -d`); all 9 services now run fresh images with clean startup logs (execution connected to IB paper `DUN551088` on 7497; risk-management loaded an empty book, nav=20000).
- [ ] **Step 2: Monitored entries-on run — DURING US MARKET HOURS** (so limit orders fill; the scheduled 04:15 SGT job runs after close, so do the first one manually while the market is open):
```bash
cd ~/GitHub/algo-poc && \
ALGO_DATABASE_URL="postgresql://algo:algo@localhost:55432/algo_poc" \
ALGO_REDIS_URL="redis://localhost:56379/0" \
.venv/bin/python scripts/run_paper.py --publish --no-entries-disabled
```
Expected: the run logs `entries: enabled`, publishes recommendations, and a handful of BUYs route (capped at $20k deployable).
- [ ] **Step 3: Confirm the live-order path actually records fills** — the single most important check, because `execution_fills` has **0 rows ever**:
```bash
PGPASSWORD=algo psql -h localhost -p 55432 -U algo -d algo_poc -c \
"SELECT count(*), min(executed_at), max(executed_at) FROM execution_fills WHERE account_id='DUN551088';"
```
Expected after fills route: `count > 0`. If it stays 0 while orders were placed, the fill-recording path is broken and must be debugged before Gate 4 can accrue.
- [ ] **Step 4: Confirm positions are now broker-owned** — `SELECT count(*) FROM positions WHERE account_id IS NOT NULL;` returns the newly filled positions (account_id + con_id populated), and the next day's reconciliation stays `ok`.
- [ ] **Step 5: Make entries persistent for scheduled runs** — once the monitored run confirms fills persist, add `--no-entries-disabled` to the `run_paper.py --publish` line in **both** `deploy/launchd/run_paper.sh` and `~/ibc/run_paper.sh`, commit, and push the held commits (`f17bd88` entries-enable, `e0d3add` cap, + the wrapper change).
- [ ] **Step 6: Accumulate Gate 4 evidence** over subsequent sessions — median slippage ≤ 20 bps and failed-order rate ≤ 1% (the divergence monitor and reconciliation reports carry these).

---

### Task 7: Update the go-live record and memory

**Files:**
- Modify: `docs/operations/go-live-checklist.md`
- Modify: `~/.claude/projects/-Users-huiliang-GitHub-algo-poc/memory/dual-currency-paper-live.md`

- [ ] **Step 1:** In the go-live checklist, record the re-baseline date and **restart Gate 1's 60-day continuous clock** from the first clean entries-enabled run. Note Gate 6 is cleared (reconciliation `ok`) and Gate 4 is now accruing.
- [ ] **Step 2:** Update the memory file: paper book re-baselined via Path A on <date>; legacy 33 sim positions retired; IB account flattened; entries re-enabled behind the reconciliation guard.
- [ ] **Step 3: Commit**
```bash
git add docs/operations/go-live-checklist.md
git commit -m "docs: record Path A paper re-baseline and Gate 1 clock restart"
```

---

## Sequencing & ownership summary

| Task | Owner | Destructive? |
|---|---|---|
| 1 — capture pre-state | agent (read-only) / operator | no |
| 2 — build retirement tool | **agent (TDD)** | no (tests use in-memory SQLite) |
| 3 — retire rows + flatten IB | **operator only** | **yes** |
| 4 — verify clean reconciliation | operator + agent read-only | no |
| 5 — re-enable entries (config) | **agent** | no (guard already enforced) |
| 6 — monitored entries-on run | **operator only** | **yes** (places paper orders) |
| 7 — docs/memory | agent | no |

## Self-review notes

- **Gate 6 coverage:** Tasks 3–4 drive reconciliation to `ok` (both books empty). ✓
- **Gate 4 coverage:** Tasks 5–6 re-enable entries and verify fills persist + accrue slippage/fail-rate. ✓
- **Safety:** every position mutation is dry-run-first, TTY + exact-count gated, backup-verified, and operator-run; the config flip is inert until reconciliation is `ok` (existing guard, locked by a test). ✓
- **No inferred ownership:** legacy rows are *closed*, not re-attributed; broker positions are *flattened*, not guessed into sleeves — consistent with the dual-currency plan's "do not infer legacy position ownership" constraint. ✓
