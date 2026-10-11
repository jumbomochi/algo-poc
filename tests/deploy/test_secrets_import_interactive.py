"""KAN-115: `secrets.sh --import` must do what its prompt says.

Incident, 2026-10-09 ~16:16 SGT: to add ONE dead-man URL the operator ran
``deploy/launchd/secrets.sh --import``. It walked every known name and printed
"Enter POSTGRES_PASSWORD (input hidden, empty to skip)", then ran ``security
add-generic-password ... -U -w`` with no value, so ``security`` prompted for the
secret itself. There was no skip: pressing Enter stored an EMPTY
POSTGRES_PASSWORD over the real one, and Ctrl-C did nothing because the terminal
was in password mode. Every job that reads it would have failed at its next run.
``security``'s own prompt also truncates at 128 bytes.

These drive the real script through a pseudo-terminal (it reads from /dev/tty,
as a human would type) against a stub ``security`` (``ALGO_SECURITY_BIN``) that
keeps its "keychain" in a directory and logs every argv and every line fed to
``security -i``. No test touches a real keychain.

The stub models the properties of the real binary the write path depends on,
established against it once by hand:
  * ``security -i`` reads commands from stdin; ``-X`` takes the value as hex;
  * its exit status is that of the last command it ran;
  * an input line longer than ~4 KB is split and the tail is run as a separate
    command whose "unknown command" error echoes it (hence the value cap and the
    stderr redaction).
"""

from __future__ import annotations

import json
import os
import pty
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import termios
import textwrap
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SECRETS_SH = REPO / "deploy/launchd/secrets.sh"

pytestmark = [
    pytest.mark.skipif(sys.platform == "win32", reason="needs a pty"),
    # pty.fork() in a threaded xdist worker warns; the child only calls execve
    # with arguments built before the fork.
    pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning"),
]

STUB = r'''
#!{python}
"""Stub `security`: a directory-backed keychain that logs argv and -i stdin."""
import json, os, sys
from pathlib import Path

ROOT = Path({root!r})
STORE = ROOT / "store"
MODE = (ROOT / "mode").read_text().strip() if (ROOT / "mode").exists() else ""
STORE.mkdir(exist_ok=True)

with open(ROOT / "argv.log", "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")

NOT_FOUND = "security: SecKeychainSearchCopyNext: The specified item could not be found in the keychain."

def opts(args, flags_without_value=()):
    out, i = {{}}, 0
    while i < len(args):
        a = args[i]
        if a in flags_without_value:
            out[a] = True; i += 1
        elif a.startswith("-") and i + 1 < len(args):
            out[a] = args[i + 1]; i += 2
        else:
            i += 1
    return out

def find(args):
    if MODE == "locked":
        print("security: SecKeychainSearchCopyNext: User interaction is not allowed.", file=sys.stderr)
        return 36
    o = opts(args, ("-w", "-g"))
    item = STORE / o.get("-a", "")
    if not o.get("-a") or not item.exists():
        print(NOT_FOUND, file=sys.stderr)
        return 44
    if o.get("-w"):
        sys.stdout.write(item.read_bytes().decode("utf-8", "replace") + "\n")
        return 0
    mdat = (STORE / (o["-a"] + ".mdat")).read_text().strip()
    hexd = "".join(f"{{ord(c):02X}}" for c in mdat + "Z") + "00"
    print(f'keychain: "/stub/login.keychain-db"\nclass: "genp"\nattributes:\n'
          f'    "acct"<blob>="{{o["-a"]}}"\n'
          f'    "mdat"<timedate>=0x{{hexd}}  "{{mdat}}Z\\000"\n'
          f'    "svce"<blob>="{{o.get("-s", "")}}"')
    return 0

def add(args):
    o = opts(args, ("-U", "-A"))
    acct = o.get("-a")
    if not acct or not o.get("-s"):
        print("Usage: add-generic-password [-h] [-a account] [-s service] ...", file=sys.stderr)
        return 2
    if MODE == "write_fails":
        # A real failure message, plus an echo of the hex, as a split line would.
        print("security: SecKeychainItemCreateFromContent (<default>): Unable to obtain authorization for this operation.", file=sys.stderr)
        print(f'security: unknown command "{{o.get("-X", "")}}"', file=sys.stderr)
        return 152
    if "-X" in o:
        try:
            data = bytes.fromhex(o["-X"])
        except ValueError:
            print("security: Unable to convert password data (-X must specify valid hex digits)", file=sys.stderr)
            return 2
    elif "-w" in o:
        data = o["-w"].encode()
    else:
        print("stub: refusing to prompt for a password", file=sys.stderr)
        return 99
    item = STORE / acct
    if item.exists() and "-U" not in args:
        print("security: SecKeychainItemCreateFromContent (<default>): The specified item already exists in the keychain.", file=sys.stderr)
        return 45
    if MODE == "truncate128":
        data = data[:128]
    if MODE == "slow_write":
        (ROOT / "write.started").touch()
        import time
        time.sleep(10)
    item.write_bytes(data)
    (STORE / (acct + ".mdat")).write_text("20261011010203")
    return 0

def run(args):
    if not args:
        return 0
    if args[0] == "find-generic-password":
        return find(args[1:])
    if args[0] == "add-generic-password":
        return add(args[1:])
    print(f'security: unknown command "{{args[0]}}"', file=sys.stderr)
    return 1

if sys.argv[1:] == ["-i"]:
    rc = 0
    for line in sys.stdin:
        with open(ROOT / "stdin.log", "a") as f:
            f.write(line)
        rc = run(line.split())
        if rc:
            print(f"{{line.split()[0]}}: returned {{rc}}", file=sys.stderr)
    sys.exit(rc)
sys.exit(run(sys.argv[1:]))
'''


class Keychain:
    """The stub's state, as the test sees it."""

    def __init__(self, root: Path):
        self.root = root
        self.store = root / "store"
        self.store.mkdir(parents=True, exist_ok=True)
        self.bin = root / "security"
        self.bin.write_text(STUB.format(python=sys.executable, root=str(root)).lstrip())
        self.bin.chmod(0o755)

    def put(self, name: str, value: str, mdat: str = "20261009081616") -> None:
        (self.store / name).write_bytes(value.encode())
        (self.store / f"{name}.mdat").write_text(mdat)

    def get(self, name: str) -> str | None:
        p = self.store / name
        return p.read_bytes().decode() if p.exists() else None

    def mode(self, mode: str) -> None:
        (self.root / "mode").write_text(mode)

    def argv(self) -> list[list[str]]:
        log = self.root / "argv.log"
        return [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []

    def stdin_lines(self) -> list[str]:
        log = self.root / "stdin.log"
        return log.read_text().splitlines() if log.exists() else []

    def writes(self) -> list[str]:
        """Every add-generic-password the stub was asked to run, by any route."""
        out = [" ".join(a) for a in self.argv() if a and a[0] == "add-generic-password"]
        out += [ln for ln in self.stdin_lines() if ln.startswith("add-generic-password")]
        return out


@pytest.fixture
def kc(tmp_path: Path) -> Keychain:
    return Keychain(tmp_path / "kc")


def _env(kc: Keychain, tmp_path: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        ALGO_SECURITY_BIN=str(kc.bin),
        ALGO_KEYCHAIN_SERVICE="algo-poc-test",
        ALGO_SECRETS_ENV_FILE=str(tmp_path / "no.env"),
        HOME=str(tmp_path),
        TERM="dumb",
    )
    return env


class Session:
    """The script running on a pty, driven like an operator at a keyboard."""

    def __init__(self, args: list[str], env: dict[str, str], prefix: list[str] | None = None):
        # The wrapper reports the exit status and the terminal's echo state
        # AFTER secrets.sh is gone — that is the state the operator is left in.
        wrapper = '"$@"; rc=$?; echo; echo "ECHO=$(stty -a | tr " " "\\n" | grep -E "^-?echo$")"; echo "RC=$rc"'
        # Everything is prepared before the fork so the child does nothing but
        # execve (pytest-xdist workers are multi-threaded).
        bash = shutil.which("bash") or "/bin/bash"
        argv = ["bash", "-c", wrapper, "wrapper", *(prefix or []), str(SECRETS_SH), "--import", *args]
        self.pid, self.fd = pty.fork()
        if self.pid == 0:  # child
            try:
                os.execve(bash, argv, env)
            finally:
                os._exit(127)
        self.out = b""
        self.cursor = 0
        self.status: int | None = None

    def _pump(self, timeout: float) -> bool:
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return True
        try:
            data = os.read(self.fd, 4096)
        except OSError:  # EIO: child side closed (Linux)
            return False
        if not data:
            return False
        self.out += data
        return True

    def expect(self, pattern: str, timeout: float = 10) -> str:
        deadline = time.monotonic() + timeout
        rx = re.compile(pattern.encode())
        while time.monotonic() < deadline:
            m = rx.search(self.out, self.cursor)
            if m:
                self.cursor = m.end()
                return m.group(0).decode()
            if not self._pump(0.05):
                break
        raise AssertionError(f"never saw {pattern!r}; transcript:\n{self.text}")

    def echo_off(self, timeout: float = 5) -> None:
        """Wait until the script has switched echo off for a hidden prompt."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not termios.tcgetattr(self.fd)[3] & termios.ECHO:
                return
            time.sleep(0.01)
        raise AssertionError(f"echo never went off; transcript:\n{self.text}")

    def send(self, data: str) -> None:
        os.write(self.fd, data.encode())

    def secret(self, value: str) -> None:
        """Type a value at a hidden prompt."""
        self.expect(r"\(input hidden, empty to skip\): |to confirm: ")
        self.echo_off()
        self.send(value + "\n")

    def finish(self, timeout: float = 15) -> int:
        deadline = time.monotonic() + timeout
        done = re.compile(rb"RC=\d+\r?\n")
        while time.monotonic() < deadline and not done.search(self.out, self.cursor):
            if not self._pump(0.05):
                break
        try:
            os.kill(self.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        os.waitpid(self.pid, 0)
        os.close(self.fd)
        m = re.search(rb"RC=(\d+)", self.out)
        assert m, f"script did not finish; transcript:\n{self.text}"
        self.status = int(m.group(1))
        return self.status

    @property
    def text(self) -> str:
        return self.out.decode(errors="replace").replace("\r\n", "\n")

    @property
    def echo_restored(self) -> bool:
        m = re.search(r"ECHO=(-?echo)", self.text)
        assert m, self.text
        return m.group(1) == "echo"


def _session(kc: Keychain, tmp_path: Path, *args: str) -> Session:
    return Session(list(args), _env(kc, tmp_path))


# --- the incident ------------------------------------------------------------


def test_enter_at_an_existing_secret_keeps_it(kc, tmp_path):
    """The incident keystroke. Enter now means "no", and nothing is written."""
    kc.put("POSTGRES_PASSWORD", "real-pg-password")
    s = _session(kc, tmp_path, "--only", "POSTGRES_PASSWORD")
    s.expect(r"POSTGRES_PASSWORD already set \(16 bytes, modified 2026-10-09 08:16:16 UTC\); overwrite\? \[y/N\] ")
    s.send("\n")
    assert s.finish() == 0, s.text
    assert kc.get("POSTGRES_PASSWORD") == "real-pg-password"
    assert kc.writes() == []
    assert "kept    POSTGRES_PASSWORD (unchanged)" in s.text
    assert "real-pg-password" not in s.text


@pytest.mark.parametrize("answer", ["n", "no", "", "Y please", "x"])
def test_anything_but_an_explicit_yes_keeps_an_existing_secret(kc, tmp_path, answer):
    kc.put("REDIS_PASSWORD", "real-redis")
    s = _session(kc, tmp_path, "--only", "REDIS_PASSWORD")
    s.expect(r"overwrite\? \[y/N\] ")
    s.send(answer + "\n")
    assert s.finish() == 0, s.text
    assert kc.get("REDIS_PASSWORD") == "real-redis"
    assert kc.writes() == []


def test_empty_entry_after_yes_still_writes_nothing(kc, tmp_path):
    """Even having said y, an empty value is a skip, never an empty secret."""
    kc.put("POSTGRES_PASSWORD", "real-pg-password")
    s = _session(kc, tmp_path, "--only", "POSTGRES_PASSWORD")
    s.expect(r"overwrite\? \[y/N\] ")
    s.send("y\n")
    s.secret("")
    assert s.finish() == 0, s.text
    assert kc.get("POSTGRES_PASSWORD") == "real-pg-password"
    assert kc.writes() == []
    assert "skipped POSTGRES_PASSWORD" in s.text


def test_empty_entry_for_a_new_secret_creates_nothing(kc, tmp_path):
    s = _session(kc, tmp_path, "--only", "ALGO_DEADMAN_EARNINGS_URL")
    s.secret("")
    assert s.finish() == 0, s.text
    assert kc.get("ALGO_DEADMAN_EARNINGS_URL") is None
    assert kc.writes() == []
    assert "skipped ALGO_DEADMAN_EARNINGS_URL" in s.text


def test_explicit_yes_overwrites_and_reports_length_not_value(kc, tmp_path):
    kc.put("POSTGRES_PASSWORD", "old-pg-password")
    new = "new-pg-password-0123456789"
    s = _session(kc, tmp_path, "--only", "POSTGRES_PASSWORD")
    s.expect(r"overwrite\? \[y/N\] ")
    s.send("y\n")
    s.secret(new)
    s.secret(new)
    assert s.finish() == 0, s.text
    assert kc.get("POSTGRES_PASSWORD") == new
    assert f"stored  POSTGRES_PASSWORD ({len(new)} bytes, read back intact)" in s.text
    assert new not in s.text and "old-pg-password" not in s.text


# --- --only ------------------------------------------------------------------


def test_only_prompts_for_that_one_name(kc, tmp_path):
    url = "https://hc-ping.com/0f4c3c3e-earnings"
    s = _session(kc, tmp_path, "--only", "ALGO_DEADMAN_EARNINGS_URL")
    s.secret(url)
    s.secret(url)
    assert s.finish() == 0, s.text
    assert kc.get("ALGO_DEADMAN_EARNINGS_URL") == url
    prompted = re.findall(r"Enter (\w+) \(input hidden", s.text)
    assert prompted == ["ALGO_DEADMAN_EARNINGS_URL"], s.text
    # Only that item was looked at, let alone written.
    touched = {a[a.index("-a") + 1] for a in kc.argv() if "-a" in a}
    assert touched == {"ALGO_DEADMAN_EARNINGS_URL"}, kc.argv()


def test_only_is_repeatable_and_keeps_order(kc, tmp_path):
    s = _session(kc, tmp_path, "--only", "TELEGRAM_CHAT_ID", "--only", "ALPHAVANTAGE_API_KEY")
    for v in ("-100123", "-100123", "AVKEY123", "AVKEY123"):
        s.secret(v)
    assert s.finish() == 0, s.text
    assert re.findall(r"Enter (\w+) \(input hidden", s.text) == ["TELEGRAM_CHAT_ID", "ALPHAVANTAGE_API_KEY"]
    assert kc.get("TELEGRAM_CHAT_ID") == "-100123"
    assert kc.get("ALPHAVANTAGE_API_KEY") == "AVKEY123"


@pytest.mark.parametrize("args", [["--only", "NOT_A_SECRET"], ["--only", "REDIS_PASSWORD", "--only", "postgres_password"]])
def test_unknown_only_name_is_refused_before_anything_runs(kc, tmp_path, args):
    res = subprocess.run(
        [str(SECRETS_SH), "--import", *args],
        env=_env(kc, tmp_path), capture_output=True, text=True, timeout=15,
        stdin=subprocess.DEVNULL,
    )
    assert res.returncode == 64, res
    assert "refusing" in res.stderr
    assert "ALGO_DEADMAN_EARNINGS_URL" in res.stderr, "should list the known names"
    assert kc.argv() == [], "an unknown name must be refused before security is run at all"


@pytest.mark.parametrize("args", [["--only"], ["--bogus"]])
def test_malformed_import_arguments_are_a_usage_error(kc, tmp_path, args):
    res = subprocess.run(
        [str(SECRETS_SH), "--import", *args],
        env=_env(kc, tmp_path), capture_output=True, text=True, timeout=15,
        stdin=subprocess.DEVNULL,
    )
    assert res.returncode == 64, res
    assert kc.argv() == []


# --- Ctrl-C / Ctrl-D -----------------------------------------------------------


def test_ctrl_c_at_a_hidden_prompt_aborts_writes_nothing_and_restores_echo(kc, tmp_path):
    kc.put("POSTGRES_PASSWORD", "real-pg-password")
    s = _session(kc, tmp_path, "--only", "POSTGRES_PASSWORD")
    s.expect(r"overwrite\? \[y/N\] ")
    s.send("y\n")
    s.expect(r"\(input hidden, empty to skip\): ")
    s.echo_off()
    s.send("half-typ\x03")
    assert s.finish() == 130, s.text
    assert kc.get("POSTGRES_PASSWORD") == "real-pg-password"
    assert kc.writes() == []
    assert "nothing was written" in s.text
    assert s.echo_restored, f"terminal left with echo off:\n{s.text}"


def test_ctrl_c_at_the_confirmation_prompt_aborts(kc, tmp_path):
    s = _session(kc, tmp_path, "--only", "API_KEYS")
    s.secret("k" * 48 + ":admin")
    s.expect(r"to confirm: ")
    s.echo_off()
    s.send("\x03")
    assert s.finish() == 130, s.text
    assert kc.writes() == []
    assert s.echo_restored


def test_ctrl_c_after_an_earlier_name_was_answered_writes_nothing_at_all(kc, tmp_path):
    """Answers are collected before anything is written, so aborting at the
    second prompt does not leave the first secret half-imported."""
    s = _session(kc, tmp_path, "--only", "TELEGRAM_BOT_TOKEN", "--only", "TELEGRAM_CHAT_ID")
    s.secret("123:bot-token")
    s.secret("123:bot-token")
    s.expect(r"Enter TELEGRAM_CHAT_ID \(input hidden, empty to skip\): ")
    s.echo_off()
    s.send("\x03")
    assert s.finish() == 130, s.text
    assert kc.get("TELEGRAM_BOT_TOKEN") is None
    assert kc.writes() == []


def test_ctrl_c_at_the_overwrite_question_aborts(kc, tmp_path):
    kc.put("REDIS_PASSWORD", "real-redis")
    s = _session(kc, tmp_path, "--only", "REDIS_PASSWORD")
    s.expect(r"overwrite\? \[y/N\] ")
    s.send("\x03")
    assert s.finish() == 130, s.text
    assert kc.get("REDIS_PASSWORD") == "real-redis"
    assert kc.writes() == []
    assert s.echo_restored


def test_ctrl_d_at_a_hidden_prompt_aborts(kc, tmp_path):
    s = _session(kc, tmp_path, "--only", "REDIS_PASSWORD")
    s.expect(r"\(input hidden, empty to skip\): ")
    s.echo_off()
    s.send("\x04")
    assert s.finish() == 1, s.text
    assert kc.writes() == []
    assert "nothing was written" in s.text
    assert s.echo_restored


# --- values --------------------------------------------------------------------


def test_a_200_char_value_round_trips_intact(kc, tmp_path):
    """security's own prompt truncated at 128 bytes. Shell metacharacters and
    the option letters security -i parses must also survive."""
    value = ("a$b'c\"d`e f\\g -X 00 -w ;h|" * 10)[:200]
    assert len(value) == 200
    s = _session(kc, tmp_path, "--only", "ALPHAVANTAGE_API_KEY")
    s.secret(value)
    s.secret(value)
    assert s.finish() == 0, s.text
    assert kc.get("ALPHAVANTAGE_API_KEY") == value
    assert "stored  ALPHAVANTAGE_API_KEY (200 bytes, read back intact)" in s.text


def test_mismatched_confirmation_is_refused_and_nothing_written(kc, tmp_path):
    kc.put("POSTGRES_PASSWORD", "real-pg-password")
    s = _session(kc, tmp_path, "--only", "POSTGRES_PASSWORD")
    s.expect(r"overwrite\? \[y/N\] ")
    s.send("y\n")
    s.secret("new-password")
    s.secret("new-passwrod")
    assert s.finish() == 1, s.text
    assert kc.get("POSTGRES_PASSWORD") == "real-pg-password"
    assert kc.writes() == []
    assert "did not match" in s.text


def test_value_over_the_cap_is_refused_before_security_sees_it(kc, tmp_path):
    """security -i splits a line past ~4 KB and echoes the tail in an error.

    The real cap is 1024 bytes, which is also macOS's canonical-mode tty line
    limit, so a longer line cannot even be typed through a pty here; the cap is
    lowered for the test (the script only lets it go down, never up)."""
    env = _env(kc, tmp_path)
    env["ALGO_IMPORT_MAX_BYTES"] = "100"
    s = Session(["--only", "API_KEYS"], env)
    s.secret("z" * 101)
    assert s.finish() == 1, s.text
    assert kc.writes() == []
    assert "101 bytes is over the 100-byte limit" in s.text


@pytest.mark.parametrize("override", ["99999", "lots", "-5"])
def test_value_cap_cannot_be_raised_or_broken_by_the_environment(override):
    res = subprocess.run(
        ["bash", "-c", f'. "{SECRETS_SH}"; _algo_import_max_bytes'],
        env={**os.environ, "ALGO_IMPORT_MAX_BYTES": override},
        capture_output=True, text=True, timeout=15,
    )
    assert res.stdout == "1024", res


def test_the_value_never_reaches_argv_or_the_terminal(kc, tmp_path):
    value = "Sup3r-Secret-Value-1234567890"
    s = _session(kc, tmp_path, "--only", "REDIS_PASSWORD")
    s.secret(value)
    s.secret(value)
    assert s.finish() == 0, s.text
    assert value not in s.text
    for argv in kc.argv():
        assert value not in " ".join(argv), argv
        assert "-w" not in argv[1:] or argv[0] == "find-generic-password", argv
    # It went through security -i's stdin, hex-encoded.
    assert kc.stdin_lines() == [
        f"add-generic-password -s algo-poc-test -a REDIS_PASSWORD -T {kc.bin} -U -X {value.encode().hex()}"
    ]


def test_a_failed_write_is_reported_without_leaking_the_value(kc, tmp_path):
    kc.mode("write_fails")
    value = "leak-check-value-abcdef"
    s = _session(kc, tmp_path, "--only", "REDIS_PASSWORD")
    s.secret(value)
    s.secret(value)
    assert s.finish() == 1, s.text
    assert "FAILED  REDIS_PASSWORD" in s.text
    assert "Unable to obtain authorization" in s.text, "the OS reason should survive redaction"
    assert value not in s.text
    assert value.encode().hex() not in s.text.lower()


def test_a_write_that_does_not_read_back_is_flagged(kc, tmp_path):
    """The 128-byte truncation class of bug: never report "stored" for a value
    the keychain does not actually hold."""
    kc.mode("truncate128")
    value = "t" * 192
    s = _session(kc, tmp_path, "--only", "ALPHAVANTAGE_API_KEY")
    s.secret(value)
    s.secret(value)
    assert s.finish() == 1, s.text
    assert "WARNING ALPHAVANTAGE_API_KEY was written but does not read back as entered (wrote 192 bytes, read 128)" in s.text
    assert "stored  ALPHAVANTAGE_API_KEY" not in s.text


def test_a_locked_keychain_aborts_before_any_prompt(kc, tmp_path):
    kc.mode("locked")
    s = _session(kc, tmp_path, "--only", "REDIS_PASSWORD")
    assert s.finish() == 1, s.text
    assert "cannot tell whether REDIS_PASSWORD is already set: login keychain is LOCKED" in s.text
    assert "unlock-keychain" in s.text
    assert "Enter REDIS_PASSWORD" not in s.text
    assert kc.writes() == []


def test_full_import_walks_every_known_name_and_skips_on_enter(kc, tmp_path):
    """Plain --import, Enter at every prompt (the incident's keystrokes, on every
    name): existing secrets kept, new ones skipped, nothing written."""
    kc.put("POSTGRES_PASSWORD", "real-pg-password")
    s = _session(kc, tmp_path)
    s.expect(r"POSTGRES_PASSWORD already set .*overwrite\? \[y/N\] ")
    s.send("\n")
    for _ in range(12):
        s.secret("")
    assert s.finish() == 0, s.text
    assert kc.get("POSTGRES_PASSWORD") == "real-pg-password"
    assert kc.writes() == []
    assert s.text.count("skipped ") == 12
    assert "Nothing to write." in s.text


def test_import_without_a_terminal_refuses(kc, tmp_path):
    res = subprocess.run(
        [str(SECRETS_SH), "--import", "--only", "REDIS_PASSWORD"],
        env=_env(kc, tmp_path), capture_output=True, text=True, timeout=15,
        stdin=subprocess.DEVNULL, start_new_session=True,
    )
    assert res.returncode == 1, res
    assert "--import-from-env" in res.stderr
    assert kc.writes() == []


# --- static -------------------------------------------------------------------


def test_import_never_lets_security_prompt_for_a_value():
    """`-w` as the last option makes security prompt itself: no skip, -U
    overwrites, 128-byte buffer. The interactive path must not use it."""
    text = SECRETS_SH.read_text()
    assert "_algo_keychain_put_interactive" not in text
    for line in text.splitlines():
        code = line.split("#", 1)[0].rstrip()
        assert not re.search(r"-U\s+-w\s*$", code), line
        assert not code.endswith(" -w"), line


def test_readme_documents_only_and_the_new_import_behaviour():
    readme = (REPO / "deploy/launchd/README.md").read_text()
    assert "--import --only" in readme
    assert "overwrite? [y/N]" in readme


def test_a_locked_keychain_is_refused_before_the_first_prompt_even_for_a_later_name(kc, tmp_path):
    """Every item is looked up before anything is asked, so a lookup failure on
    the SECOND name does not surface after the operator typed the first."""
    kc.mode("locked")
    s = _session(kc, tmp_path, "--only", "REDIS_PASSWORD", "--only", "API_KEYS")
    assert s.finish() == 1, s.text
    assert "Enter " not in s.text
    assert "LOCKED" in s.text
    assert kc.writes() == []


def test_the_keychain_dialog_warning_comes_before_any_security_call(kc, tmp_path):
    kc.put("REDIS_PASSWORD", "real-redis")
    s = _session(kc, tmp_path, "--only", "REDIS_PASSWORD")
    s.expect(r"a macOS keychain dialog may appear — approve it, or Ctrl-C to abort")
    s.expect(r"overwrite\? \[y/N\] ")
    s.send("\n")
    assert s.finish() == 0, s.text


def test_ctrl_c_during_the_write_phase_says_one_more_may_have_landed(kc, tmp_path):
    kc.mode("slow_write")
    s = _session(kc, tmp_path, "--only", "REDIS_PASSWORD")
    s.secret("slow-value")
    s.secret("slow-value")
    deadline = time.monotonic() + 10
    while not (kc.root / "write.started").exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    s.send("\x03")
    assert s.finish() == 130, s.text
    assert "one more may have been stored just before the interrupt" in s.text
    assert "--check" in s.text
    assert s.echo_restored


# --- xtrace (review follow-up) ----------------------------------------------

XTRACE_VALUE = "Xtrace-Leak-Probe-Value-42"


@pytest.mark.parametrize(
    "how",
    ["bash -x", "SHELLOPTS=xtrace", "SHELLOPTS=xtrace + BASH_XTRACEFD=1"],
)
def test_xtrace_never_shows_the_value_or_its_hex(kc, tmp_path, how):
    """`bash -x secrets.sh --import`, or an exported SHELLOPTS=xtrace, would
    otherwise trace every assignment and command line carrying the value."""
    kc.put("REDIS_PASSWORD", "existing-redis-secret")
    env = _env(kc, tmp_path)
    bash = "/bin/bash" if Path("/bin/bash").exists() else (shutil.which("bash") or "bash")
    if how == "bash -x":
        prefix = [bash, "-x"]
    else:
        prefix = ["env", "SHELLOPTS=xtrace"]
        if "BASH_XTRACEFD" in how:
            prefix.append("BASH_XTRACEFD=1")
    s = Session(["--only", "REDIS_PASSWORD"], env, prefix=prefix)
    s.expect(r"overwrite\? \[y/N\] ")
    s.send("y\n")
    s.secret(XTRACE_VALUE)
    s.secret(XTRACE_VALUE)
    assert s.finish() == 0, s.text
    assert kc.get("REDIS_PASSWORD") == XTRACE_VALUE
    # The trace really was on, so the assertions below are not vacuous.
    assert re.search(r"^\++ ", s.text, re.M), "expected xtrace output before the import began"
    for secret in (XTRACE_VALUE, "existing-redis-secret"):
        assert secret not in s.text, f"{secret!r} leaked via xtrace:\n{s.text}"
        assert secret.encode().hex() not in s.text.lower(), f"hex of {secret!r} leaked"


# --- sourcing stays side-effect free (review follow-up) ---------------------


def test_sourcing_sets_no_import_state():
    """Every launchd job sources secrets.sh. The import path must only define
    functions at source time: the variables sourcing creates are exactly the
    ones it created before KAN-115."""
    script = textwrap.dedent(
        f"""\
        compgen -v | sort > "$1"
        . "{SECRETS_SH}"
        compgen -v | sort > "$2"
        """
    )
    with tempfile.TemporaryDirectory() as d:
        before, after = Path(d) / "before", Path(d) / "after"
        res = subprocess.run(
            ["bash", "-c", script, "probe", str(before), str(after)],
            capture_output=True, text=True, timeout=15,
            env={k: v for k, v in os.environ.items() if not k.startswith(("ALGO_", "_ALGO"))},
        )
        assert res.returncode == 0, res
        added = set(after.read_text().split()) - set(before.read_text().split())
    added -= {"_", "BASH_ARGC", "BASH_ARGV", "BASH_LINENO", "BASH_SOURCE", "FUNCNAME", "PIPESTATUS"}
    assert added == {
        "ALGO_KEYCHAIN_SERVICE",
        "ALGO_SECRETS_ENV_FILE",
        "ALGO_SECURITY_BIN",
        "ALGO_OSASCRIPT_BIN",
        "ALGO_SECRET_NAMES",
        "ALGO_OPTIONAL_SECRET_NAMES",
        "ALGO_JOB_SECRET_NAMES",
        "ALGO_SECRETS_ERROR",
        "_ALGO_SECRET_VALUE",
        "_algo_sourced",
    }, sorted(added)
