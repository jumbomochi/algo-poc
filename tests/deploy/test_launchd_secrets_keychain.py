"""Behavioural tests for the shared launchd secret loader.

Regression cover for the 2026-08-12 outage: 1Password Environments replaced the
repo's ``.env`` with a named pipe, every wrapper read credentials with ``grep
'^POSTGRES_PASSWORD=' .env``, and against an app-backed FIFO that nothing is
serving that open BLOCKS ~60s and returns nothing. The 04:15 paper run and the
04:45 divergence run aborted silently on 2026-08-13 and 2026-08-14.

These drive ``deploy/launchd/secrets.sh`` for real through a stubbed ``security``
binary (``ALGO_SECURITY_BIN``), so they assert behaviour rather than the presence
of strings. The FIFO test is the load-bearing one: if the loader ever goes back
to reading ``.env`` unconditionally it will block and the test fails on timeout
instead of quietly passing.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import textwrap
import time
from pathlib import Path

import pytest

DEPLOY_DIR = Path("deploy/launchd")
SECRETS_SH = DEPLOY_DIR / "secrets.sh"

WRAPPERS = (
    DEPLOY_DIR / "run_paper.sh",
    DEPLOY_DIR / "run_divergence.sh",
    DEPLOY_DIR / "run_pipeline_report.sh",
    DEPLOY_DIR / "run_db_backup.sh",
    DEPLOY_DIR / "run_backtest_refresh.sh",
    DEPLOY_DIR / "gateway_watchdog.sh",
)

# If the loader regresses to reading the pipe, the real failure takes ~60s.
# Anything above this means it blocked.
BLOCKING_BUDGET_S = 15


def _write_security_stub(tmp_path: Path, mode: str, store: dict[str, str] | None = None) -> Path:
    """A fake `security` binary. mode: found | missing | locked."""
    stub = tmp_path / "security-stub"
    # shlex.quote, not repr(): Python's repr is not valid shell quoting, so a
    # password containing a quote character would produce a broken stub and a
    # test failure that looks like a loader bug.
    lines = "\n".join(
        f'  {name}) printf "%s" {shlex.quote(value)}; exit 0 ;;'
        for name, value in (store or {}).items()
    )
    stub.write_text(
        textwrap.dedent(
            f"""\
            #!/bin/bash
            # Args look like: find-generic-password -w -s SERVICE -a ACCOUNT
            acct=""
            while [ $# -gt 0 ]; do
                case "$1" in
                    -a) acct="$2"; shift 2 ;;
                    *) shift ;;
                esac
            done
            case "{mode}" in
              locked)
                echo "security: SecKeychainSearchCopyNext: User interaction is not allowed." >&2
                exit 36
                ;;
              missing)
                echo "security: SecKeychainSearchCopyNext: The specified item could not be found in the keychain." >&2
                exit 44
                ;;
            esac
            case "$acct" in
            {lines}
            esac
            echo "security: SecKeychainSearchCopyNext: The specified item could not be found in the keychain." >&2
            exit 44
            """
        )
    )
    stub.chmod(0o755)
    return stub


def _run_loader(snippet: str, *, env_file: Path, security_bin: Path, home: Path):
    """Source secrets.sh in a real bash and run `snippet` against it."""
    env = dict(os.environ)
    env.update(
        ALGO_SECRETS_ENV_FILE=str(env_file),
        ALGO_SECURITY_BIN=str(security_bin),
        ALGO_KEYCHAIN_SERVICE="algo-poc-test",
        HOME=str(home),
    )
    script = f'. "{SECRETS_SH.resolve()}"\n{snippet}\n'
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=BLOCKING_BUDGET_S,
        env=env,
    )


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "home"
    (h / "ibc" / "logs").mkdir(parents=True)
    return h


def test_keychain_value_is_returned(tmp_path: Path, home: Path):
    stub = _write_security_stub(tmp_path, "found", {"POSTGRES_PASSWORD": "pg-from-keychain"})
    res = _run_loader(
        'algo_secret_into POSTGRES_PASSWORD && printf "%s" "$_ALGO_SECRET_VALUE"',
        env_file=tmp_path / "does-not-exist.env",
        security_bin=stub,
        home=home,
    )
    assert res.returncode == 0, res.stderr
    assert res.stdout == "pg-from-keychain"


def test_env_file_fallback_when_keychain_has_no_item(tmp_path: Path, home: Path):
    env_file = tmp_path / ".env"
    env_file.write_text("POSTGRES_PASSWORD=pg-from-envfile\n")
    stub = _write_security_stub(tmp_path, "missing")
    res = _run_loader(
        'algo_secret_into POSTGRES_PASSWORD && printf "%s" "$_ALGO_SECRET_VALUE"',
        env_file=env_file,
        security_bin=stub,
        home=home,
    )
    assert res.returncode == 0, res.stderr
    assert res.stdout == "pg-from-envfile"


def test_fifo_env_file_is_refused_loudly_and_without_blocking(tmp_path: Path, home: Path):
    """THE regression test for 2026-08-12.

    A `.env` that exists but is not a regular file must fail fast with a named
    error. The original code ran `grep` straight at it, which blocked ~60s per
    read and then reported a bare "not found" that read like a config typo.
    """
    env_file = tmp_path / ".env"
    os.mkfifo(env_file)  # exactly what 1Password Environments leaves behind
    stub = _write_security_stub(tmp_path, "missing")

    started = time.monotonic()
    res = _run_loader(
        'algo_secret_into POSTGRES_PASSWORD; echo "rc=$?"; printf "%s" "$ALGO_SECRETS_ERROR"',
        env_file=env_file,
        security_bin=stub,
        home=home,
    )
    elapsed = time.monotonic() - started

    assert "rc=1" in res.stdout, res.stdout
    assert "NOT a regular file" in res.stdout, res.stdout
    # Name the remedy, not just the symptom.
    assert "--import" in res.stdout, res.stdout
    # A single blocked read cost ~60s in production.
    assert elapsed < 10, f"loader appears to have read the FIFO ({elapsed:.1f}s)"


def test_locked_keychain_is_distinguished_from_a_missing_secret(tmp_path: Path, home: Path):
    """After a reboot with no login the keychain is locked. That is a different
    operator action from "you never imported the secret", so it must not be
    reported as a missing item."""
    stub = _write_security_stub(tmp_path, "locked")
    res = _run_loader(
        'algo_secret_into POSTGRES_PASSWORD; printf "%s" "$ALGO_SECRETS_ERROR"',
        env_file=tmp_path / "does-not-exist.env",
        security_bin=stub,
        home=home,
    )
    assert "LOCKED" in res.stdout, res.stdout
    assert "--import" not in res.stdout, "a locked keychain must not be reported as un-imported"


def test_error_survives_algo_load_secrets(tmp_path: Path, home: Path):
    """`v=$(algo_secret X)` runs in a subshell and loses $ALGO_SECRETS_ERROR.

    The wrappers log that variable after a failed load, so if the loader ever
    goes back to a command substitution internally the operator gets a blank
    reason at 4am — which is how this class of bug stays unnoticed.
    """
    stub = _write_security_stub(tmp_path, "locked")
    res = _run_loader(
        'algo_load_secrets POSTGRES_PASSWORD REDIS_PASSWORD; '
        'echo "rc=$?"; printf "err=[%s]" "$ALGO_SECRETS_ERROR"',
        env_file=tmp_path / "does-not-exist.env",
        security_bin=stub,
        home=home,
    )
    assert "rc=1" in res.stdout, res.stdout
    assert "err=[]" not in res.stdout, "ALGO_SECRETS_ERROR was lost to a subshell"
    assert "LOCKED" in res.stdout, res.stdout


def test_load_secrets_exports_every_requested_name(tmp_path: Path, home: Path):
    stub = _write_security_stub(
        tmp_path, "found", {"POSTGRES_PASSWORD": "pg-val", "REDIS_PASSWORD": "redis-val"}
    )
    res = _run_loader(
        'algo_load_secrets POSTGRES_PASSWORD REDIS_PASSWORD && '
        'printf "%s|%s" "$POSTGRES_PASSWORD" "$REDIS_PASSWORD"',
        env_file=tmp_path / "does-not-exist.env",
        security_bin=stub,
        home=home,
    )
    assert res.returncode == 0, res.stderr
    assert res.stdout == "pg-val|redis-val"


def test_values_with_shell_metacharacters_survive_a_round_trip(tmp_path: Path, home: Path):
    """Generated passwords contain $ ' " ` and spaces; none of them may be
    re-evaluated on the way to the DSN."""
    nasty = "a$b'c\"d`e f&g"
    stub = _write_security_stub(tmp_path, "found", {"POSTGRES_PASSWORD": nasty})
    res = _run_loader(
        'algo_load_secrets POSTGRES_PASSWORD && printf "%s" "$POSTGRES_PASSWORD"',
        env_file=tmp_path / "does-not-exist.env",
        security_bin=stub,
        home=home,
    )
    assert res.returncode == 0, res.stderr
    assert res.stdout == nasty


# --- static assertions across the wrappers ---------------------------------


def _code_lines(path: Path) -> list[str]:
    """Lines with comments stripped, so prose about the old bug doesn't match."""
    out = []
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        out.append(line)
    return out


def test_every_wrapper_sources_the_shared_loader():
    for wrapper in WRAPPERS:
        text = wrapper.read_text()
        assert 'deploy/launchd/secrets.sh"' in text, f"{wrapper} does not source secrets.sh"


def test_no_wrapper_greps_secrets_out_of_the_env_file():
    """The exact construct that hung for ~60s per read against the FIFO."""
    for wrapper in WRAPPERS:
        for line in _code_lines(wrapper):
            for name in ("POSTGRES_PASSWORD", "REDIS_PASSWORD", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
                assert not (
                    "grep" in line and f"^{name}=" in line
                ), f"{wrapper} still greps {name} out of a file: {line.strip()}"


def test_no_wrapper_silently_skips_on_a_missing_env_file():
    """`[ -f "$ENV_FILE" ] || return 0` is FALSE for a FIFO, so it turned a
    dead alerting path into a success. That is what kept the outage quiet."""
    for wrapper in WRAPPERS:
        for line in _code_lines(wrapper):
            assert '[ -f "$ENV_FILE" ]' not in line, (
                f"{wrapper} still short-circuits on a non-regular .env: {line.strip()}"
            )


def test_wrappers_export_compose_vars_before_calling_docker_compose():
    """`docker compose` interpolates ${POSTGRES_PASSWORD:?} by reading `.env`
    itself, bypassing the loader entirely. On 2026-08-13/14 every compose call
    in the pipeline report died with "required variable POSTGRES_PASSWORD is
    missing a value". Exporting first makes compose resolve from the process
    environment, so `.env` is never consulted."""
    # An actual invocation, not the phrase inside an alert string such as
    # `telegram "... (docker compose up?)"`.
    invocation = re.compile(r"(?:^|[;&|]\s*|\$\()\s*docker\s+compose\s")
    for wrapper in WRAPPERS:
        lines = _code_lines(wrapper)
        compose_at = [i for i, ln in enumerate(lines) if invocation.search(ln)]
        if not compose_at:
            continue
        loaded_before = [
            i
            for i, ln in enumerate(lines)
            if "algo_load_secrets" in ln
            and "POSTGRES_PASSWORD" in ln
            and "REDIS_PASSWORD" in ln
        ]
        assert loaded_before, (
            f"{wrapper} invokes docker compose but never exports the interpolated vars"
        )
        assert min(loaded_before) < min(compose_at), (
            f"{wrapper} invokes docker compose at line ~{min(compose_at)} before loading "
            "POSTGRES_PASSWORD/REDIS_PASSWORD"
        )


@pytest.mark.skipif(
    shutil.which("zsh") is None,
    reason="needs a real zsh to drive the loader; the operator's mac ships one, "
    "a bare linux image may not. tests.yml installs it so CI does not skip this.",
)
def test_loader_refuses_to_be_sourced_from_a_non_bash_shell():
    """zsh does not define BASH_SOURCE (so the sourced/executed guard collapsed
    to $0 == $0 and ran the CLI) and does not word-split unquoted parameter
    expansions (so the name list became one bogus account)."""
    res = subprocess.run(
        ["zsh", "-c", f'. "{SECRETS_SH.resolve()}"'],
        capture_output=True,
        text=True,
        timeout=BLOCKING_BUDGET_S,
    )
    assert res.returncode != 0
    assert "bash only" in res.stderr, res.stderr
    assert "--export" in res.stderr, "must point the user at the working alternative"
    # The giveaway that it fell through to the CLI instead of refusing.
    assert "keychain service" not in res.stdout, "sourcing from zsh still ran the CLI"


def test_loader_can_be_sourced_from_bash():
    res = subprocess.run(
        ["bash", "-c", f'. "{SECRETS_SH.resolve()}" && type algo_load_secrets >/dev/null'],
        capture_output=True,
        text=True,
        timeout=BLOCKING_BUDGET_S,
    )
    assert res.returncode == 0, res.stderr


def test_secrets_loader_is_not_deployed_to_ibc():
    """It is sourced by path from the repo; a copy in ~/ibc would be inert and
    would recreate the stale-hand-copy trap from the 2026-08-11 cold boot."""
    text = (DEPLOY_DIR / "deploy.sh").read_text()
    assert 'basename "$f")" = "secrets.sh"' in text, "deploy.sh must skip secrets.sh"


def test_loader_defaults_to_the_absolute_security_binary():
    text = SECRETS_SH.read_text()
    assert 'ALGO_SECURITY_BIN="${ALGO_SECURITY_BIN:-/usr/bin/security}"' in text, (
        "the security binary must default to an absolute path so PATH cannot be hijacked"
    )


def test_loader_is_executable_with_a_shebang():
    assert SECRETS_SH.exists()
    assert os.access(SECRETS_SH, os.X_OK), "secrets.sh must be executable for its CLI modes"
    assert SECRETS_SH.read_text().startswith("#!")
