"""`--check` must answer "is this secret usable", not merely "is it there".

On 2026-09-12 a 192-character JIRA API token was pasted into the `API_KEYS`
field of the 1Password "algo-poc" environment, which serves the repo `.env`.
`deploy/launchd/secrets.sh --check` reported:

    OK      API_KEYS

`API_KEYS` must be ``key:role[,key:role]`` with role in (admin, operator,
viewer). ``services/api/auth.py:91`` parses it **at import time** and raises on
a malformed value, so the `api` service would have refused to start. Behind that
service:

    routes/kill.py:28   require_role("admin")  -> trigger the kill switch
    routes/kill.py:84   require_role("admin")  -> clear a persisted halt

With ``restart: unless-stopped`` it would have crash-looped rather than failed
once, and the first sign would have been at the next ``docker compose up`` — a
reboot or a rebuild, which is exactly when nobody is watching. The emergency
stop would have been unavailable.

Nothing broke only because the running container had been started with the
keychain value and held it in memory; the two stores had silently diverged.

**Why presence is the wrong question.** `--check` is consulted precisely when
someone is already worried. A check that says OK for a value the system cannot
use is worse than no check: it answers confidently, and it answers something
else. That is the same shape as KAN-64, KAN-71 and KAN-80 — a detector whose
output does not mean what its reader takes it to mean.

The idea is already accepted elsewhere in the tree: ``evidence_digest.py``
refuses a ``ALGO_DEADMAN_DIGEST_URL`` that "is not set to an http(s) URL". This
centralises it rather than inventing it.

No test here asserts on a secret *value*, and none may: the point of the feature
is to report shape without disclosing content.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SECRETS_SH = REPO / "deploy/launchd/secrets.sh"

# A well-formed pair, shaped like the real one: 48-char keys, known roles.
GOOD_API_KEYS = f"{'a' * 48}:admin,{'b' * 48}:viewer"
# What was actually pasted: an Atlassian API token, 192 chars, no role.
JIRA_TOKEN_SHAPED = "T" * 192


def _security_stub(tmp_path: Path, store: dict[str, str]) -> Path:
    stub = tmp_path / "security-stub"
    lines = "\n".join(
        f'  {name}) printf "%s" {shlex.quote(value)}; exit 0 ;;'
        for name, value in store.items()
    )
    stub.write_text(textwrap.dedent(f"""\
        #!/bin/bash
        # find-generic-password -w -s SERVICE -a ACCOUNT
        acct=""
        while [ $# -gt 0 ]; do
          case "$1" in -a) acct="$2"; shift 2 ;; *) shift ;; esac
        done
        case "$acct" in
        {lines}
          *) exit 44 ;;
        esac
        """))
    stub.chmod(0o755)
    return stub


def _check(tmp_path: Path, store: dict[str, str]) -> subprocess.CompletedProcess:
    """Run the shipped `secrets.sh --check` against a stubbed keychain."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env_file = tmp_path / "dotenv"
    env_file.write_text("")          # regular, empty: keychain is the source
    env = dict(
        os.environ,
        ALGO_SECRETS_ENV_FILE=str(env_file),
        ALGO_SECURITY_BIN=str(_security_stub(tmp_path, store)),
        ALGO_KEYCHAIN_SERVICE="algo-poc-test",
        HOME=str(home),
        # Keep the run to the names under test so an unrelated required secret
        # cannot decide the exit status.
        ALGO_SECRET_NAMES=" ".join(store) or "API_KEYS",
        ALGO_OPTIONAL_SECRET_NAMES="",
    )
    return subprocess.run(
        [str(SECRETS_SH), "--check"],
        capture_output=True, text=True, timeout=60, env=env,
    )


def _line_for(out: str, name: str) -> str:
    for line in out.splitlines():
        if line.strip().endswith(name) or f" {name} " in line or f" {name}" in line:
            if any(tok in line for tok in ("OK", "MISSING", "MALFORMED")):
                return line.strip()
    return ""


def test_a_jira_token_in_api_keys_is_reported_malformed(tmp_path):
    """The 2026-09-12 case. Against the old presence-only check this reported
    OK, which is how it survived long enough to reach the .env."""
    res = _check(tmp_path, {"API_KEYS": JIRA_TOKEN_SHAPED})
    line = _line_for(res.stdout, "API_KEYS")
    assert "MALFORMED" in line, res.stdout
    assert "OK" not in line.split("API_KEYS")[0], res.stdout


def test_a_malformed_required_secret_exits_non_zero(tmp_path):
    """`--check` is used as a gate. "Retrievable but unusable" is not passing."""
    res = _check(tmp_path, {"API_KEYS": JIRA_TOKEN_SHAPED})
    assert res.returncode != 0, res.stdout


def test_a_well_formed_api_keys_is_ok(tmp_path):
    res = _check(tmp_path, {"API_KEYS": GOOD_API_KEYS})
    assert "OK" in _line_for(res.stdout, "API_KEYS"), res.stdout
    assert res.returncode == 0, res.stdout


def test_an_unknown_role_is_malformed(tmp_path):
    """`root` is not a role. auth.py would raise on it at import time."""
    res = _check(tmp_path, {"API_KEYS": f"{'a' * 48}:root"})
    assert "MALFORMED" in _line_for(res.stdout, "API_KEYS"), res.stdout


def test_a_missing_comma_merging_two_keys_is_malformed(tmp_path):
    """The likeliest paste error after the token case: one entry, two colons."""
    res = _check(tmp_path, {"API_KEYS": f"{'a' * 48}:admin{'b' * 48}:viewer"})
    assert "MALFORMED" in _line_for(res.stdout, "API_KEYS"), res.stdout


def test_a_non_integer_telegram_chat_id_is_malformed(tmp_path):
    """A bad chat id makes every alert fail to deliver, silently — the exact
    class of failure the alerting layer exists to prevent."""
    res = _check(tmp_path, {"TELEGRAM_CHAT_ID": "not-a-number"})
    assert "MALFORMED" in _line_for(res.stdout, "TELEGRAM_CHAT_ID"), res.stdout


def test_a_negative_telegram_chat_id_is_valid(tmp_path):
    """Group chats are negative. Rejecting them would be worse than no check."""
    res = _check(tmp_path, {"TELEGRAM_CHAT_ID": "-1001234567890"})
    assert "OK" in _line_for(res.stdout, "TELEGRAM_CHAT_ID"), res.stdout


def test_a_name_with_no_validator_still_reports_ok_on_presence(tmp_path):
    """Adding a secret must not require adding a validator. The point is to add
    signal, not to make --check fail on anything it does not recognise."""
    res = _check(tmp_path, {"POSTGRES_PASSWORD": "anything at all"})
    assert "OK" in _line_for(res.stdout, "POSTGRES_PASSWORD"), res.stdout
    assert res.returncode == 0, res.stdout


def test_a_missing_secret_is_still_distinct_from_a_malformed_one(tmp_path):
    """Three states, not two: absent and unusable need different fixes."""
    res = _check(tmp_path, {"API_KEYS": JIRA_TOKEN_SHAPED})
    out = res.stdout
    assert "MALFORMED" in out and "MISSING" not in out, out


def test_no_secret_value_is_ever_printed(tmp_path):
    """Including in the MALFORMED branch, which is the tempting place to echo
    'got: <value>'. --check is run in terminals and pasted into tickets."""
    res = _check(tmp_path, {"API_KEYS": JIRA_TOKEN_SHAPED})
    blob = res.stdout + res.stderr
    assert JIRA_TOKEN_SHAPED not in blob, "the malformed value was echoed"
    assert "T" * 40 not in blob, "a long fragment of the value was echoed"

    ok = _check(tmp_path, {"API_KEYS": GOOD_API_KEYS})
    assert "a" * 40 not in ok.stdout + ok.stderr, "a key was echoed"


def test_the_reason_is_stated_without_the_value(tmp_path):
    """"MALFORMED" alone sends nobody anywhere. It has to say what shape was
    expected."""
    res = _check(tmp_path, {"API_KEYS": JIRA_TOKEN_SHAPED})
    line = _line_for(res.stdout, "API_KEYS").lower()
    assert "role" in line or "key:role" in line, res.stdout
