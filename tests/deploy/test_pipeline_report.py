"""The morning pipeline report: what the operator is told about a paper run.

Two subjects share this module because they are one operator-facing surface.

KAN-30 — the pure summariser (``scripts/ops/pipeline_report_summary.py``),
driven against a real database so the counts are proven to come from
``execution_fills`` / ``order_intents`` rather than from grepping a log; and the
launchd wrapper driven end-to-end with ``curl`` stubbed, so the assertion is on
the message actually sent.

KAN-31 — the status branch inside ``run_pipeline_report.sh``, which decides the
Telegram line by grepping the paper log for ``exit code: 0``. That grep became
load-bearing when a publish failure started writing ``exit code: 1``: it is what
turns a silent, order-less run into "❌ paper run FAILED". The block is extracted
from the real script and run in bash, so this exercises the shipped code rather
than a paraphrase of it.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from scripts.ops.pipeline_report_summary import (
    _redact,
    collect_facts,
    render_summary,
)
from shared.models import Base
from shared.models.order_ledger import ExecutionFill, OrderIntent, OrderStatus
from shared.models.system_halt import SystemHaltState

REPO = Path(__file__).resolve().parents[2]

SINCE = datetime(2026, 8, 16, 0, 0, tzinfo=timezone.utc)
DURING = SINCE + timedelta(hours=4)
BEFORE = SINCE - timedelta(hours=4)


@pytest.fixture()
def session(tmp_path) -> Session:
    engine = create_engine(f"sqlite:///{tmp_path / 'report.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _halt(session, *, mode="paper", active=True, reason="daily drawdown -6.2%",
          source="circuit_breaker", at=DURING):
    session.add(SystemHaltState(
        mode=mode, active=active, source=source, reason=reason,
        triggered_by="risk_management", activated_at=at,
        cleared_at=None if active else at, cleared_by=None if active else "op",
    ))
    session.commit()


def _fill(session, *, at=DURING, symbol="AAPL"):
    n = session.query(ExecutionFill).count()
    session.add(ExecutionFill(
        account_id="DUN551088", execution_id=f"exec-{n}", ib_order_id=str(n),
        con_id=1 + n, symbol=symbol, exchange="SMART", currency="USD",
        side="BUY", quantity=10.0, price=100.0, executed_at=at,
    ))
    session.commit()


def _intent(session, *, status, at=DURING, mode="paper"):
    n = session.query(OrderIntent).count()
    session.add(OrderIntent(
        recommendation_id=f"rec-{n}", account_id="DUN551088", mode=mode,
        portfolio="thematic_momentum", con_id=1 + n, symbol="AAPL",
        exchange="SMART", currency="USD", action="BUY",
        requested_quantity=10.0, order_type="LMT", status=status,
        created_at=at, updated_at=at,
    ))
    session.commit()


def _facts(session, *, mode="paper"):
    return collect_facts(session, since=SINCE, mode=mode)


# ---------------------------------------------------------------------------
# Halt state (AC2)
# ---------------------------------------------------------------------------

def test_a_quiet_run_reports_a_clear_halt_and_zero_counts(session):
    summary = render_summary(_facts(session))
    assert "halt: clear" in summary
    assert "fills:0" in summary


def test_an_active_halt_leads_the_summary_and_names_its_reason(session):
    _halt(session, reason="daily drawdown -6.2%")
    summary = render_summary(_facts(session))
    assert summary.startswith("🛑 HALT"), summary
    assert "daily drawdown -6.2%" in summary
    assert "circuit_breaker" in summary


def test_a_cleared_halt_is_not_reported_as_active(session):
    _halt(session, active=False, reason="yesterday's kill")
    summary = render_summary(_facts(session))
    assert "halt: clear" in summary
    assert "yesterday's kill" not in summary


def test_a_halt_in_another_mode_is_not_reported(session):
    """Halt is scoped by mode so a live halt never bleeds into the paper
    digest, and vice versa."""
    _halt(session, mode="live", reason="live circuit breaker")
    summary = render_summary(_facts(session, mode="paper"))
    assert "halt: clear" in summary
    assert "live circuit breaker" not in summary


def test_an_overlong_halt_reason_is_truncated(session):
    """`reason` is a String(500) and Telegram rejects a 4096-char body with
    HTTP 400 — which the fire-and-forget send would swallow into silence."""
    _halt(session, reason="x" * 500)
    summary = render_summary(_facts(session))
    assert len(summary) < 400, len(summary)
    assert "🛑 HALT" in summary


# ---------------------------------------------------------------------------
# Fills and rejections come from the tables, not the log (AC3)
# ---------------------------------------------------------------------------

def test_fills_are_counted_from_execution_fills(session):
    _fill(session)
    _fill(session, symbol="MSFT")
    assert "fills:2" in render_summary(_facts(session))


def test_rejections_are_split_by_status(session):
    """"Risk said no" and "the broker said no" are different operator
    problems, so one combined number would hide which one happened."""
    _intent(session, status=OrderStatus.RISK_REJECTED)
    _intent(session, status=OrderStatus.RISK_REJECTED)
    _intent(session, status=OrderStatus.SUBMISSION_FAILED)
    summary = render_summary(_facts(session))
    assert "risk 2" in summary
    assert "broker 1" in summary


def test_non_rejected_intents_are_not_counted_as_rejections(session):
    _intent(session, status=OrderStatus.FILLED)
    _intent(session, status=OrderStatus.CANCELLED)
    summary = render_summary(_facts(session))
    assert "risk 0" in summary
    assert "broker 0" in summary


def test_activity_before_the_window_is_excluded(session):
    """Yesterday's fills must not be reported as this morning's run."""
    _fill(session, at=BEFORE)
    _intent(session, status=OrderStatus.RISK_REJECTED, at=BEFORE)
    summary = render_summary(_facts(session))
    assert "fills:0" in summary
    assert "risk 0" in summary


def test_an_intent_from_another_mode_is_excluded(session):
    _intent(session, status=OrderStatus.RISK_REJECTED, mode="live")
    assert "risk 0" in render_summary(_facts(session, mode="paper"))


# ---------------------------------------------------------------------------
# The CLI
# ---------------------------------------------------------------------------

def _run_cli(db_path, *extra):
    return subprocess.run(
        [sys.executable, str(REPO / "scripts/ops/pipeline_report_summary.py"),
         "--database-url", f"sqlite:///{db_path}",
         "--since", SINCE.isoformat(), *extra],
        capture_output=True, text=True, cwd=REPO, timeout=60,
    )


def test_cli_prints_the_summary(tmp_path):
    db = tmp_path / "report.db"
    engine = create_engine(f"sqlite:///{db}")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        _fill(s)
        _halt(s, reason="manual kill")
    res = _run_cli(db)
    assert res.returncode == 0, res.stderr
    assert "🛑 HALT" in res.stdout
    assert "manual kill" in res.stdout
    assert "fills:1" in res.stdout


@pytest.mark.parametrize("password", [
    "sup3r-s3cret",
    "p@ssw0rd",     # an '@' in the secret used to split the match
    "pa ss",        # whitespace used to defeat the match entirely
])
def test_a_dsn_in_an_error_is_redacted_before_it_reaches_the_log(password):
    """The wrapper appends this script's stderr straight into the report log.
    SQLAlchemy 2.x does not echo the URL today, so this is depth rather than a
    live hole — but a driver that does must not be the thing that discovers
    it. Anchoring on the first '@' leaks the tail, so the match runs to the
    last one."""
    text = (
        "OperationalError: could not connect to "
        f"postgresql://algo:{password}@localhost:55432/algo_poc"
    )
    redacted = _redact(text)
    assert password not in redacted, redacted
    assert "***@localhost:55432/algo_poc" in redacted, redacted


def test_cli_stderr_carries_no_password_on_a_bad_dsn():
    res = subprocess.run(
        [sys.executable, str(REPO / "scripts/ops/pipeline_report_summary.py"),
         "--database-url", "nosuchdriver://algo:sup3r-s3cret@localhost:55432/db",
         "--since", SINCE.isoformat()],
        capture_output=True, text=True, cwd=REPO, timeout=60,
    )
    assert res.returncode != 0
    assert "sup3r-s3cret" not in res.stderr + res.stdout, res.stderr
    # A named cause, not a bare traceback the operator has to decode.
    assert "NoSuchModuleError" in res.stderr, res.stderr


def test_cli_fails_loudly_on_an_unreachable_database(tmp_path):
    """It must not print a reassuring "halt: clear" it cannot substantiate —
    the wrapper needs a nonzero exit so it can render the unknown marker."""
    res = subprocess.run(
        [sys.executable, str(REPO / "scripts/ops/pipeline_report_summary.py"),
         "--database-url", "postgresql://nobody@127.0.0.1:1/nothing",
         "--since", SINCE.isoformat()],
        capture_output=True, text=True, cwd=REPO, timeout=120,
    )
    assert res.returncode != 0
    assert "halt: clear" not in res.stdout


# ---------------------------------------------------------------------------
# The wrapper, driven end-to-end (AC1, AC4, AC5, AC6)
# ---------------------------------------------------------------------------

DEPLOY_DIR = REPO / "deploy" / "launchd"
RUN_REPORT = DEPLOY_DIR / "run_pipeline_report.sh"


def _drive_wrapper(tmp_path, *, paper_log="paper run finished, exit code: 0\n",
                   seed=None, database_url=None, curl_exit=0):
    """Run run_pipeline_report.sh end-to-end against stubs.

    Everything it reaches out to is stubbed on PATH: ``docker`` (compose logs
    and the equity psql query), ``curl`` (the send, recorded to a file),
    ``osascript`` (the local alert) and ``security`` (the keychain). The fake
    python dispatches: the summariser is the real thing under this
    interpreter, the IB heredoc is a stub.
    """
    home = tmp_path / "home"
    (home / "ibc" / "logs").mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl_log = tmp_path / "curl.log"
    today = datetime.now().strftime("%Y%m%d")

    if paper_log is not None:
        (home / "ibc" / "logs" / f"paper_trading_{today}.log").write_text(
            "  BUY  AAPL 10\n  SELL MSFT 5\n  SKIP NVDA\n" + paper_log
        )

    db = tmp_path / "report.db"
    if database_url is None:
        engine = create_engine(f"sqlite:///{db}")
        Base.metadata.create_all(engine)
        with Session(engine) as s:
            if seed is not None:
                seed(s)
        database_url = f"sqlite:///{db}"

    def stub(name, body):
        p = bin_dir / name
        p.write_text(body)
        p.chmod(0o755)
        return p

    stub("osascript", "#!/bin/bash\nexit 0\n")
    stub("docker", f"#!/bin/bash\necho ' {datetime.now():%Y-%m-%d} | 1 | 1000.00'\nexit 0\n")
    stub("security", """#!/bin/bash
case "${@: -1}" in
  POSTGRES_PASSWORD)  echo "stub-pg" ;;
  REDIS_PASSWORD)     echo "stub-redis" ;;
  TELEGRAM_BOT_TOKEN) echo "stub-token" ;;
  TELEGRAM_CHAT_ID)   echo "stub-chat" ;;
  *) echo "could not be found" >&2; exit 44 ;;
esac
""")
    stub("curl", f"""#!/bin/bash
{{ for a in "$@"; do printf '%s\\n' "$a"; done; printf -- '---END---\\n'; }} >> {curl_log}
exit {curl_exit}
""")
    fake_python = stub("fake-python", f"""#!/bin/bash
case "$1" in
  *pipeline_report_summary.py) exec {sys.executable} "$@" ;;
esac
echo "2 resting orders"
exit 0
""")

    env = dict(
        os.environ,
        HOME=str(home),
        # The wrapper REPLACES PATH (launchd's lacks /usr/local/bin), so the
        # stubs go in via its own prefix knob rather than via PATH.
        ALGO_PATH_PREFIX=str(bin_dir),
        ALGO_DIR=str(REPO),
        ALGO_PYTHON=str(fake_python),
        ALGO_DATABASE_URL=database_url,
        ALGO_SECURITY_BIN=str(bin_dir / "security"),
        ALGO_OSASCRIPT_BIN=str(bin_dir / "osascript"),
        ALGO_KEYCHAIN_SERVICE="algo-poc-absent-test-service",
    )
    res = subprocess.run(
        [str(RUN_REPORT)], capture_output=True, text=True,
        timeout=300, env=env, cwd=str(REPO),
    )
    sends = (
        [s for s in curl_log.read_text().split("---END---\n") if s.strip()]
        if curl_log.exists() else []
    )
    logs = list((home / "ibc" / "logs").glob("pipeline_report_*.log"))
    return res, sends, (logs[0].read_text() if logs else "")


def _message(sends):
    assert len(sends) == 1, sends
    lines = sends[0].splitlines()
    # `curl ... --data-urlencode text=<body>`; the body is its own argv entry.
    body = [l for l in lines if l.startswith("text=")]
    assert body, lines
    return body[0][len("text="):]


def test_the_message_reports_the_documented_facts_in_order(tmp_path):
    """AC1. Halt, then fills, then rejections split by status, then the run
    status, divergence, resting orders and equity continuity."""
    def seed(s):
        _fill(s, at=datetime.now(timezone.utc))
        _intent(s, status=OrderStatus.RISK_REJECTED, at=datetime.now(timezone.utc))

    res, sends, log = _drive_wrapper(tmp_path, seed=seed)
    assert res.returncode == 0
    msg = _message(sends)
    order = ["halt", "fills:", "risk ", "paper run", "divergence", "resting", "snapshot"]
    positions = [msg.find(token) for token in order]
    assert all(p >= 0 for p in positions), (msg, order, positions)
    assert positions == sorted(positions), msg
    assert "fills:1" in msg
    assert "risk 1" in msg


def test_an_active_halt_leads_the_telegram_message(tmp_path):
    """AC2. A halt cannot be missed at the end of a line, so it goes first."""
    res, sends, _ = _drive_wrapper(
        tmp_path, seed=lambda s: _halt(s, at=datetime.now(timezone.utc),
                                       reason="drawdown breach"),
    )
    msg = _message(sends)
    assert msg.startswith("🛑 HALT"), msg
    assert "drawdown breach" in msg


def test_a_missing_paper_log_still_sends_a_message(tmp_path):
    """AC4. The existing ❌ semantics, not a silent skip — silence from this
    job is itself the signal, so it must never be manufactured."""
    res, sends, _ = _drive_wrapper(tmp_path, paper_log=None)
    assert res.returncode == 0
    msg = _message(sends)
    assert "paper run MISSING" in msg


def test_the_grep_counts_stay_in_the_log_body(tmp_path):
    """They remain useful for diagnosis; they are just no longer the headline
    number, because a log line is not evidence that an order existed."""
    res, sends, log = _drive_wrapper(tmp_path)
    assert "BUYs: 1" in log
    assert "B:1" not in _message(sends)


def test_a_failing_send_does_not_change_the_exit_code(tmp_path):
    """AC5. `|| true` and the unconditional `exit 0` are preserved."""
    res, sends, _ = _drive_wrapper(tmp_path, curl_exit=7)
    assert res.returncode == 0
    assert len(sends) == 1, sends


def test_an_unreadable_database_degrades_to_a_marker_not_a_false_all_clear(tmp_path):
    """Absence of evidence is not evidence of no halt. If the summary cannot
    be computed the message must say so, and must still be sent."""
    res, sends, _ = _drive_wrapper(
        tmp_path, database_url="postgresql://nobody@127.0.0.1:1/nothing",
    )
    assert res.returncode == 0
    msg = _message(sends)
    assert "halt: clear" not in msg
    assert "unknown" in msg.lower()
    assert "paper run" in msg


REPO_ROOT = Path(__file__).resolve().parents[2]
REPORT_SCRIPT = REPO_ROOT / "deploy/launchd/run_pipeline_report.sh"

# The `if grep -q "exit code: 0" ... fi` block, lifted verbatim.
_STATUS_BLOCK = re.compile(
    r'^if grep -q "exit code: 0".*?^fi$', re.MULTILINE | re.DOTALL
)


def status_block() -> str:
    match = _STATUS_BLOCK.search(REPORT_SCRIPT.read_text())
    assert match, "run_pipeline_report.sh no longer has a RUN_STATUS grep block"
    return match.group(0)


def run_status(paper_log: Path | str) -> str:
    """Execute the script's own status block against a fixture log."""
    script = f'PAPER_LOG="{paper_log}"\n{status_block()}\nprintf %s "$RUN_STATUS"'
    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


LOG_TEMPLATE = (
    "Sun Aug 16 04:15:02 +08 2026: Starting paper trading run\n"
    "1 signals generated\n"
    "State committed to database\n"
    "WARNING: publish to pipeline failed (Error 111 connecting to redis:6379); "
    "intents remain replayable\n"
    "Sun Aug 16 04:17:44 +08 2026: Paper trading run completed (exit code: {code})\n"
)


@pytest.fixture
def paper_log(tmp_path):
    def write(code: int) -> Path:
        path = tmp_path / "paper_trading_20260816.log"
        path.write_text(LOG_TEMPLATE.format(code=code))
        return path

    return write


def test_a_publish_failure_reports_the_run_as_failed(paper_log):
    """AC4: exit code 1 in the log ⇒ the Telegram digest says FAILED."""
    assert run_status(paper_log(1)) == "❌ paper run FAILED"


def test_a_clean_run_still_reports_ok(paper_log):
    assert run_status(paper_log(0)) == "✅ paper run OK"


def test_a_missing_log_reports_missing(tmp_path):
    """The job never ran at all — distinct from a run that failed."""
    assert run_status(tmp_path / "absent.log") == "❌ paper run MISSING"


# ---------------------------------------------------------------------------
# The wrapper's own failure alert must not assert something false
# ---------------------------------------------------------------------------

PAPER_WRAPPER = REPO_ROOT / "deploy/launchd/run_paper.sh"

# The `if [ "$EXIT_CODE" != "0" ]; then ... fi` alert block, lifted verbatim.
_FAILURE_BLOCK = re.compile(
    r'^if \[ "\$EXIT_CODE" != "0" \]; then.*?^fi$', re.MULTILINE | re.DOTALL
)


def failure_alerts(
    exit_code: int, log_body: str, tmp_path: Path, *, lines_before_run: int = 0
) -> str:
    """Run the wrapper's failure-alert block with the delivery calls stubbed."""
    match = _FAILURE_BLOCK.search(PAPER_WRAPPER.read_text())
    assert match, "run_paper.sh no longer has an EXIT_CODE failure-alert block"

    log = tmp_path / "paper_trading_20260816.log"
    log.write_text(log_body)
    script = (
        'algo_alert_local() { printf "local: %s\\n" "$1"; }\n'
        'telegram() { printf "telegram: %s\\n" "$1"; }\n'
        f"EXIT_CODE={exit_code}\nLOG_LINES_BEFORE_RUN={lines_before_run}\n"
        f'LOG_FILE="{log}"\n{match.group(0)}\n'
    )
    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_a_publish_failure_is_not_reported_as_an_uncommitted_book(tmp_path):
    """KAN-31 makes exit 1 mean two different things. The alert must not claim
    "No signals committed today" when the book committed and only the publish
    failed — that sends the operator hunting the wrong fault."""
    out = failure_alerts(1, LOG_TEMPLATE.format(code=1), tmp_path)

    assert "No signals committed today" not in out
    assert "no orders reached risk/execution" in out
    assert out.count("exit 1") == 2, "both the local and Telegram paths must fire"


def test_a_run_that_committed_nothing_still_says_so(tmp_path):
    """The pre-KAN-31 meaning of a nonzero exit is unchanged."""
    log = (
        "Sun Aug 16 04:15:02 +08 2026: Starting paper trading run\n"
        "ERROR: No data fetched. Is IB Gateway running?\n"
        "Sun Aug 16 04:15:44 +08 2026: Paper trading run completed (exit code: 1)\n"
    )

    out = failure_alerts(1, log, tmp_path)

    assert "No signals committed today" in out


def test_a_rerun_does_not_inherit_the_mornings_diagnosis(tmp_path):
    """The log is per-day and appended to. A manual catch-up run that dies for
    an unrelated reason must be classified on its OWN output, not on the
    publish failure still sitting above it in the same file."""
    morning = LOG_TEMPLATE.format(code=1)
    rerun = (
        "Sun Aug 16 10:28:03 +08 2026: Starting paper trading run\n"
        "ERROR: No data fetched. Is IB Gateway running?\n"
        "Sun Aug 16 10:28:51 +08 2026: Paper trading run completed (exit code: 1)\n"
    )

    out = failure_alerts(
        1, morning + rerun, tmp_path, lines_before_run=len(morning.splitlines())
    )

    assert "No signals committed today" in out
    assert "no orders reached risk/execution" not in out


def test_a_clean_run_raises_no_failure_alert(tmp_path):
    assert failure_alerts(0, LOG_TEMPLATE.format(code=0), tmp_path) == ""


def test_the_wrapper_records_the_log_length_before_the_run(tmp_path):
    """The classification above is only honest if the offset is captured before
    the python process appends anything."""
    body = PAPER_WRAPPER.read_text()
    offset_at = body.index("LOG_LINES_BEFORE_RUN=")
    run_at = body.index('"$VENV" scripts/run_paper.py')

    assert offset_at < run_at
