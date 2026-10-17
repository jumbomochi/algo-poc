"""run_sentiment_collect.sh and its launchd job, driven end to end.

The collector's source keys moved from plaintext in the plist into the login
keychain, the same per-job credential model as the earnings refresh's Alpha
Vantage key. The wrapper is executed against a throwaway ALGO_DIR whose Python
is a stub recording which keys reached its environment; the keychain is a stub
too. Nothing real is reachable.

Pinned: present keys reach the collector and absent ones are named, never
valued; no paper DB password stops the job before Python runs; the plist
carries no secrets and runs the deployed copy in ~/ibc.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
from pathlib import Path

import pytest

DEPLOY_DIR = Path("deploy/launchd")
WRAPPER = DEPLOY_DIR / "run_sentiment_collect.sh"
PLIST = DEPLOY_DIR / "local.algo-sentiment-collect.plist"
FINNHUB_KEY = "STUBFINNHUB24680"

pytestmark = pytest.mark.skipif(os.name != "posix", reason="launchd wrappers are POSIX shell")


def _exec(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)
    return path


def _drive(tmp_path: Path, *, keychain: dict[str, str]) -> dict:
    tree = tmp_path / "algo-dir"
    shutil.copytree(DEPLOY_DIR, tree / "deploy" / "launchd")
    env_log = tmp_path / "python-env.log"
    _exec(
        tree / ".venv" / "bin" / "python",
        "#!/bin/bash\n"
        f'printf "argv %s\\n" "$*" >> "{env_log}"\n'
        "for n in FINNHUB_API_KEY REDDIT_CLIENT_ID REDDIT_CLIENT_SECRET ALGO_DATABASE_URL; do\n"
        f'  [ -n "${{!n:-}}" ] && echo "$n=${{!n}}" >> "{env_log}"\n'
        "done\n"
        "exit 0\n",
    )
    lines = "".join(
        f"  {name}) echo 'security: interaction is not allowed' >&2; exit 36 ;;\n"
        if value == "LOCKED"
        else f"  {name}) echo '{value}' ;;\n"
        for name, value in keychain.items()
    )
    security = _exec(
        tmp_path / "stubs" / "security",
        "#!/bin/bash\n"
        'name=""; prev=""\n'
        'for arg in "$@"; do [ "$prev" = "-a" ] && name="$arg"; prev="$arg"; done\n'
        'case "$name" in\n'
        f"{lines}"
        "  *) echo 'could not be found' >&2; exit 44 ;;\n"
        "esac\n",
    )
    home = tmp_path / "home"
    home.mkdir()
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home),
        "ALGO_DIR": str(tree),
        "ALGO_SECURITY_BIN": str(security),
        "ALGO_OSASCRIPT_BIN": str(_exec(tmp_path / "stubs" / "osascript", "#!/bin/bash\nexit 0\n")),
    }
    result = subprocess.run(
        ["/bin/bash", str(tree / "deploy" / "launchd" / WRAPPER.name)],
        capture_output=True, text=True, timeout=60, env=env,
    )
    return {
        "rc": result.returncode,
        "out": result.stdout + result.stderr,
        "python": env_log.read_text() if env_log.exists() else "",
    }


def test_present_keys_reach_the_collector_and_absent_ones_are_named(tmp_path: Path) -> None:
    run = _drive(tmp_path, keychain={"POSTGRES_PASSWORD": "pg", "FINNHUB_API_KEY": FINNHUB_KEY})
    assert run["rc"] == 0, run
    assert "scripts/collect_sentiment.py --aggregate-days 5" in run["python"]
    assert f"FINNHUB_API_KEY={FINNHUB_KEY}" in run["python"]
    assert "REDDIT_CLIENT_ID=" not in run["python"]
    assert "ALGO_DATABASE_URL=postgresql://algo:pg@localhost:55432/algo_poc" in run["python"]
    assert "keys present: FINNHUB_API_KEY;" in run["out"]
    assert "absent: REDDIT_CLIENT_ID REDDIT_CLIENT_SECRET" in run["out"]
    assert FINNHUB_KEY not in run["out"], "the wrapper printed a key"


def test_a_locked_keychain_is_named_not_reported_as_absent(tmp_path: Path) -> None:
    run = _drive(tmp_path, keychain={"POSTGRES_PASSWORD": "pg", "FINNHUB_API_KEY": "LOCKED"})
    assert run["rc"] == 0, run
    assert "WARNING - FINNHUB_API_KEY unreadable: login keychain is LOCKED" in run["out"]
    # Keys that were simply never imported stay quiet.
    assert "REDDIT_CLIENT_ID unreadable" not in run["out"]


def test_no_paper_db_password_stops_the_job_before_python(tmp_path: Path) -> None:
    run = _drive(tmp_path, keychain={"FINNHUB_API_KEY": FINNHUB_KEY})
    assert run["rc"] == 1
    assert run["python"] == ""


def test_the_plist_carries_no_secrets_and_runs_the_deployed_copy() -> None:
    plist = plistlib.loads(PLIST.read_bytes())
    assert plist["Label"] == PLIST.stem
    assert plist["ProgramArguments"] == [f"/Users/huiliang/ibc/{WRAPPER.name}"]
    assert "EnvironmentVariables" not in plist
    assert plist["StartInterval"] == 3600


def test_the_source_keys_are_job_credentials() -> None:
    """--check reports them without failing the stack's status, --import
    prompts for them, and docker compose never receives them."""
    text = (DEPLOY_DIR / "secrets.sh").read_text()
    job = next(l for l in text.splitlines() if l.startswith("ALGO_JOB_SECRET_NAMES="))
    for name in ("FINNHUB_API_KEY", "REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET"):
        assert name in job, name
