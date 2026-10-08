"""KAN-110 — run_earnings_refresh.sh and its launchd job, driven end to end.

The wrapper is executed against a throwaway ALGO_DIR in which the Python that
would call Alpha Vantage is a stub printing what scripts/fetch_earnings.py
prints, and exiting with the code under test. curl (Telegram and the dead-man
ping), the keychain and caffeinate are stubs too. Nothing real is reachable.

Pinned: the dead-man is pinged on exit 0 only; every other outcome alerts; a
missing key aborts before Python runs; the key never reaches the log or a
command line; and the job is scheduled daily, weekends included, in a slot
that clashes with nothing in the KAN-104 chain.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
from pathlib import Path

import pytest

DEPLOY_DIR = Path("deploy/launchd")
WRAPPER = DEPLOY_DIR / "run_earnings_refresh.sh"
PLIST = DEPLOY_DIR / "local.algo-earnings-refresh.plist"
TOPUP_PLIST = DEPLOY_DIR / "local.algo-earnings-topup.plist"
PING_URL = "https://hc.example.test/ping/earnings-1234"
AV_KEY = "STUBAVKEY987654"

pytestmark = pytest.mark.skipif(os.name != "posix", reason="launchd wrappers are POSIX shell")


def _exec(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)
    return path


def _drive(tmp_path: Path, *, exit_code: int, output: str = "", av_key: bool = True,
           args: tuple[str, ...] = ()):
    tree = tmp_path / "algo-dir"
    shutil.copytree(DEPLOY_DIR, tree / "deploy" / "launchd")
    argv_log = tmp_path / "python-argv.log"
    env_log = tmp_path / "python-env.log"
    _exec(
        tree / ".venv" / "bin" / "python",
        "#!/bin/bash\n"
        f'printf "%s\\n" "$*" >> "{argv_log}"\n'
        # Record only whether the key arrived, never its value.
        f'[ -n "${{ALPHAVANTAGE_API_KEY:-}}" ] && echo KEY_PRESENT >> "{env_log}"\n'
        f"cat <<'EOF'\n{output}\nEOF\n"
        f"exit {exit_code}\n",
    )
    curl_log = tmp_path / "curl.log"
    stub_bin = tmp_path / "bin"
    _exec(stub_bin / "curl", f'#!/bin/bash\nprintf "%s\\n" "$*" >> "{curl_log}"\nexit 0\n')
    av_line = f"  ALPHAVANTAGE_API_KEY) echo '{AV_KEY}' ;;\n" if av_key else ""
    security = _exec(
        tmp_path / "stubs" / "security",
        "#!/bin/bash\n"
        'name=""; prev=""\n'
        'for arg in "$@"; do [ "$prev" = "-a" ] && name="$arg"; prev="$arg"; done\n'
        'case "$name" in\n'
        "  TELEGRAM_BOT_TOKEN) echo '123456789:stub' ;;\n"
        "  TELEGRAM_CHAT_ID) echo '-100123' ;;\n"
        f"{av_line}"
        "  *) echo 'could not be found' >&2; exit 44 ;;\n"
        "esac\n",
    )
    home = tmp_path / "home"
    home.mkdir()
    env = {
        "PATH": f"{stub_bin}:/usr/bin:/bin",
        "HOME": str(home),
        "ALGO_DIR": str(tree),
        "ALGO_SECURITY_BIN": str(security),
        "ALGO_OSASCRIPT_BIN": str(_exec(tmp_path / "stubs" / "osascript", "#!/bin/bash\nexit 0\n")),
        "ALGO_CAFFEINATE_BIN": str(tmp_path / "no-caffeinate"),
        "ALGO_DEADMAN_EARNINGS_URL": PING_URL,
        "ALGO_BOUNDED_POLL_SECONDS": "1",
    }
    result = subprocess.run(
        ["/bin/bash", str(tree / "deploy" / "launchd" / WRAPPER.name), *args],
        capture_output=True, text=True, timeout=120, env=env,
    )
    logs = list((home / "ibc" / "logs").glob("earnings_refresh_*.log"))
    return {
        "rc": result.returncode,
        "log": logs[0].read_text() if logs else "",
        "curl": curl_log.read_text().splitlines() if curl_log.exists() else [],
        "argv": argv_log.read_text() if argv_log.exists() else "",
        "key_seen": env_log.exists(),
        "stderr": result.stderr,
    }


def _pings(run) -> list[str]:
    return [c for c in run["curl"] if PING_URL in c]


def _telegrams(run) -> list[str]:
    return [c for c in run["curl"] if "api.telegram.org" in c]


CURRENT = (
    "EARNINGS_REFRESH status=CURRENT calls=21 fetched=20 recent_reporters=3 "
    "live_unfetched=0 recent_missed=0 backfill_remaining=612 fetched_at=2026-10-08T20:45:00+00:00"
)


def test_a_current_run_pings_and_stays_quiet(tmp_path):
    run = _drive(tmp_path, exit_code=0, output=CURRENT)
    assert run["rc"] == 0, run["log"] + run["stderr"]
    assert len(_pings(run)) == 1
    assert _telegrams(run) == []
    assert "earnings refresh OK" in run["log"] and "dead-man switch: pinged" in run["log"]
    assert "scripts/fetch_earnings.py --universe pit" in run["argv"]
    assert run["key_seen"], "the key never reached the fetcher's environment"


def test_a_current_run_with_a_live_ticker_without_data_warns_but_still_pings(tmp_path):
    output = "LIVE_NO_DATA: Alpha Vantage has no reported quarters for ZZZ\n" + CURRENT
    run = _drive(tmp_path, exit_code=0, output=output)
    assert run["rc"] == 0
    assert len(_pings(run)) == 1
    [message] = _telegrams(run)
    assert "ZZZ" in message and "⚠️" in message


@pytest.mark.parametrize(
    "code, marker",
    [(3, "INCOMPLETE"), (2, "FAILED"), (1, "could not run"), (7, "could not run"),
     (75, "SKIPPED"), (124, "TIMED OUT")],
)
def test_every_other_outcome_alerts_and_withholds_the_ping(tmp_path, code, marker):
    summary = f"EARNINGS_REFRESH status=X calls=4 stopped='limit: Note: ...'"
    run = _drive(tmp_path, exit_code=code, output=summary)
    assert run["rc"] == code
    assert _pings(run) == []
    [message] = _telegrams(run)
    assert marker in message
    assert "status=X" in message, "the alert should carry the run's own summary"
    assert f"not pinged (run exited {code})" in run["log"]


def test_a_missing_key_aborts_before_the_fetcher_runs(tmp_path):
    run = _drive(tmp_path, exit_code=0, output=CURRENT, av_key=False)
    assert run["rc"] == 1
    assert run["argv"] == "", "the fetcher ran without a key"
    assert _pings(run) == []
    [message] = _telegrams(run)
    assert "ABORTED" in message and "ALPHAVANTAGE_API_KEY" in message


def test_the_key_never_reaches_the_log_or_a_command_line(tmp_path):
    run = _drive(tmp_path, exit_code=2, output="EARNINGS_REFRESH status=FAILED")
    assert AV_KEY not in run["log"]
    assert all(AV_KEY not in c for c in run["curl"])
    assert AV_KEY not in run["argv"]


def test_an_earlier_runs_summary_is_not_reported_as_this_runs(tmp_path):
    """Two runs on one day share a log; a crash that prints nothing must not
    be narrated with the morning's CURRENT line."""
    first = _drive(tmp_path, exit_code=0, output=CURRENT)
    assert first["rc"] == 0
    # Same day, same HOME: re-drive with a crash that prints no summary.
    tree = tmp_path / "algo-dir"
    python = tree / ".venv" / "bin" / "python"
    python.write_text("#!/bin/bash\necho 'Traceback ...'\nexit 1\n")
    curl_log = tmp_path / "curl.log"
    curl_log.write_text("")
    env = {
        "PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin",
        "HOME": str(tmp_path / "home"),
        "ALGO_DIR": str(tree),
        "ALGO_SECURITY_BIN": str(tmp_path / "stubs" / "security"),
        "ALGO_OSASCRIPT_BIN": str(tmp_path / "stubs" / "osascript"),
        "ALGO_CAFFEINATE_BIN": str(tmp_path / "no-caffeinate"),
        "ALGO_BOUNDED_POLL_SECONDS": "1",
    }
    result = subprocess.run(
        ["/bin/bash", str(tree / "deploy" / "launchd" / WRAPPER.name)],
        capture_output=True, text=True, timeout=120, env=env,
    )
    assert result.returncode == 1
    sends = [c for c in curl_log.read_text().splitlines() if "api.telegram.org" in c]
    assert sends and "status=CURRENT" not in sends[0]
    assert "no summary line" in sends[0]


def _default(text: str, var: str) -> int:
    return int(text.split(f"{var}:-", 1)[1].split("}", 1)[0])


def test_both_runs_are_bounded_so_neither_reaches_the_paper_run():
    text = WRAPPER.read_text()
    assert 'algo_run_bounded "$EARNINGS_TIMEOUT"' in text
    grace = 20  # lib/bounded.sh's kill grace
    full = _default(text, "ALGO_EARNINGS_TIMEOUT_SECONDS")
    topup = _default(text, "ALGO_EARNINGS_TOPUP_TIMEOUT_SECONDS")
    # 04:45 + full must end before the 05:05 top-up starts...
    assert full + grace < 20 * 60
    # ...and 05:05 + top-up before the 05:15 paper run.
    assert topup + grace < 10 * 60


# ---------------------------------------------------------------------------
# The launchd job
# ---------------------------------------------------------------------------


def _slots(plist: Path) -> list[dict]:
    data = plistlib.loads(plist.read_bytes())
    slots = data["StartCalendarInterval"]
    return slots if isinstance(slots, list) else [slots]


def test_the_job_runs_daily_including_weekends():
    """KAN-109 degrades the sleeve once the cache is 2 days old. A Tue-Sat job
    would hand the Tuesday paper run a Saturday fetch every week."""
    data = plistlib.loads(PLIST.read_bytes())
    assert data["Label"] == "local.algo-earnings-refresh"
    assert data["ProgramArguments"] == ["/Users/huiliang/ibc/run_earnings_refresh.sh"]
    [slot] = _slots(PLIST)
    assert "Weekday" not in slot
    assert (slot["Hour"], slot["Minute"]) == (4, 45)


def test_the_slot_precedes_the_paper_run_and_clashes_with_no_other_job():
    [mine] = _slots(PLIST)
    paper = _slots(DEPLOY_DIR / "local.algo-paper-trading.plist")
    assert all((mine["Hour"], mine["Minute"]) < (p["Hour"], p["Minute"]) for p in paper), (
        "the refresh must land before the paper run reads earnings.json"
    )
    for other in DEPLOY_DIR.glob("local.algo-*.plist"):
        if other == PLIST:
            continue
        data = plistlib.loads(other.read_bytes())
        if "StartCalendarInterval" not in data:
            continue
        for slot in _slots(other):
            assert (slot.get("Hour"), slot.get("Minute")) != (mine["Hour"], mine["Minute"]), other.name


def test_the_key_is_a_job_credential_not_a_stack_secret():
    """--check's exit status means "the stack cannot authenticate"; a missing
    vendor key must not change it, and docker compose must never receive it."""
    text = (DEPLOY_DIR / "secrets.sh").read_text()
    required = next(l for l in text.splitlines() if l.startswith("ALGO_SECRET_NAMES="))
    assert "ALPHAVANTAGE" not in required
    job = next(l for l in text.splitlines() if l.startswith("ALGO_JOB_SECRET_NAMES="))
    assert "ALPHAVANTAGE_API_KEY" in job
    export = text[text.index("_algo_cli_export() {"):text.index("_algo_cli_env_file() {")]
    env_file = text[text.index("_algo_cli_env_file() {"):text.index("# Am I being sourced")]
    assert "ALGO_JOB_SECRET_NAMES" not in export + env_file


def _check(tmp_path: Path, *, with_key: bool) -> subprocess.CompletedProcess[str]:
    key_line = "  ALPHAVANTAGE_API_KEY) echo 'k' ;;\n" if with_key else ""
    security = _exec(
        tmp_path / ("sec-with" if with_key else "sec-without"),
        "#!/bin/bash\n"
        'name=""; prev=""\n'
        'for arg in "$@"; do [ "$prev" = "-a" ] && name="$arg"; prev="$arg"; done\n'
        'case "$name" in\n'
        "  POSTGRES_PASSWORD|REDIS_PASSWORD) echo 'x' ;;\n"
        f"{key_line}"
        "  *) echo 'could not be found' >&2; exit 44 ;;\n"
        "esac\n",
    )
    return subprocess.run(
        ["/bin/bash", str(DEPLOY_DIR / "secrets.sh"), "--check"],
        capture_output=True, text=True, timeout=60,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "ALGO_SECURITY_BIN": str(security),
            "ALGO_SECRETS_ENV_FILE": str(tmp_path / "none.env"),
            "ALGO_SECRET_NAMES": "POSTGRES_PASSWORD REDIS_PASSWORD",
            "ALGO_OPTIONAL_SECRET_NAMES": "",
        },
    )


def test_check_reports_the_key_without_failing_on_its_absence(tmp_path):
    present = _check(tmp_path, with_key=True)
    assert present.returncode == 0, present.stdout
    assert "OK      ALPHAVANTAGE_API_KEY" in present.stdout
    absent = _check(tmp_path, with_key=False)
    assert absent.returncode == 0, absent.stdout
    assert "ABSENT  ALPHAVANTAGE_API_KEY" in absent.stdout


# ---------------------------------------------------------------------------
# Review follow-ups (PR #236)
# ---------------------------------------------------------------------------


def test_a_timeout_is_reported_as_a_timeout_and_never_pings(tmp_path):
    """The fetcher exits 124 on SIGTERM; before the fix it exited 2 and this
    branch was dead, so a timeout read as 'Alpha Vantage refused'."""
    run = _drive(tmp_path, exit_code=124, output="EARNINGS_REFRESH mode=full status=TERMINATED")
    assert run["rc"] == 124
    assert _pings(run) == []
    [message] = _telegrams(run)
    assert "TIMED OUT" in message and "FAILED" not in message


def test_live_refused_is_alerted_even_on_a_current_run(tmp_path):
    output = ("LIVE_REFUSED: Alpha Vantage has refused BAD on 3+ consecutive days; "
              "map it in data.earnings.refresh.symbol_overrides\n" + CURRENT)
    run = _drive(tmp_path, exit_code=0, output=output)
    assert run["rc"] == 0 and len(_pings(run)) == 1
    [message] = _telegrams(run)
    assert "BAD" in message and "symbol_overrides" in message


TOPUP_OK = "EARNINGS_REFRESH mode=top-up status=TOPUP calls=2 fetched=2"


def test_the_top_up_runs_the_fetcher_in_top_up_mode_and_never_pings(tmp_path):
    run = _drive(tmp_path, exit_code=0, output=TOPUP_OK, args=("--top-up",))
    assert run["rc"] == 0, run["log"] + run["stderr"]
    assert "scripts/fetch_earnings.py --top-up" in run["argv"]
    assert "--universe" not in run["argv"]
    assert _pings(run) == [] and _telegrams(run) == []
    assert "earnings top-up OK" in run["log"]
    assert "top-up runs never ping" in run["log"]


def test_a_top_up_that_finds_the_lock_held_is_silent(tmp_path):
    run = _drive(tmp_path, exit_code=75, output="EARNINGS_REFRESH mode=top-up status=SKIPPED",
                 args=("--top-up",))
    assert run["rc"] == 75
    assert _telegrams(run) == [] and _pings(run) == []
    assert "SKIPPED" in run["log"]


@pytest.mark.parametrize("code, marker", [(2, "FAILED"), (124, "TIMED OUT"), (1, "could not run")])
def test_a_failing_top_up_alerts_but_never_pings(tmp_path, code, marker):
    run = _drive(tmp_path, exit_code=code, output="EARNINGS_REFRESH mode=top-up status=X",
                 args=("--top-up",))
    assert run["rc"] == code
    assert _pings(run) == []
    [message] = _telegrams(run)
    assert "top-up" in message and marker in message


def test_the_top_up_job_runs_daily_at_05_05_with_the_flag():
    data = plistlib.loads(TOPUP_PLIST.read_bytes())
    assert data["Label"] == "local.algo-earnings-topup"
    assert data["ProgramArguments"] == ["/Users/huiliang/ibc/run_earnings_refresh.sh", "--top-up"]
    [slot] = _slots(TOPUP_PLIST)
    assert "Weekday" not in slot
    assert (slot["Hour"], slot["Minute"]) == (5, 5)
    paper = _slots(DEPLOY_DIR / "local.algo-paper-trading.plist")
    assert all((5, 5) < (p["Hour"], p["Minute"]) for p in paper)


def test_the_documented_daily_budget_fits_the_free_tier():
    """Full (1 calendar + calls_per_run) plus the top-up, with headroom."""
    from shared.config import load_config

    cfg = load_config("config/default.yaml").data.earnings.refresh
    assert 1 + cfg.calls_per_run + cfg.topup_calls <= 21


@pytest.mark.parametrize(
    "doc",
    ["docs/strategy.md", "docs/strategies/portfolio-2026-05.md",
     "docs/operations/divergence-monitor.md"],
)
def test_the_after_market_divergence_is_documented(doc):
    text = Path(doc).read_text()
    assert "KAN-110" in text
    assert "after-market" in text and "Friday" in text
