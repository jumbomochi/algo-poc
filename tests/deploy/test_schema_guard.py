"""Every wrapper that reads the paper DB refuses a schema behind the code.

KAN-103 adds ``equity_snapshots.session_date``, and every reader of that table
now names it. ``run_paper.sh`` has compared ``alembic current`` with
``alembic heads`` since the 2026-07-25 incident. The 04:45 divergence monitor
and the Monday digest did not, so a pull without ``alembic upgrade head``
would have killed them mid-run on a raw psycopg2 UndefinedColumn. The check
now lives once, in ``deploy/launchd/lib/schema_guard.sh``, and all three
wrappers call it before anything reads the database.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DEPLOY_DIR = REPO / "deploy" / "launchd"
LIB = DEPLOY_DIR / "lib" / "schema_guard.sh"
HEAD = "b3d5f7a9c1e2"
BEHIND = "007388445941"

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="the launchd wrappers are POSIX shell"
)


def _exec(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)
    return path


def _alembic(path: Path, *, current: str | None, head: str | None = HEAD) -> Path:
    return _exec(path, (
        "#!/bin/bash\n"
        'case "$1" in\n'
        + (f"  heads) echo '{head} (head)' ;;\n" if head else "  heads) exit 1 ;;\n")
        + (f"  current) echo '{current} (head)' ;;\n" if current else "  current) exit 1 ;;\n")
        + "esac\n"
    ))


# ----------------------------------------------------------------- the lib


def _guard(tmp_path: Path, alembic: Path) -> tuple[int, str]:
    script = (
        f'. "{LIB}"\n'
        f'algo_schema_guard "{tmp_path}"; rc=$?\n'
        'printf "%s|%s|%s" "$ALGO_SCHEMA_DB_REV" "$ALGO_SCHEMA_HEAD_REV" "$ALGO_SCHEMA_ERROR"\n'
        "exit $rc\n"
    )
    proc = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=30,
        env={**os.environ, "ALGO_ALEMBIC_BIN": str(alembic)},
    )
    return proc.returncode, proc.stdout


def test_a_db_at_head_passes(tmp_path):
    rc, out = _guard(tmp_path, _alembic(tmp_path / "alembic", current=HEAD))
    assert rc == 0
    assert out == f"{HEAD}|{HEAD}|"


def test_a_db_behind_head_is_refused_with_both_revisions_and_the_fix(tmp_path):
    rc, out = _guard(tmp_path, _alembic(tmp_path / "alembic", current=BEHIND))
    assert rc == 1
    db, head, error = out.split("|", 2)
    assert (db, head) == (BEHIND, HEAD)
    assert f"DB at '{BEHIND}', head '{HEAD}'" in error
    assert "alembic upgrade head" in error


def test_an_unreadable_db_revision_is_refused_not_passed(tmp_path):
    rc, out = _guard(tmp_path, _alembic(tmp_path / "alembic", current=None))
    assert rc == 1
    assert "DB at 'none'" in out


def test_an_unreadable_head_is_refused(tmp_path):
    rc, out = _guard(tmp_path, _alembic(tmp_path / "alembic", current=HEAD, head=None))
    assert rc == 1
    assert out.endswith("could not determine alembic head revision")


def test_the_guard_never_migrates():
    """Applying a migration to the paper DB is a human's call."""
    text = LIB.read_text()
    invocations = [
        line.strip() for line in text.splitlines()
        if '"$alembic"' in line and not line.lstrip().startswith("#")
    ]
    assert invocations, "the guard no longer calls alembic at all"
    for line in invocations:
        assert '"$alembic" heads' in line or '"$alembic" current' in line, line


# ------------------------------------------------- every wrapper calls it


@pytest.mark.parametrize(
    "wrapper, main_script",
    [
        ("run_paper.sh", "scripts/run_paper.py"),
        ("run_divergence.sh", "scripts/divergence_monitor.py"),
        ("run_evidence_digest.sh", "scripts/ops/evidence_digest.py"),
    ],
)
def test_each_db_reading_wrapper_guards_before_its_run(wrapper, main_script):
    text = (DEPLOY_DIR / wrapper).read_text()
    source = '. "$ALGO_DIR/deploy/launchd/lib/schema_guard.sh"'
    assert source in text, f"{wrapper} does not source the schema guard"
    call = text.index('algo_schema_guard "$ALGO_DIR"')
    run = text.index(main_script, call - 1 if call else 0)
    assert text.index(source) < call < run, (
        f"{wrapper} must check the schema before it runs {main_script}"
    )


# ------------------------------------------------- the wrappers, driven


def test_the_divergence_monitor_refuses_a_schema_behind_head(tmp_path):
    from tests.deploy.test_divergence_alerting import _drive_wrapper, _monitor_argv

    res, sends, log = _drive_wrapper(tmp_path, 0, alembic_current=BEHIND)

    assert res.returncode == 2, log  # hard error: page
    assert _monitor_argv(tmp_path) == [], "the monitor ran against a stale schema"
    assert f"DB at '{BEHIND}', head 'a1b2c3d4e5f6'" in log
    assert len(sends) == 1
    assert "Divergence monitor ABORTED" in sends[0]
    assert "alembic upgrade head" in sends[0]


def test_the_divergence_monitor_runs_when_the_schema_is_at_head(tmp_path):
    from tests.deploy.test_divergence_alerting import _drive_wrapper, _monitor_argv

    res, _sends, log = _drive_wrapper(tmp_path, 0)

    assert res.returncode == 0, log
    assert _monitor_argv(tmp_path), "the monitor never ran"


def _stub_tree(tmp_path: Path, *, current: str) -> tuple[Path, dict[str, str]]:
    """A throwaway ALGO_DIR: the wrappers under test are the repo's, copied."""
    tree = tmp_path / "algo-dir"
    shutil.copytree(DEPLOY_DIR, tree / "deploy" / "launchd")
    ran = tmp_path / "python-ran.log"
    _exec(tree / ".venv" / "bin" / "python",
          f'#!/bin/bash\nprintf "%s\\n" "$*" >> "{ran}"\nexit 0\n')
    _alembic(tree / ".venv" / "bin" / "alembic", current=current)
    bin_dir = tmp_path / "bin"
    _exec(bin_dir / "nc", "#!/bin/bash\nexit 0\n")
    _exec(bin_dir / "curl",
          f'#!/bin/bash\nprintf "%s\\n" "$*" >> "{tmp_path / "curl.log"}"\nexit 0\n')
    security = _exec(
        tmp_path / "stubs" / "security",
        "#!/bin/bash\n"
        'case "${@: -1}" in\n'
        "  POSTGRES_PASSWORD) echo 'stub-pg' ;;\n"
        "  REDIS_PASSWORD) echo 'stub-redis' ;;\n"
        "  TELEGRAM_BOT_TOKEN) echo '123456789:stub' ;;\n"
        "  TELEGRAM_CHAT_ID) echo '-100123' ;;\n"
        "  *) echo 'could not be found' >&2; exit 44 ;;\n"
        "esac\n",
    )
    home = tmp_path / "home"
    home.mkdir()
    env = dict(
        PATH=f"{bin_dir}:/usr/bin:/bin",
        HOME=str(home),
        ALGO_DIR=str(tree),
        ALGO_SECURITY_BIN=str(security),
        ALGO_KEYCHAIN_SERVICE="algo-poc-absent-test-service",
        ALGO_OSASCRIPT_BIN=str(_exec(tmp_path / "stubs" / "osascript",
                                     "#!/bin/bash\nexit 0\n")),
    )
    return tree, env


def _drive(tmp_path: Path, wrapper: str, *, current: str):
    tree, env = _stub_tree(tmp_path, current=current)
    proc = subprocess.run(
        [str(tree / "deploy" / "launchd" / wrapper)],
        capture_output=True, text=True, timeout=300, env=env, cwd=str(tmp_path),
    )
    logs = sorted((tmp_path / "home" / "ibc" / "logs").glob("*.log"))
    log = "\n".join(p.read_text() for p in logs)
    ran = tmp_path / "python-ran.log"
    curl = tmp_path / "curl.log"
    return (
        proc.returncode,
        log,
        ran.read_text() if ran.exists() else "",
        curl.read_text() if curl.exists() else "",
    )


def test_the_digest_refuses_a_schema_behind_head(tmp_path):
    rc, log, ran, curl = _drive(tmp_path, "run_evidence_digest.sh", current=BEHIND)

    assert rc == 1, log
    assert "evidence_digest.py" not in ran, "the digest ran against a stale schema"
    assert f"DB at '{BEHIND}', head '{HEAD}'" in log
    assert "Weekly evidence digest ABORTED" in curl
    assert "dead-man check was NOT pinged" in curl


def test_the_digest_runs_when_the_schema_is_at_head(tmp_path):
    rc, log, ran, _curl = _drive(tmp_path, "run_evidence_digest.sh", current=HEAD)

    assert rc == 0, log
    assert "evidence_digest.py" in ran


def test_the_paper_run_still_refuses_a_schema_behind_head(tmp_path):
    """The original guard, now through the shared lib, with its words intact."""
    rc, log, ran, curl = _drive(tmp_path, "run_paper.sh", current=BEHIND)

    assert rc == 1, log
    assert "run_paper.py" not in ran
    assert f"DB at '{BEHIND}', head '{HEAD}'" in log
    alerts = (tmp_path / "home" / "ibc" / "logs" / "ALERTS.log")
    assert f"DB schema at '{BEHIND}', head '{HEAD}'" in alerts.read_text()
    assert "paper DB schema out of date" in curl
