"""KAN-31 AC4: the daily digest's run status, executed as the operator sees it.

``run_pipeline_report.sh`` decides the Telegram line by grepping the paper log
for ``exit code: 0``. That was already true before KAN-31 — what changed is that
it became load-bearing: a publish failure now writes ``exit code: 1``, so this
branch is the thing that turns a silent, order-less run into "❌ paper run
FAILED". Pinned here because it is the behaviour the operator actually reads,
and because a future edit to the grep would re-open the KAN-31 hole from the
other end.

The status block is extracted from the real script and run in bash, so this
exercises the shipped code rather than a paraphrase of it.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

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
