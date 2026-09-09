"""Every scheduled run must hold a power assertion for its own lifetime.

On 2026-09-09 the host was found with ``pmset sleep 1`` — idle sleep after ONE
minute — and 57 sleep events in a day, 37 of them ``Dark Wake Thermal
Emergency``. No wrapper in ``deploy/launchd/`` took a power assertion, so launchd
would wake the machine to fire a job and the machine would then sleep out from
under it. A single 04:15 paper run recorded:

    39 x Error 1100  (connectivity between IBKR and TWS LOST)
    38 x Error 1102  (RESTORED)
    69 x Error 322   (max account summary requests exceeded)
    87 x "0 bars (~61s)"

It reached ticker 88 of 140 in fifteen hours with no usable bars, produced no
shadow series, and the 04:47 divergence monitor exited 3 (BLIND) as a result.
The same signature accounts for the 2026-09-08 refresh wedging at 22 of 826, and
for the "IB Gateway never came up on 7497" aborts on 09-01 and 09-04.

``sudo pmset -a sleep 0`` fixes the host and has been applied. These tests exist
because that is a *host* setting: it does not survive a new machine, a restored
backup, or someone changing power policy for an unrelated reason. The jobs must
be correct on their own. The manual runbook already invokes long jobs under
``caffeinate -is``; the scheduled ones should not be weaker than the manual ones.

**Why re-exec rather than a background caffeinate.** ``exec caffeinate -is "$0"``
makes caffeinate the parent of the run, so the assertion is released by process
exit — there is no cleanup path to forget, and no way for it to outlive the job.
A backgrounded ``caffeinate &`` would need releasing on every abort path, and
three ``caffeinate`` processes were found still holding assertions on 2026-09-09
after their runs had been killed, which is precisely that failure. ``caffeinate``
also propagates the child's exit status verbatim (verified for 0/1/2/143), so
the wrappers' exit-code contracts and dead-man decisions are unaffected.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
LAUNCHD = REPO / "deploy/launchd"
LIB = LAUNCHD / "lib/power.sh"

# The jobs long enough for a sleep to land inside them. gateway_watchdog.sh is
# excluded on purpose: it is a StartInterval job that runs for seconds, and
# re-execing it 288 times a day would be cost with no benefit.
LONG_RUNNING = [
    "run_paper.sh",
    "run_divergence.sh",
    "run_backtest_refresh.sh",
    "run_db_backup.sh",
    "run_evidence_digest.sh",
]


def _stub_caffeinate(path: Path, log: Path) -> None:
    """Records its invocation, then behaves like the real one: run the utility."""
    path.write_text(
        "#!/bin/bash\n"
        f'printf "INVOKED %s\\n" "$*" >> "{log}"\n'
        "shift\n"           # drop the -is flag bundle
        f'"$@"; rc=$?\n'
        f'printf "RELEASED rc=%s\\n" "$rc" >> "{log}"\n'
        "exit $rc\n"
    )
    path.chmod(0o755)


def _run_lib(script_body: str, *, caffeinate: str | None, tmp_path: Path,
             args: str = "") -> subprocess.CompletedProcess:
    """Run a tiny script that sources power.sh and holds an assertion."""
    victim = tmp_path / "victim.sh"
    victim.write_text(
        "#!/bin/bash\n"
        f'LOG_FILE="{tmp_path}/victim.log"\n'
        f'. "{LIB}"\n'
        'algo_hold_power_assertion "$@"\n'
        f"{script_body}\n"
    )
    victim.chmod(0o755)
    env = dict(os.environ)
    if caffeinate is None:
        env["ALGO_CAFFEINATE_BIN"] = str(tmp_path / "definitely-not-here")
    else:
        env["ALGO_CAFFEINATE_BIN"] = caffeinate
    env.pop("ALGO_POWER_HELD", None)
    cmd = [str(victim)] + (args.split() if args else [])
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env)


@pytest.fixture()
def stub(tmp_path):
    log = tmp_path / "caffeinate.log"
    path = tmp_path / "caffeinate"
    _stub_caffeinate(path, log)
    return path, log


def test_the_run_is_re_executed_under_a_power_assertion(stub, tmp_path):
    stub_path, log = stub
    res = _run_lib('echo BODY_RAN', caffeinate=str(stub_path), tmp_path=tmp_path)
    assert res.returncode == 0, res.stderr
    assert "BODY_RAN" in res.stdout, res.stdout
    assert log.exists(), "caffeinate was never invoked"
    assert "INVOKED" in log.read_text(), log.read_text()


def test_the_assertion_covers_idle_and_system_sleep(stub, tmp_path):
    """-i alone still lets the machine sleep on a lid close or a maintenance
    wake, which is where 37 of the 57 sleeps came from."""
    stub_path, log = stub
    _run_lib("true", caffeinate=str(stub_path), tmp_path=tmp_path)
    invoked = [l for l in log.read_text().splitlines() if l.startswith("INVOKED")][0]
    assert "-i" in invoked, invoked
    assert "s" in invoked.split()[1], invoked  # -is bundle


def test_the_assertion_is_released_when_the_run_exits(stub, tmp_path):
    """Released by process exit, not by a cleanup path that can be missed.
    Three orphaned caffeinate processes were found holding assertions on
    2026-09-09 after their runs were killed."""
    stub_path, log = stub
    _run_lib("echo done", caffeinate=str(stub_path), tmp_path=tmp_path)
    body = log.read_text()
    assert "RELEASED" in body, body
    assert body.index("INVOKED") < body.index("RELEASED"), body


def test_the_exit_code_survives_the_re_exec(stub, tmp_path):
    """The wrappers' dead-man decisions and exit-code contracts key on this.
    A swallowed 143 would turn a killed run into a healthy beat."""
    stub_path, log = stub
    for code in (0, 1, 2, 3, 143):
        res = _run_lib(f"exit {code}", caffeinate=str(stub_path), tmp_path=tmp_path)
        assert res.returncode == code, f"expected {code}, got {res.returncode}"


def test_the_run_is_not_re_executed_twice(stub, tmp_path):
    """Without a guard the exec would recurse forever."""
    stub_path, log = stub
    _run_lib("true", caffeinate=str(stub_path), tmp_path=tmp_path)
    assert log.read_text().count("INVOKED") == 1, log.read_text()


def test_arguments_survive_the_re_exec(stub, tmp_path):
    stub_path, _ = stub
    res = _run_lib('printf "ARGS:%s\\n" "$*"', caffeinate=str(stub_path),
                   tmp_path=tmp_path, args="--publish --no-entries-disabled")
    assert "ARGS:--publish --no-entries-disabled" in res.stdout, res.stdout


def test_a_host_without_caffeinate_warns_and_still_runs(tmp_path):
    """A missing power tool must never stop a trading run — that would be
    monitoring causing the outage it exists to prevent."""
    res = _run_lib("echo BODY_RAN", caffeinate=None, tmp_path=tmp_path)
    assert res.returncode == 0, res.stderr
    assert "BODY_RAN" in res.stdout, res.stdout
    log = (tmp_path / "victim.log").read_text()
    assert "WARNING" in log and "power assertion" in log, log


@pytest.mark.parametrize("wrapper", LONG_RUNNING)
def test_every_long_running_wrapper_holds_an_assertion(wrapper):
    text = (LAUNCHD / wrapper).read_text()
    assert "lib/power.sh" in text, f"{wrapper} does not source the power lib"
    assert "algo_hold_power_assertion" in text, f"{wrapper} never holds an assertion"


@pytest.mark.parametrize("wrapper", LONG_RUNNING)
def test_the_assertion_is_taken_before_the_run_starts(wrapper):
    """It must wrap the whole run, not just the tail. Taken after the work
    begins, the sleep it exists to prevent has already landed."""
    lines = (LAUNCHD / wrapper).read_text().splitlines()
    hold = next(i for i, l in enumerate(lines) if "algo_hold_power_assertion" in l
                and not l.strip().startswith("#"))
    start = next(i for i, l in enumerate(lines) if "Starting" in l and ">>" in l)
    assert hold < start, (
        f"{wrapper} takes its power assertion at line {hold+1}, after the run "
        f"announces itself at line {start+1}"
    )


def test_the_short_interval_watchdog_is_left_alone():
    """gateway_watchdog.sh runs every 300s for a couple of seconds. Re-execing
    it 288 times a day is cost with no benefit, and the exclusion should be
    deliberate rather than an oversight."""
    text = (LAUNCHD / "gateway_watchdog.sh").read_text()
    assert "algo_hold_power_assertion" not in text


def test_the_host_power_requirement_is_written_down():
    """The code half cannot fix a host that sleeps between jobs; both halves
    are needed and the operator half has to be discoverable."""
    doc = (REPO / "docs/operations/live-topology.md").read_text().lower()
    assert "pmset" in doc, "live-topology.md does not state the host power requirement"
    assert "sleep 0" in doc, "live-topology.md does not state the required value"


# ---------------------------------------------------------------------------
# End-to-end against the shipped wrappers
# ---------------------------------------------------------------------------
# The structural checks above (sources the lib, calls it, calls it early) were
# NOT sufficient: run_db_backup.sh and run_evidence_digest.sh passed all three
# while failing to re-exec at all, because they hardcoded ALGO_DIR and so
# sourced the lib from a path the test was not writing to. Only running the
# shipped script caught it.


def _drive_wrapper(wrapper: str, tmp_path: Path, *, algo_dir: Path):
    """Run a shipped wrapper with a caffeinate stub that STOPS the run.

    Exiting 99 instead of exec'ing the utility means nothing downstream
    happens — no IB connection, no database, no Telegram — while still proving
    the re-exec was reached.
    """
    log = tmp_path / "caffeinate.log"
    stub = tmp_path / "caffeinate"
    stub.write_text(
        "#!/bin/bash\n"
        f'printf "CALLED %s\\n" "$*" >> "{log}"\n'
        "exit 99\n"
    )
    stub.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    res = subprocess.run(
        ["/bin/bash", str(algo_dir / "deploy/launchd" / wrapper)],
        capture_output=True, text=True, timeout=120,
        env=dict(
            os.environ, HOME=str(home), ALGO_DIR=str(algo_dir),
            ALGO_CAFFEINATE_BIN=str(stub),
        ),
    )
    return res, (log.read_text() if log.exists() else "")


@pytest.mark.parametrize("wrapper", LONG_RUNNING)
def test_every_long_running_wrapper_actually_re_execs(wrapper, tmp_path):
    res, log = _drive_wrapper(wrapper, tmp_path, algo_dir=REPO)
    assert wrapper in log, f"{wrapper} never re-executed under caffeinate: {log!r}"
    assert res.returncode == 99, (
        f"{wrapper} continued past the re-exec (rc={res.returncode})"
    )


@pytest.mark.parametrize("wrapper", LONG_RUNNING)
def test_every_wrapper_honours_the_algo_dir_override(wrapper):
    """Two of these hardcoded it, which made them undrivable and silently
    unprotected in any tree but the operator's. Kept overridable so a test can
    reach them at all — and, as the wrappers' own comments say, never exported
    in a login shell."""
    line = next(
        l for l in (LAUNCHD / wrapper).read_text().splitlines()
        if l.startswith("ALGO_DIR=")
    )
    assert "${ALGO_DIR:-" in line, f"{wrapper} hardcodes ALGO_DIR: {line}"


def test_a_wrapper_whose_power_lib_is_missing_warns_and_still_runs(tmp_path):
    """`. missing.sh` fails without aborting under `set -uo pipefail`, so the
    call would be a bare "command not found" and the run would carry on with no
    assertion and no record. Silence is the failure mode this tranche exists to
    break."""
    fake = tmp_path / "fake"
    (fake / "deploy" / "launchd" / "lib").mkdir(parents=True)
    wrapper = "run_db_backup.sh"
    (fake / "deploy" / "launchd" / wrapper).write_bytes(
        (LAUNCHD / wrapper).read_bytes()
    )
    _drive_wrapper(wrapper, tmp_path, algo_dir=fake)

    logs = list((tmp_path / "home" / "ibc" / "logs").glob("*.log"))
    assert logs, "no log written"
    body = "\n".join(p.read_text() for p in logs)
    assert "WARNING" in body and "power assertion" in body, body[-500:]
