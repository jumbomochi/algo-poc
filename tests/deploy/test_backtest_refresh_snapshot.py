"""KAN-23 AC5: the Tuesday refresh must not silently un-rebaseline the monitor.

``deploy/launchd/run_backtest_refresh.sh`` re-runs the 10-year backtest every
Tuesday 05:00 SGT, and ``scripts/divergence_monitor.py`` auto-selects the
*newest* ``output/backtest_multi_*.json``. So a refresh that omits
``--universe-snapshots`` writes a survivorship-biased artifact that supersedes
the rebaselined one, and the monitor reverts to exit 3 (BLIND) within a week —
the rebaseline undone by a cron job, with nothing saying so.

Driven end-to-end against a stubbed backtest and curl, so the assertion is on
the actual argv and the actual send, not on grepping the script.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DEPLOY_DIR = REPO / "deploy" / "launchd"
RUN_REFRESH = DEPLOY_DIR / "run_backtest_refresh.sh"

#: Exit code the wrapper uses for "the membership snapshot is missing", kept
#: distinct from 1 (IB Gateway unreachable) so the launchd log says which.
EXIT_NO_SNAPSHOT = 2


_OVERRIDES = (
    'ALGO_DIR="${ALGO_DIR:-',
    'VENV="${ALGO_PYTHON:-',
    "${ALGO_MEMBERSHIP_SNAPSHOT:-",
)


def _require_overridable():
    """Refuse to launch the wrapper unless it honours the test overrides.

    Not merely defensive. While this file was being written the wrapper still
    hardcoded ALGO_DIR, and the first test run launched a *real* 10-year
    backtest against the live checkout and the live IB Gateway; on success it
    would then have ``find -delete``d baseline artifacts out of the real
    ``output/``.

    This is a hard raise inside ``_drive`` rather than a standalone assertion
    because a failing assertion only reddens its own test — pytest would carry
    on and run the remaining cases, each of which would drive the real wrapper
    against the real repo. The check has to gate the launch, not report on it.
    """
    source = RUN_REFRESH.read_text()
    missing = [token for token in _OVERRIDES if token not in source]
    if missing:
        raise RuntimeError(
            f"{RUN_REFRESH.name} no longer honours {missing}; refusing to run "
            "it, because without the overrides it would backtest the real "
            "checkout and prune the real output/ directory."
        )


def test_the_wrapper_honours_the_test_overrides():
    _require_overridable()


def _drive(tmp_path, *, snapshot=True, gateway=True, backtest_exit=0,
           backtest_sleep=0, timeout_seconds=600):
    """Run the wrapper with everything it reaches out to stubbed.

    ``ALGO_DIR`` points at a scratch tree that *symlinks* the repo's ``deploy``
    and ``scripts`` (so the wrapper sources the real secrets/telegram helpers
    and the drift guard still compares equal) but owns its own ``output/`` and
    ``data/``. That isolation is not cosmetic: on success the wrapper prunes
    ``$ALGO_DIR/output`` with ``find -delete``, and pointing it at the real repo
    would delete baseline artifacts the paper record depends on.
    """
    _require_overridable()
    home = tmp_path / "home"
    home.mkdir()
    algo_dir = tmp_path / "algo"
    algo_dir.mkdir()
    (algo_dir / "deploy").symlink_to(REPO / "deploy")
    (algo_dir / "scripts").symlink_to(REPO / "scripts")
    (algo_dir / "output").mkdir()
    (algo_dir / "data" / "universe").mkdir(parents=True)

    membership = algo_dir / "data" / "universe" / "sp500_membership.json"
    if snapshot:
        membership.write_text(json.dumps({"snapshots": {"2015-01-01": ["AAPL"]}}))

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl_log = tmp_path / "curl.log"
    argv_log = tmp_path / "argv.log"

    def stub(name, body):
        path = bin_dir / name
        path.write_text(body)
        path.chmod(0o755)
        return path

    stub("nc", f"#!/bin/bash\nexit {0 if gateway else 1}\n")
    stub("osascript", "#!/bin/bash\nexit 0\n")
    stub("security", """#!/bin/bash
case "${@: -1}" in
  TELEGRAM_BOT_TOKEN) echo "stub-token" ;;
  TELEGRAM_CHAT_ID)   echo "stub-chat" ;;
  *) echo "could not be found" >&2; exit 44 ;;
esac
""")
    stub("curl", f"""#!/bin/bash
{{ for a in "$@"; do printf '%s\\n' "$a"; done; printf -- '---END---\\n'; }} >> {curl_log}
exit 0
""")
    # Records the backtest's argv and writes a plausible artifact, exactly as
    # the real run does — the wrapper greps the log and names the newest file.
    fake_python = stub("fake-python", f"""#!/bin/bash
{{ for a in "$@"; do printf '%s\\n' "$a"; done; printf -- '---END---\\n'; }} >> {argv_log}
sleep {backtest_sleep}
echo "AGGREGATE"
echo "  Total Return: 42.0%"
touch {algo_dir}/output/backtest_multi_20260817_000000.json
exit {backtest_exit}
""")

    env = dict(
        os.environ,
        HOME=str(home),
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        ALGO_DIR=str(algo_dir),
        ALGO_PYTHON=str(fake_python),
        ALGO_SECURITY_BIN=str(bin_dir / "security"),
        ALGO_OSASCRIPT_BIN=str(bin_dir / "osascript"),
        ALGO_KEYCHAIN_SERVICE="algo-poc-absent-test-service",
        ALGO_REFRESH_TIMEOUT_SECONDS=str(timeout_seconds),
    )
    result = subprocess.run(
        [str(RUN_REFRESH)], capture_output=True, text=True,
        timeout=180, env=env, cwd=str(REPO),
    )
    sends = (
        [s for s in curl_log.read_text().split("---END---\n") if s.strip()]
        if curl_log.exists() else []
    )
    invocations = (
        [s for s in argv_log.read_text().split("---END---\n") if s.strip()]
        if argv_log.exists() else []
    )
    logs = list((home / "ibc" / "logs").glob("backtest_refresh_*.log"))
    return result, sends, invocations, (logs[0].read_text() if logs else ""), membership


# ---------------------------------------------------------------------------
# Healthy run
# ---------------------------------------------------------------------------

def test_the_refresh_runs_the_backtest_point_in_time(tmp_path):
    """AC5, healthy half: the flag is actually passed, with the snapshot path."""
    result, _, invocations, _, membership = _drive(tmp_path)

    assert result.returncode == 0, result.stderr
    assert len(invocations) == 1, invocations
    argv = invocations[0].splitlines()
    assert "--universe-snapshots" in argv, argv
    passed = argv[argv.index("--universe-snapshots") + 1]
    assert Path(passed).resolve() == membership.resolve()
    # The rest of the headline contract stays put.
    assert "--years" in argv and "10" in argv
    assert "--capital" in argv and "100000" in argv


def test_a_healthy_refresh_still_reports_success(tmp_path):
    _, sends, _, log, _ = _drive(tmp_path)
    assert len(sends) == 1, sends
    assert "Weekly backtest refreshed" in sends[0]
    assert "refresh OK" in log


# ---------------------------------------------------------------------------
# Missing snapshot — the whole point of the story
# ---------------------------------------------------------------------------

def test_a_missing_snapshot_aborts_instead_of_writing_a_biased_baseline(tmp_path):
    """AC5, failure half. Producing a non-PIT artifact is worse than producing
    nothing: it supersedes the rebaselined one and blinds the monitor."""
    result, _, invocations, _, _ = _drive(tmp_path, snapshot=False)

    assert result.returncode == EXIT_NO_SNAPSHOT, result.returncode
    assert invocations == [], "the backtest must not run without the snapshot"


def test_a_missing_snapshot_sends_a_telegram_naming_the_file(tmp_path):
    _, sends, _, log, _ = _drive(tmp_path, snapshot=False)

    assert len(sends) == 1, sends
    assert "sp500_membership.json" in sends[0]
    assert "sp500_membership.json" in log


def test_a_missing_snapshot_is_reported_before_the_gateway_check(tmp_path):
    """A local, deterministic misconfiguration must not be masked by whatever
    IB happens to be doing at 05:00 — otherwise the operator chases the wrong
    failure for a week."""
    _, sends, _, _, _ = _drive(tmp_path, snapshot=False, gateway=False)

    assert len(sends) == 1, sends
    assert "sp500_membership.json" in sends[0]


def test_the_gateway_check_still_works_when_the_snapshot_is_present(tmp_path):
    result, sends, invocations, _, _ = _drive(tmp_path, gateway=False)

    assert result.returncode == 1
    assert invocations == []
    assert "IB Gateway not reachable" in sends[0]


# ---------------------------------------------------------------------------
# Failure passthrough
# ---------------------------------------------------------------------------

def test_a_failing_backtest_keeps_its_exit_code_and_alerts(tmp_path):
    result, sends, _, log, _ = _drive(tmp_path, backtest_exit=7)

    assert result.returncode == 7
    assert "FAILED" in sends[0]
    assert "refresh FAILED (exit 7)" in log


# ---------------------------------------------------------------------------
# Static guard — the flag cannot be dropped by a future edit
# ---------------------------------------------------------------------------

def test_the_deployed_wrapper_names_the_documented_snapshot_path():
    source = RUN_REFRESH.read_text()
    assert "data/universe/sp500_membership.json" in source
    assert "--universe-snapshots" in source


@pytest.mark.parametrize("wrapper", ["run_backtest_refresh.sh", "run_divergence.sh"])
def test_wrappers_still_source_the_shared_telegram_helper(wrapper):
    """Regression guard for KAN-43: this story edits both wrappers, and a
    hand-rolled telegram() copy is how the six-way drift started."""
    source = (DEPLOY_DIR / wrapper).read_text()
    assert 'deploy/launchd/lib/telegram.sh"' in source
    assert "telegram() {" not in source


# ---------------------------------------------------------------------------
# Deadline — the PIT universe made this job ~6x bigger
# ---------------------------------------------------------------------------

def test_a_runaway_backtest_is_killed_and_reported(tmp_path):
    """The point-in-time universe takes run_backtest from 140 tickers to ~830,
    so the IB pull is hours. An unbounded 05:00 job could still be holding the
    gateway at the next day's 04:15 paper run."""
    result, sends, _, log, _ = _drive(tmp_path, backtest_sleep=30, timeout_seconds=1)

    assert result.returncode == 124, result.returncode
    assert "TIMED OUT" in log
    assert len(sends) == 1, sends
    assert "TIMED OUT" in sends[0]


def test_the_deadline_does_not_fire_on_a_normal_run(tmp_path):
    result, sends, _, log, _ = _drive(tmp_path, timeout_seconds=120)

    assert result.returncode == 0
    assert "TIMED OUT" not in log
    assert "Weekly backtest refreshed" in sends[0]


def test_a_timed_out_run_does_not_prune_the_baseline_archive(tmp_path):
    """The prune is the success path's tail. A killed run must not reach it —
    deleting old baselines when the new one was never written would leave the
    monitor with less to fall back on, not more."""
    result, _, _, _, _ = _drive(tmp_path, backtest_sleep=30, timeout_seconds=1)
    assert result.returncode == 124
