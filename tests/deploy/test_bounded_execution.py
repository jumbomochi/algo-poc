"""One tested bounded-execution helper, measured against the wall clock.

``run_backtest_refresh.sh`` bounded the backtest at 6h with a loop that counted
its own sleeps:

    waited=0
    while [ "$waited" -lt "$REFRESH_TIMEOUT" ]; do
        kill -0 "$BACKTEST_PID" 2>/dev/null || exit 0
        sleep 5
        waited=$((waited + 5))
    done

``sleep`` is suspended when the machine suspends, so on a host that sleeps
``waited`` counts AWAKE seconds while the deadline it defends is a wall-clock
one. On 2026-09-08 a refresh started 06:30:27 with a 6h bound was still running
at 19:12 — **6h42m past its deadline**, with the watchdog subshell alive and
never having fired. ``pmset`` showed four suspends that afternoon.

The run was not merely late, it was unrecoverable and the bound existed to end
it: 16.29 seconds of CPU across 12h42m, stalled at ticker 473 of 826, each fetch
timing out at ~25 minutes and returning ``0 bars``. At that rate the remaining
353 tickers would have taken about 150 hours.

**The sleeping host is not the defect.** That has an operator fix (KAN-77, and
``pmset -a sleep 0``). A deadline is a promise about wall time, and this one
silently became a promise about awake time. The same drift accumulates with no
suspension at all — 4,320 iterations of ``sleep 5`` plus a ``kill -0`` and loop
overhead is strictly more than 21600s — it is simply slower to matter.

**Why one helper.** This was the second bounded call in three weeks that did not
bound: KAN-70's ``docker compose config`` blocked past every notion of a cycle,
and its fix needed a killer subshell whose stdout had to be redirected or
command substitution waited out the whole sleep. There were three ad-hoc
implementations in ``deploy/launchd`` — ``docker_health.sh``'s
``_algo_docker_bounded``, ``branch_guard.sh``'s guarded copy of it, and this
loop — each with its own bugs. The wall-clock rule, the SIGTERM-then-SIGKILL
escalation and the descriptor discipline should be settled once, not
rediscovered per call site.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
LIB = REPO / "deploy/launchd/lib/bounded.sh"
LAUNCHD = REPO / "deploy/launchd"


def _run(snippet: str, *, timeout: int = 60) -> subprocess.CompletedProcess:
    body = f'. "{LIB}"\n{snippet}\n'
    return subprocess.run(
        ["bash", "-c", body], capture_output=True, text=True, timeout=timeout,
        env=dict(os.environ),
    )


def test_a_command_that_finishes_returns_its_output_and_status():
    res = _run('algo_run_bounded 10 printf "hello"; echo "rc=$?"')
    assert "hello" in res.stdout, res.stdout
    assert "rc=0" in res.stdout, res.stdout


def test_a_non_zero_exit_is_passed_through():
    """Callers branch on this. The refresh's exit-code contract (1 gateway,
    2 snapshot, 124 timeout) depends on the child's status surviving."""
    res = _run('algo_run_bounded 10 bash -c "exit 3"; echo "rc=$?"')
    assert "rc=3" in res.stdout, res.stdout


def test_an_overrun_is_killed_and_reports_124():
    """124 is timeout(1)'s conventional code and one the commands used here
    cannot produce themselves."""
    start = time.monotonic()
    res = _run('algo_run_bounded 2 sleep 30; echo "rc=$?"', timeout=30)
    elapsed = time.monotonic() - start
    assert "rc=124" in res.stdout, res.stdout
    assert elapsed < 15, f"took {elapsed:.1f}s — the bound did not fire promptly"


#: A child that ignores SIGTERM, in ONE process. Deliberately not
#: `bash -c "trap '' TERM; sleep 30"`: the whole process group is signalled, so
#: that shell's `sleep` — which traps nothing — would take the TERM and die, and
#: its parent would exit right behind it. The test would go green in two seconds
#: while never reaching SIGKILL at all. A single process that genuinely ignores
#: TERM is the only child that exercises the escalation this asserts.
_IGNORES_TERM = (
    "import signal, time; "
    "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
    "time.sleep(30)"
)


def test_a_child_that_ignores_sigterm_is_still_killed():
    """SIGTERM then SIGKILL. A backtest wedged on a socket read may not take a
    polite signal, and the whole point is that it stops."""
    start = time.monotonic()
    res = _run(
        'ALGO_BOUNDED_KILL_GRACE_SECONDS=2\n'
        f"algo_run_bounded 2 python3 -c '{_IGNORES_TERM}'; echo \"rc=$?\"",
        timeout=40,
    )
    elapsed = time.monotonic() - start
    assert "rc=124" in res.stdout, res.stdout
    # Shortened grace: this asserts the escalation happens, not how long
    # production waits before it.
    assert elapsed < 20, f"took {elapsed:.1f}s — SIGKILL did not follow"


def test_command_substitution_is_not_held_open_by_the_killer():
    """The KAN-70 trap. Command substitution does not return until every
    inherited descriptor is closed, so a killer subshell holding the pipe makes
    `$(...)` wait out the whole bound — turning the guard into the stall it
    exists to prevent. This is the bug that bit while writing KAN-70's fix."""
    start = time.monotonic()
    res = _run('out=$(algo_run_bounded 30 printf "quick"); echo "got=$out"', timeout=25)
    elapsed = time.monotonic() - start
    assert "got=quick" in res.stdout, res.stdout
    assert elapsed < 10, (
        f"took {elapsed:.1f}s — the substitution waited for the killer, not the child"
    )


def test_a_child_that_finishes_early_leaves_no_watchdog_behind():
    """A killer outliving its child would fire at an unrelated pid later, and
    accumulate one stray process per call in a job that runs every 5 minutes."""
    res = _run(
        'algo_run_bounded 20 true\n'
        'sleep 0.5\n'
        'jobs -r | wc -l | tr -d " "',
        timeout=30,
    )
    assert res.stdout.strip().endswith("0"), f"leftover background jobs: {res.stdout!r}"


# ---------------------------------------------------------------------------
# The wall clock, which is the actual defect
# ---------------------------------------------------------------------------


def test_the_deadline_is_read_from_the_clock_not_counted_in_sleeps():
    """The 2026-09-08 defect. A loop that increments by its own sleep interval
    measures awake time; a suspended host then overruns the deadline by however
    long it was asleep — observed at 6h42m past a 6h bound."""
    code = "\n".join(
        line for line in LIB.read_text().splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "date +%s" in code, (
        "the bound is not measured against the wall clock"
    )
    assert "waited + " not in code and "waited+" not in code, (
        "an iteration-counting deadline remains"
    )


def test_no_sleep_counting_deadline_remains_anywhere_in_launchd():
    """The reverse drift: a new call site that grows its own loop reintroduces
    exactly this. Three ad-hoc implementations existed before this helper."""
    offenders: list[str] = []
    for path in sorted(LAUNCHD.rglob("*.sh")):
        if path.name == "bounded.sh":
            continue
        for n, line in enumerate(path.read_text().splitlines(), 1):
            s = line.strip()
            if s.startswith("#"):
                continue
            if "waited=$((waited" in s.replace(" ", ""):
                offenders.append(f"{path.relative_to(REPO)}:{n}: {s}")
    assert not offenders, (
        "sleep-counting deadline loops remain:\n" + "\n".join(offenders)
    )


def test_every_bounded_call_site_uses_the_shared_helper():
    """docker_health.sh and branch_guard.sh each carried a private copy, and
    branch_guard's was a `command -v` guarded duplicate of docker_health's —
    two files, one implementation, no test between them."""
    private: list[str] = []
    for path in sorted(LAUNCHD.rglob("*.sh")):
        if path.name == "bounded.sh":
            continue
        for n, line in enumerate(path.read_text().splitlines(), 1):
            s = line.strip()
            if s.startswith("#"):
                continue
            if s.startswith("_algo_bounded()") or s.startswith("_algo_docker_bounded()"):
                private.append(f"{path.relative_to(REPO)}:{n}")
    assert not private, "private bounded-exec copies remain: " + ", ".join(private)


def test_the_refresh_bounds_its_backtest_through_the_helper():
    text = (LAUNCHD / "run_backtest_refresh.sh").read_text()
    assert "lib/bounded.sh" in text, "the refresh does not source the shared helper"
    assert "algo_run_bounded" in text, "the refresh does not use it"


def test_a_helper_process_the_child_forked_is_killed_too():
    """The bug the branch guard actually hit.

    ``git ls-remote https://<blackholed>`` forks a ``git-remote-https`` helper.
    Signalling only the child left the helper alive holding the same stdout, so
    ``$(algo_run_bounded ... | awk ...)`` waited out the helper's own ~75s
    network timeout with the bound long since fired — the bound looked like it
    worked and the caller hung anyway. The child is started in its own process
    group and the group is signalled.
    """
    start = time.monotonic()
    res = _run(
        'ALGO_BOUNDED_KILL_GRACE_SECONDS=2\n'
        # The outer bash is the child; `sleep 60` is a helper it forked, and it
        # inherits the substitution's pipe exactly as git's helper does.
        'out=$(algo_run_bounded 2 bash -c "sleep 60 & wait"; echo "rc=$?")\n'
        'echo "$out"',
        timeout=40,
    )
    elapsed = time.monotonic() - start
    assert "rc=124" in res.stdout, res.stdout
    assert elapsed < 20, (
        f"took {elapsed:.1f}s — a forked helper outlived the bound and held the pipe"
    )
