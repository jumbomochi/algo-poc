"""KAN-43: a divergence BREACH must notify someone.

Two layers: the pure renderer (``scripts/ops/divergence_alert.py``) and the
launchd wrapper driven end-to-end with ``curl`` stubbed, so the assertion is on
the actual send, not on grepping the script.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.ops.divergence_alert import MAX_MESSAGE_CHARS, render_alert

REPO = Path(__file__).resolve().parents[2]


def _report(*reports, execution_model=None):
    payload = {
        "generated_at": "2026-08-16T00:00:00+00:00",
        "backtest_source": "output/backtest_multi_20260801_000000.json",
        "window_days": 30,
        "threshold": 0.2,
        "reports": list(reports),
    }
    if execution_model is not None:
        payload["baseline_execution_model"] = execution_model
    return payload


def _portfolio(name, status, **kw):
    base = {
        "portfolio": name,
        "status": status,
        "days_compared": 30,
        "live_return": -0.032,
        "backtest_return": 0.041,
        "absolute_divergence_pp": -0.073,
        "relative_divergence": -1.78,
        "daily_correlation": 0.12,
        "baseline_comparable": True,
        "notes": [],
    }
    base.update(kw)
    return base


def test_exit_zero_renders_nothing():
    assert render_alert(0, _report(_portfolio("momentum", "OK"))) is None


def test_breach_names_every_breaching_sleeve_and_its_numbers():
    msg = render_alert(
        1,
        _report(
            _portfolio("momentum", "BREACH"),
            _portfolio("value", "OK"),
            _portfolio(
                "_aggregate", "BREACH",
                absolute_divergence_pp=-0.051, relative_divergence=-0.9,
            ),
        ),
    )
    assert "BREACH" in msg
    assert "momentum" in msg
    assert "_aggregate" in msg
    assert "value" not in msg, "a non-breaching sleeve must not be named"
    # The numbers, not just the name.
    assert "-7.30 pp" in msg
    assert "-5.10 pp" in msg


def test_hard_error_names_the_failure_from_the_log():
    log = (
        "Mon Aug 16 04:45:00 SGT 2026: Starting daily divergence monitor\n"
        "  Backtest source: output/backtest_multi_20260801_000000.json\n"
        "ERROR: Could not load paper state from DB (postgresql://...):\n"
    )
    msg = render_alert(2, None, log_tail=log)
    assert "HARD ERROR" in msg
    assert "Could not load paper state from DB" in msg


def test_hard_error_redacts_credentials_from_the_db_url():
    """The monitor prints the full DSN on a connection failure, and that DSN
    carries the live Postgres password. Telegram is not a secret store."""
    log = (
        "ERROR: Could not load paper state from DB "
        "(postgresql://algo:sup3r-s3cret@localhost:55432/algo_poc):\n"
    )
    msg = render_alert(2, None, log_tail=log)
    assert "sup3r-s3cret" not in msg, msg
    assert "postgresql://algo:***@localhost:55432/algo_poc" in msg, msg


def test_hard_error_still_alerts_when_the_log_has_no_error_line():
    msg = render_alert(2, None, log_tail="nothing useful here\n")
    assert "HARD ERROR" in msg


def test_blind_names_each_unmet_requirement():
    msg = render_alert(
        3,
        _report(
            _portfolio("momentum", "NO_DATA", baseline_comparable=False),
            execution_model={
                "fill_model": "same_bar",
                "slippage_bps": 10.0,
                "commission_per_share": 0.005,
                "commission_minimum": 0.0,
                "point_in_time_universe": False,
                "coverage_state": "MISSING",
            },
        ),
    )
    assert "BLIND" in msg
    assert "same_bar" in msg
    assert "commission floor" in msg
    assert "survivorship" in msg
    assert "never measured" in msg


def test_blind_falls_back_to_report_notes_without_an_execution_model():
    msg = render_alert(
        3,
        _report(
            _portfolio(
                "momentum", "NO_DATA", baseline_comparable=False,
                notes=["baseline is not comparable: stale universe"],
            ),
        ),
    )
    assert "BLIND" in msg
    assert "stale universe" in msg


def test_blind_alerts_even_with_an_unreadable_report():
    # A missing report must never mute the alert — silence is the failure mode
    # this story exists to remove.
    msg = render_alert(3, None)
    assert "BLIND" in msg


# --- the pinned baseline's two named refusals (KAN-51) ---------------------
#
# Both exit 3, and neither writes a report — the monitor returns before it has
# scored anything, precisely so a session that was never judged does not land in
# the evidence store as a NO_DATA observation. So the reason has to come from the
# log, the way the exit-4 age line already does.


def test_blind_names_a_missing_pin_instead_of_saying_not_comparable():
    """"the baseline backtest is not comparable to live" is wrong for this
    failure and sends the operator to regenerate an artifact that is fine. The
    pin is what is broken."""
    msg = render_alert(
        3, None,
        log_tail=(
            "  Backtest source: output/backtest_multi_20260819_183451.json\n"
            "  \u26a0 BASELINE_PIN_MISSING: the pinned baseline "
            "output/backtest_multi_20260819_183451.json is absent or unreadable. "
            "Refusing to fall back to the newest artifact in output/.\n"
        ),
    )

    assert "BLIND" in msg
    assert "BASELINE_PIN_MISSING" in msg
    assert "backtest_multi_20260819_183451.json" in msg
    assert "not comparable to live" not in msg


def test_blind_names_a_shape_mismatch_and_both_sleeve_counts():
    msg = render_alert(
        3, None,
        log_tail=(
            "  \u26a0 BASELINE_SHAPE_MISMATCH: the pinned baseline describes 6 "
            "sleeve(s) and the live book has 1 (baseline sleeves absent from "
            "live: earnings_drift, momentum).\n"
        ),
    )

    assert "BLIND" in msg
    assert "BASELINE_SHAPE_MISMATCH" in msg
    assert "6 sleeve(s)" in msg and "earnings_drift" in msg


def test_a_pin_failure_line_from_an_earlier_run_is_not_narrated_as_this_one():
    """The log-offset the wrapper passes already bounds the tail to this run, so
    the renderer only ever sees this run's lines. Guard that the *last* one wins
    when a single run somehow logs both."""
    msg = render_alert(
        3, None,
        log_tail=(
            "  \u26a0 BASELINE_PIN_MISSING: an earlier line\n"
            "  \u26a0 BASELINE_SHAPE_MISMATCH: the live book has 1\n"
        ),
    )

    assert "BASELINE_SHAPE_MISMATCH" in msg
    assert "BASELINE_PIN_MISSING" not in msg


def test_the_ordinary_blind_message_is_unchanged_without_a_pin_failure():
    """Regression guard: the pre-existing exit-3 text is what a genuinely
    non-comparable baseline still gets."""
    msg = render_alert(
        3,
        _report(
            _portfolio(
                "momentum", "NO_DATA", baseline_comparable=False,
                notes=["baseline is not comparable: stale universe"],
            ),
        ),
        log_tail="  Backtest source: output/backtest_multi_20260819_183451.json\n",
    )

    assert "not comparable to live" in msg
    assert "stale universe" in msg
    assert "BASELINE_PIN_MISSING" not in msg


def test_cli_prints_nothing_on_exit_zero(tmp_path):
    report = tmp_path / "divergence.json"
    report.write_text(json.dumps(_report(_portfolio("momentum", "OK"))))
    res = subprocess.run(
        [sys.executable, str(REPO / "scripts/ops/divergence_alert.py"),
         "--exit-code", "0", "--report", str(report), "--log", os.devnull],
        capture_output=True, text=True, cwd=REPO, timeout=60,
    )
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == ""


def test_cli_prints_the_breach_message(tmp_path):
    report = tmp_path / "divergence.json"
    report.write_text(json.dumps(_report(_portfolio("momentum", "BREACH"))))
    res = subprocess.run(
        [sys.executable, str(REPO / "scripts/ops/divergence_alert.py"),
         "--exit-code", "1", "--report", str(report), "--log", os.devnull],
        capture_output=True, text=True, cwd=REPO, timeout=60,
    )
    assert res.returncode == 0, res.stderr
    assert "momentum" in res.stdout


# ---------------------------------------------------------------------------
# Review follow-ups: redaction, length, staleness, the fallback path
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("password", [
    "sup3r-s3cret",
    "p@ssw0rd",          # an '@' in the secret used to split the match
    "Tr0ub4dor@3",
    "pa ss",             # whitespace used to defeat the match entirely
    "a:b@c d@e",
])
def test_hard_error_redacts_any_password_from_the_db_url(password):
    """The monitor prints the full DSN on a connection failure and the DSN
    carries the live Postgres password verbatim — secrets.sh imports whatever
    the operator typed, so '@' and spaces are both legal. Anchoring on the
    first '@' leaked the tail; the match now runs to the last one."""
    log = (
        "ERROR: Could not load paper state from DB "
        f"(postgresql://algo:{password}@localhost:55432/algo_poc):\n"
    )
    msg = render_alert(2, None, log_tail=log)
    assert password not in msg, msg
    assert "***@localhost:55432/algo_poc" in msg, msg


def test_hard_error_redacts_a_dsn_with_no_username():
    log = "ERROR: bad DSN postgresql://:only-a-password@localhost/algo_poc\n"
    msg = render_alert(2, None, log_tail=log)
    assert "only-a-password" not in msg, msg


def test_a_huge_message_is_truncated_below_the_telegram_limit():
    """Telegram rejects sendMessage over 4096 chars with HTTP 400, and the
    wrapper discards curl's status — so an over-long body is silently dropped.
    That is silence on a BREACH, the exact failure this story removes."""
    msg = render_alert(2, None, log_tail="ERROR: " + ("x" * 20000) + "\n")
    assert len(msg) <= MAX_MESSAGE_CHARS
    assert "truncated" in msg


def test_truncation_keeps_the_headline():
    many = _report(*[
        _portfolio(f"sleeve_{i:03d}", "BREACH") for i in range(400)
    ])
    msg = render_alert(1, many)
    assert len(msg) <= MAX_MESSAGE_CHARS
    assert "BREACH" in msg
    assert "truncated" in msg


def test_a_stale_report_is_ignored_rather_than_reported_as_this_run(tmp_path):
    """`sys.exit(main())` means ANY uncaught exception exits 1, so exit 1 does
    not by itself prove a breach. Yesterday's — or this morning's — report must
    not be narrated as though this run had computed it."""
    report = tmp_path / "divergence.json"
    report.write_text(json.dumps(_report(_portfolio("momentum", "BREACH"))))
    os.utime(report, (1_000_000, 1_000_000))
    res = subprocess.run(
        [sys.executable, str(REPO / "scripts/ops/divergence_alert.py"),
         "--exit-code", "1", "--report", str(report), "--log", os.devnull,
         "--not-before", "2000000"],
        capture_output=True, text=True, cwd=REPO, timeout=60,
    )
    assert res.returncode == 0, res.stderr
    assert "momentum" not in res.stdout, res.stdout
    # Still alerts — a stale report must not buy silence either.
    assert "BREACH" in res.stdout


def test_a_fresh_report_is_used(tmp_path):
    report = tmp_path / "divergence.json"
    report.write_text(json.dumps(_report(_portfolio("momentum", "BREACH"))))
    res = subprocess.run(
        [sys.executable, str(REPO / "scripts/ops/divergence_alert.py"),
         "--exit-code", "1", "--report", str(report), "--log", os.devnull,
         "--not-before", "1000000"],
        capture_output=True, text=True, cwd=REPO, timeout=60,
    )
    assert res.returncode == 0, res.stderr
    assert "momentum" in res.stdout


def test_log_offset_scopes_the_error_to_this_run(tmp_path):
    """A day's log holds every run. Without an offset the alert would quote an
    earlier run's ERROR as this one's cause."""
    log = tmp_path / "divergence.log"
    earlier = "ERROR: an earlier run's unrelated failure\n"
    log.write_text(earlier + "ERROR: this run could not reach the DB\n")
    res = subprocess.run(
        [sys.executable, str(REPO / "scripts/ops/divergence_alert.py"),
         "--exit-code", "2", "--log", str(log),
         "--log-offset", str(len(earlier))],
        capture_output=True, text=True, cwd=REPO, timeout=60,
    )
    assert res.returncode == 0, res.stderr
    assert "this run could not reach the DB" in res.stdout
    assert "earlier run" not in res.stdout


# ---------------------------------------------------------------------------
# The shared telegram() helper (AC5)
# ---------------------------------------------------------------------------

DEPLOY_DIR = REPO / "deploy" / "launchd"
TELEGRAM_LIB = DEPLOY_DIR / "lib" / "telegram.sh"
WRAPPERS = (
    DEPLOY_DIR / "run_paper.sh",
    DEPLOY_DIR / "run_divergence.sh",
    DEPLOY_DIR / "run_pipeline_report.sh",
    DEPLOY_DIR / "run_db_backup.sh",
    DEPLOY_DIR / "run_backtest_refresh.sh",
    DEPLOY_DIR / "gateway_watchdog.sh",
)


def test_the_shared_telegram_helper_exists():
    assert TELEGRAM_LIB.is_file(), "deploy/launchd/lib/telegram.sh is missing"
    text = TELEGRAM_LIB.read_text()
    assert "telegram()" in text
    assert "api.telegram.org" in text


def test_no_wrapper_defines_its_own_telegram_copy():
    """AC5. Six copies of a credential-reading function is a maintenance
    hazard: a fix to one (a timeout, say) silently misses the other five."""
    offenders = [w.name for w in WRAPPERS if "\ntelegram() {" in w.read_text()]
    assert offenders == [], f"wrappers still define their own telegram(): {offenders}"


def test_every_wrapper_sources_the_shared_helper():
    for wrapper in WRAPPERS:
        text = wrapper.read_text()
        assert 'deploy/launchd/lib/telegram.sh"' in text, (
            f"{wrapper.name} does not source the shared telegram helper"
        )
        assert "ALGO_JOB_LABEL=" in text, (
            f"{wrapper.name} must name itself for the 'cannot alert' path"
        )


def test_the_shared_helper_is_not_deployed_to_ibc():
    """It is sourced by path from the repo, like secrets.sh and deadman.sh. A
    copy under ~/ibc would be a decoy an operator could edit with no effect."""
    deploy_sh = (DEPLOY_DIR / "deploy.sh").read_text()
    assert '"$SRC"/*.sh' in deploy_sh, (
        "deploy.sh no longer globs $SRC/*.sh — re-check that lib/ stays undeployed"
    )
    assert '"$SRC"/lib' not in deploy_sh


# ---------------------------------------------------------------------------
# The wrapper, driven end-to-end (AC1-AC4)
# ---------------------------------------------------------------------------

RUN_DIVERGENCE = DEPLOY_DIR / "run_divergence.sh"

#: Dead-man ping URL the wrapper is pointed at under test (KAN-56); held apart
#: from the Telegram sends in the same ``curl`` log by its host.
DIVERGENCE_DEADMAN_URL = "https://hc.example.test/ping/divergence-1234"


#: Where the stub monitor records the argv the wrapper handed it, one argument
#: per line wrapped in <> so an *empty* argument is still visible — which is the
#: whole point for the pinned-baseline tests (KAN-51): an unresolvable pin must
#: arrive at the monitor as an empty --backtest, not as a silently omitted flag.
MONITOR_ARGV_LOG = "monitor_argv.log"


def _monitor_argv(tmp_path) -> list[str]:
    path = tmp_path / MONITOR_ARGV_LOG
    if not path.exists():
        return []
    return [line[1:-1] for line in path.read_text().splitlines()]


def _drive_wrapper(tmp_path, exit_code, report_payload=None, curl_exit=0,
                   renderer_fails=False, serve_credentials=True,
                   stale_report=None, serve_redis=True,
                   deadman_url=DIVERGENCE_DEADMAN_URL,
                   pin=None, pin_resolver_fails=False):
    """Run run_divergence.sh end-to-end against a stubbed monitor and curl.

    Everything the wrapper reaches out to is stubbed on PATH: ``nc`` (the DB
    port wait), ``curl`` (the send, recorded to a file), ``osascript`` (the
    local alert, which would otherwise raise a real desktop notification on the
    developer's machine) and ``security`` (the keychain, which serves the
    credentials — the production path, so the .env fallback is never reached
    and the repo's 1Password FIFO can never be read).

    The fake python dispatches: the alert renderer is the real thing run under
    this interpreter, everything else is the monitor stub. In production both
    are the same interpreter, so the split is a test artifact only.
    """
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl_log = tmp_path / "curl.log"
    ping_log = tmp_path / "pings.log"

    def stub(name, body):
        p = bin_dir / name
        p.write_text(body)
        p.chmod(0o755)
        return p

    stub("nc", "#!/bin/bash\nexit 0\n")
    stub("osascript", "#!/bin/bash\nexit 0\n")
    # `security find-generic-password -w -s <service> -a <NAME>` — the name is
    # always the last argument.
    stub("security", """#!/bin/bash
case "${@: -1}" in
  POSTGRES_PASSWORD)  echo "stub-pg" ;;
""" + ("""  REDIS_PASSWORD)     echo "stub-redis" ;;
""" if serve_redis else "") + ("""  TELEGRAM_BOT_TOKEN) echo "stub-token" ;;
  TELEGRAM_CHAT_ID)   echo "stub-chat" ;;
""" if serve_credentials else "") + """  *) echo "could not be found" >&2; exit 44 ;;
esac
""")
    # Record every argument on its own line so the test can assert on the
    # message body exactly as passed, not on a re-quoted rendering of it.
    # Dead-man pings are split out of the Telegram sends by host, so the
    # existing "exactly one message" assertions keep meaning exactly that.
    stub("curl", f"""#!/bin/bash
if printf '%s\\n' "$@" | grep -q 'hc.example.test'; then
    printf '%s\\n' "$*" >> {ping_log}
else
    {{ for a in "$@"; do printf '%s\\n' "$a"; done; printf -- '---END---\\n'; }} >> {curl_log}
fi
exit {curl_exit}
""")

    # The report is staged as a fixture and copied into place *by the stub
    # monitor*, exactly as the real monitor writes it mid-run. Pre-writing it
    # would be rejected by the wrapper's staleness guard — correctly, since a
    # report older than the run start belongs to an earlier run.
    report = tmp_path / "divergence.json"
    fixture = tmp_path / "fixture.json"
    if report_payload is not None:
        fixture.write_text(json.dumps(report_payload))
    if stale_report is not None:
        # Left behind by an earlier run today; the stub monitor does not
        # rewrite it, so its mtime stays before this run's start.
        report.write_text(json.dumps(stale_report))
        os.utime(report, (1_000_000, 1_000_000))

    renderer = (
        "exit 99" if renderer_fails else f'exec {sys.executable} "$@"'
    )
    # The pin resolver is the REAL script under this interpreter — it is the
    # seam the wrapper depends on, so stubbing it would leave the wrapper's only
    # config lookup untested. ``pin_resolver_fails`` reproduces "nothing is
    # pinned": exit 1, empty stdout, exactly what resolve_pin() does when
    # divergence.baseline_pin is unset.
    resolver = "exit 1" if pin_resolver_fails else f'exec {sys.executable} "$@"'
    argv_log = tmp_path / MONITOR_ARGV_LOG
    fake_python = stub("fake-python", f"""#!/bin/bash
case "$1" in
  *divergence_alert.py) {renderer} ;;
  *baseline_pin.py) {resolver} ;;
esac
{{ for a in "$@"; do printf '<%s>\n' "$a"; done; }} >> {argv_log}
[ -f {fixture} ] && cp {fixture} {report}
echo "monitor saw ALGO_REDIS_URL=${{ALGO_REDIS_URL:-unset}}"
echo "stub monitor ran" >&2
echo "ERROR: stub monitor could not load paper state from DB"
exit {exit_code}
""")

    env = dict(
        os.environ,
        HOME=str(home),
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        ALGO_DIR=str(REPO),
        ALGO_PYTHON=str(fake_python),
        ALGO_DIVERGENCE_REPORT=str(report),
        ALGO_SECURITY_BIN=str(bin_dir / "security"),
        ALGO_OSASCRIPT_BIN=str(bin_dir / "osascript"),
        ALGO_KEYCHAIN_SERVICE="algo-poc-absent-test-service",
        ALGO_DEADMAN_DIVERGENCE_URL=deadman_url,
        # Never left to the committed config here: that pin points into the
        # gitignored output/ of the real checkout, so the assertions would pass
        # or fail depending on which machine ran them.
        ALGO_BASELINE_PIN=str(
            pin or tmp_path / "output" / "backtest_multi_20260819_183451.json"
        ),
    )
    if pin_resolver_fails:
        env.pop("ALGO_BASELINE_PIN")
    res = subprocess.run(
        [str(RUN_DIVERGENCE)], capture_output=True, text=True,
        timeout=180, env=env, cwd=str(REPO),
    )
    sends = (
        [s for s in curl_log.read_text().split("---END---\n") if s.strip()]
        if curl_log.exists() else []
    )
    logs = list((home / "ibc" / "logs").glob("divergence_*.log"))
    return res, sends, (logs[0].read_text() if logs else "")


def test_exit_zero_sends_nothing(tmp_path):
    """AC2: no alert fatigue on a healthy day."""
    res, sends, log = _drive_wrapper(
        tmp_path, 0, _report(_portfolio("momentum", "OK")),
    )
    assert res.returncode == 0
    assert sends == [], sends
    assert "OK (exit 0)" in log


def test_breach_sends_exactly_one_message_naming_the_sleeve(tmp_path):
    """AC1 + AC3: one send, the documented content, exit code untouched."""
    res, sends, _ = _drive_wrapper(
        tmp_path, 1,
        _report(_portfolio("momentum", "BREACH"), _portfolio("value", "OK")),
    )
    assert res.returncode == 1
    assert len(sends) == 1, sends
    assert "BREACH" in sends[0]
    assert "momentum" in sends[0]
    assert "-7.30 pp" in sends[0]


def test_hard_error_sends_exactly_one_message_naming_the_failure(tmp_path):
    res, sends, _ = _drive_wrapper(tmp_path, 2, report_payload=None)
    assert res.returncode == 2
    assert len(sends) == 1, sends
    assert "HARD ERROR" in sends[0]
    assert "could not load paper state" in sends[0].lower()


def test_blind_sends_exactly_one_message_naming_the_unmet_requirement(tmp_path):
    res, sends, _ = _drive_wrapper(
        tmp_path, 3,
        _report(
            _portfolio("momentum", "NO_DATA", baseline_comparable=False),
            execution_model={
                "fill_model": "same_bar",
                "slippage_bps": 10.0,
                "commission_per_share": 0.005,
                "commission_minimum": 0.0,
                "point_in_time_universe": False,
                "coverage_state": "MISSING",
            },
        ),
    )
    assert res.returncode == 3
    assert len(sends) == 1, sends
    assert "BLIND" in sends[0]
    assert "same_bar" in sends[0]


def test_a_failing_send_does_not_change_the_exit_code(tmp_path):
    """AC4: the monitor's verdict outranks the notification's delivery."""
    res, sends, _ = _drive_wrapper(
        tmp_path, 1, _report(_portfolio("momentum", "BREACH")), curl_exit=7,
    )
    assert res.returncode == 1
    assert len(sends) == 1, sends


def test_a_broken_renderer_degrades_to_the_generic_message_not_to_silence(tmp_path):
    """The load-bearing guarantee. If divergence_alert.py cannot run at all —
    a syntax error, a missing venv, a bad import — the BREACH must still reach
    the operator, just with less detail. Silence is the one unacceptable
    outcome, and it is the outcome this whole story exists to remove."""
    res, sends, log = _drive_wrapper(
        tmp_path, 1, _report(_portfolio("momentum", "BREACH")),
        renderer_fails=True,
    )
    assert res.returncode == 1
    assert len(sends) == 1, sends
    assert "Divergence BREACH" in sends[0]
    # The generic fallback, not the rendered detail.
    assert "momentum" not in sends[0], sends[0]


def test_a_broken_renderer_still_alerts_on_the_blind_path(tmp_path):
    res, sends, _ = _drive_wrapper(tmp_path, 3, renderer_fails=True)
    assert res.returncode == 3
    assert len(sends) == 1, sends
    assert "BLIND" in sends[0]


def test_missing_telegram_credentials_do_not_change_the_exit_code(tmp_path):
    """A locked keychain must not turn a BREACH into a crash. It sends nothing
    over the network, logs why, and raises the secret-free local alert."""
    res, sends, log = _drive_wrapper(
        tmp_path, 1, _report(_portfolio("momentum", "BREACH")),
        serve_credentials=False,
    )
    assert res.returncode == 1
    assert sends == [], sends
    assert "cannot send alert" in log, log


def test_the_helper_is_sourced_before_the_first_telegram_call():
    """AC5, placement not just presence: a source line that lands after the
    first call would give `telegram: command not found` at exactly the moment
    the alert matters."""
    for wrapper in WRAPPERS:
        text = wrapper.read_text()
        source_at = text.index('. "$ALGO_DIR/deploy/launchd/lib/telegram.sh"')
        calls = [
            m.start() for m in re.finditer(r"(?m)^\s*telegram\s+[\"']", text)
        ]
        assert calls, f"{wrapper.name} sources the helper but never calls it"
        assert source_at < min(calls), (
            f"{wrapper.name} calls telegram before sourcing the helper"
        )


def test_no_wrapper_defines_telegram_in_any_shell_syntax():
    """Tighter than the plain-string check: catches `telegram()\\n{`,
    `function telegram {`, and indented definitions too."""
    pattern = re.compile(r"(?m)^\s*(function\s+telegram\b|telegram\s*\(\s*\))")
    offenders = [w.name for w in WRAPPERS if pattern.search(w.read_text())]
    assert offenders == [], f"wrappers still define their own telegram(): {offenders}"


def test_an_earlier_runs_report_is_not_narrated_as_this_runs(tmp_path):
    """`sys.exit(main())` means an uncaught exception also exits 1. If the
    operator hand-ran the monitor earlier today and it wrote a report with
    momentum in BREACH, a later crashing run must not confidently report
    momentum's numbers as though it had computed them."""
    res, sends, _ = _drive_wrapper(
        tmp_path, 1, stale_report=_report(_portfolio("momentum", "BREACH")),
    )
    assert res.returncode == 1
    assert len(sends) == 1, sends
    assert "BREACH" in sends[0]
    assert "momentum" not in sends[0], sends[0]


def test_the_monitor_is_given_a_redis_url_for_its_persistence_alert(tmp_path):
    """KAN-27: the monitor's only signal for a failed evidence write is an
    alert on stream:alerts, which needs a Redis it can reach. The wrapper is
    where the credential lives, so an unexported URL would leave that alert
    permanently undeliverable — the unwired-safety failure class."""
    _, _, log = _drive_wrapper(
        tmp_path, 0, _report(_portfolio("momentum", "OK")),
    )
    assert "ALGO_REDIS_URL=redis://:stub-redis@localhost:56379/0" in log, log


def test_a_missing_redis_credential_does_not_abort_the_monitor(tmp_path):
    """The Redis credential serves only the store-failure alert. Treating it as
    required would let an alert-path dependency take down drift detection —
    strictly worse than the outage it exists to report."""
    res, sends, log = _drive_wrapper(
        tmp_path, 1, _report(_portfolio("momentum", "BREACH")),
        serve_redis=False,
    )
    assert res.returncode == 1
    assert len(sends) == 1, sends
    assert "monitor saw ALGO_REDIS_URL=unset" in log, log
    assert "evidence-store write failure cannot be alerted" in log, log


# ---------------------------------------------------------------------------
# Exit 4 — the baseline stopped being refreshed (KAN-56)
# ---------------------------------------------------------------------------


def _stale_report(age_days=21, max_age_days=14, source="filename"):
    payload = _report(_portfolio("momentum", "OK"))
    payload["backtest_source"] = "output/backtest_multi_20260728_053111.json"
    payload["baseline_staleness"] = {
        "age_days": age_days,
        "max_age_days": max_age_days,
        "source": source,
        "stale": True,
    }
    return payload


def test_stale_names_the_age_the_threshold_and_the_artifact():
    """The operator's first question is "how bad, and since when" — a message
    that only says "stale" sends them to the log to find out, which is the
    friction that let three weeks pass."""
    text = render_alert(4, _stale_report())
    assert text
    assert "21" in text
    assert "14" in text
    assert "backtest_multi_20260728_053111.json" in text


def test_stale_alerts_even_when_the_report_is_unreadable():
    """Same rule as exits 1-3: a renderer that can go quiet reintroduces the
    blindness the exit code exists to remove."""
    text = render_alert(4, None)
    assert text
    assert "stale" in text.lower() or "old" in text.lower()


def test_stale_is_not_reported_as_an_unexpected_exit_code():
    """The regression the 2026-08-11 stale deployed copy produced for exit 3:
    a handled code narrated as 'UNEXPECTED'."""
    assert "UNEXPECTED" not in (render_alert(4, _stale_report()) or "")


# ---------------------------------------------------------------------------
# Exit 4 through the wrapper (KAN-56)
# ---------------------------------------------------------------------------


def test_the_wrapper_sends_one_message_naming_the_baseline_age(tmp_path):
    res, sends, log = _drive_wrapper(tmp_path, 4, report_payload=_stale_report())

    assert res.returncode == 4
    assert len(sends) == 1, sends
    assert "21" in sends[0] and "14" in sends[0]
    assert "STALE" in log or "stale" in log


def test_the_wrapper_does_not_call_a_stale_baseline_unexpected(tmp_path):
    _, sends, log = _drive_wrapper(tmp_path, 4, report_payload=_stale_report())
    assert "UNEXPECTED" not in log, log
    assert "UNEXPECTED" not in " ".join(sends)


# ---------------------------------------------------------------------------
# The divergence monitor's own dead-man (KAN-56)
# ---------------------------------------------------------------------------
#
# The 04:45 job is the one that actually went missing: 2026-08-13 and
# 2026-08-14 produced no divergence run at all, and 08-13 is a permanent hole
# in the gate evidence. Nothing on this host could have reported that, for the
# same reason as the refresh — a job that does not start cannot alert.
#
# What counts as a healthy beat here is NOT "exit 0", though. This job's
# purpose is to reach a verdict, and a BREACH (1), a BLIND baseline (3) or a
# stale one (4) are all verdicts: the monitor ran, judged, and said so through
# its own Telegram. Suppressing the ping for those would saturate the external
# check for the entire duration of a real drift episode — exactly when the
# ability to distinguish "did not run" from "ran and found something" matters
# most. Only a hard error (2) means nothing was judged.


def _pings(tmp_path) -> list[str]:
    log = tmp_path / "pings.log"
    return log.read_text().splitlines() if log.exists() else []


@pytest.mark.parametrize("exit_code", [0, 1, 3, 4])
def test_a_run_that_reaches_a_verdict_pings_the_dead_man(tmp_path, exit_code):
    _, _, log = _drive_wrapper(
        tmp_path, exit_code, report_payload=_report(_portfolio("momentum", "OK"))
    )
    pings = _pings(tmp_path)
    assert len(pings) == 1, (exit_code, pings)
    assert DIVERGENCE_DEADMAN_URL in pings[0]
    assert "dead-man switch: pinged" in log, log


def test_a_hard_error_does_not_ping(tmp_path):
    """Exit 2 is "the monitor could not judge anything" — DB unreachable, no
    baseline file, bad arguments. That is not a healthy beat."""
    _, _, log = _drive_wrapper(tmp_path, 2, report_payload=None)
    assert _pings(tmp_path) == []
    assert "dead-man switch: not pinged" in log, log


def test_the_divergence_ping_url_is_never_logged_verbatim(tmp_path):
    _, _, log = _drive_wrapper(tmp_path, 0)
    assert DIVERGENCE_DEADMAN_URL not in log
    assert "hc.example.test" in log


def test_an_unconfigured_divergence_switch_says_so(tmp_path):
    _, _, log = _drive_wrapper(tmp_path, 0, deadman_url="")
    assert _pings(tmp_path) == []
    assert "NOT CONFIGURED" in log, log


# ---------------------------------------------------------------------------
# The pinned baseline reaches the monitor (KAN-51)
#
# D16 requires the Rung-0 baseline to have "its own monitor pins". Before this,
# production never passed --backtest at all: every 04:45 run took the recency
# path, so the artifact the gate evidence was measured against was replaced by
# the Tuesday refresh and nothing recorded that it had changed.
# ---------------------------------------------------------------------------


def test_the_wrapper_names_the_pin_flag_at_all():
    """AC1, in its literal form. Cheap, and it is the one assertion that still
    holds if someone rewrites how the pin is resolved."""
    source = RUN_DIVERGENCE.read_text()
    assert "--backtest" in source
    assert "--pinned" in source


def test_the_monitor_is_invoked_against_the_resolved_pin(tmp_path):
    pinned = tmp_path / "output" / "backtest_multi_20260819_183451.json"
    res, _, log = _drive_wrapper(
        tmp_path, 0, _report(_portfolio("momentum", "OK")), pin=pinned,
    )

    assert res.returncode == 0, res.stderr
    argv = _monitor_argv(tmp_path)
    assert "--backtest" in argv, argv
    assert argv[argv.index("--backtest") + 1] == str(pinned), argv
    assert "--pinned" in argv, argv
    # The operator has to be able to tell from the log which baseline judged the
    # session, without re-deriving it from the config that was live at the time.
    assert str(pinned) in log


def test_the_existing_monitor_arguments_are_still_passed(tmp_path):
    """Regression guard: the pin is added to the invocation, not swapped in for
    the report and metrics paths the alert renderer and node_exporter read."""
    _drive_wrapper(tmp_path, 0, _report(_portfolio("momentum", "OK")))

    argv = _monitor_argv(tmp_path)
    assert "--output" in argv and "--prometheus-textfile" in argv


def test_an_unresolvable_pin_reaches_the_monitor_as_an_empty_backtest(tmp_path):
    """AC3's wrapper half. The wrapper must NOT decide to skip, and must not drop
    the flag — dropping it is the recency fallback wearing a different hat. It
    hands the empty pin over and lets the monitor exit 3 and alert, so there is
    exactly one authority on whether the run can happen."""
    res, sends, log = _drive_wrapper(tmp_path, 3, pin_resolver_fails=True)

    argv = _monitor_argv(tmp_path)
    assert "--backtest" in argv, argv
    assert argv[argv.index("--backtest") + 1] == "", argv
    assert "--pinned" in argv, argv
    # The monitor's own exit 3 still drives the alert and the dead-man beat.
    assert res.returncode == 3
    assert len(sends) == 1, sends
    assert "BLIND" in sends[0]
    assert "could not resolve" in log
