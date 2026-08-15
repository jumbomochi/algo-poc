# KAN-43: A divergence BREACH must notify someone — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `deploy/launchd/run_divergence.sh` send a *specific*, content-rich Telegram alert on exit 1 (BREACH), 2 (hard error) and 3 (BLIND), nothing on exit 0, and collapse the six copy-pasted `telegram()` helpers into one sourced file.

**Architecture:** The wrapper keeps its exit-code contract untouched. Message *content* is rendered by a new pure-Python module `scripts/ops/divergence_alert.py` that reads the JSON report the monitor already writes (and the log tail for hard errors) — the monitor itself is not modified (explicitly out of scope). The wrapper shells out to that renderer and falls back to a generic message if rendering fails, so a broken renderer can never mute the alert. The `telegram()` bash helper moves to `deploy/launchd/lib/telegram.sh`, sourced **by path from the repo** exactly like `secrets.sh` and `deadman.sh` (so it can never drift via a stale `~/ibc` copy, and `deploy.sh`'s `"$SRC"/*.sh` glob does not reach into `lib/`).

**Tech Stack:** bash (launchd wrappers), Python 3.12 stdlib + `backtest.divergence.ExecutionModel`, pytest driving the wrapper end-to-end via `subprocess` with PATH stubs.

**Spec:** JIRA KAN-43 — https://huiliang.atlassian.net/browse/KAN-43

## Global Constraints

- **Do not modify `scripts/divergence_monitor.py` or any exit code.** Out of scope per the spec.
- **Do not route through the notifications service.** The wrapper runs outside Docker and must alert when the stack is down.
- **Do not alert on WARNING.** Only BREACH counts toward the ladder (D11); WARNING alerts would train the operator to ignore the channel.
- `|| true` semantics on the send: a failed notification must never change the wrapper's exit code.
- All Python modules start with `from __future__ import annotations`.
- Six wrappers exist, not five (the spec said five, it missed `run_paper.sh`): `run_paper.sh`, `run_divergence.sh`, `run_pipeline_report.sh`, `run_db_backup.sh`, `run_backtest_refresh.sh`, `gateway_watchdog.sh`. All six must source the shared helper.
- The issue's "current state" was verified 2026-08-12 and is now **stale**: the sends are no longer commented out, the `scripts.ops.notify` reference and the "notifications are disabled" comments are already gone (AC6 is already satisfied on `develop` — verify, do not re-do).

---

### Task 1: Alert-text renderer

**Files:**
- Create: `scripts/ops/divergence_alert.py`
- Test: `tests/deploy/test_divergence_alerting.py`

**Interfaces:**
- Produces:
  - `render_alert(exit_code: int, report: dict | None, log_tail: str = "") -> str | None`
    — returns `None` for exit 0, a non-empty message otherwise.
  - `main(argv: list[str] | None = None) -> int` — CLI: `--exit-code N --report PATH --log PATH`; prints the message to stdout (nothing for exit 0), returns 0.

**Behaviour contract**

| exit | message must contain |
|---|---|
| 0 | *(nothing — `render_alert` returns `None`, CLI prints nothing)* |
| 1 | `BREACH`, every portfolio whose `status == "BREACH"`, and that portfolio's divergence numbers |
| 2 | `HARD ERROR` and the last `ERROR:` line from the log tail |
| 3 | `BLIND` and each unmet like-for-like requirement from `ExecutionModel.unmet_requirements()` |

- [ ] **Step 1: Write the failing tests**

Create `tests/deploy/test_divergence_alerting.py`:

```python
"""KAN-43: a divergence BREACH must notify someone.

Two layers: the pure renderer (`scripts/ops/divergence_alert.py`) and the
launchd wrapper driven end-to-end with `curl` stubbed, so the assertion is on
the actual send, not on grepping the script.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.ops.divergence_alert import render_alert

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
            _portfolio("_aggregate", "BREACH", absolute_divergence_pp=-0.051,
                       relative_divergence=-0.9),
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
            _portfolio("momentum", "NO_DATA", baseline_comparable=False,
                       notes=["baseline is not comparable: stale universe"]),
        ),
    )
    assert "BLIND" in msg
    assert "stale universe" in msg


def test_blind_alerts_even_with_an_unreadable_report():
    # A missing report must never mute the alert — silence is the failure mode
    # this story exists to remove.
    msg = render_alert(3, None)
    assert "BLIND" in msg


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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/deploy/test_divergence_alerting.py -v`
Expected: collection error — `ModuleNotFoundError: No module named 'scripts.ops.divergence_alert'`.

- [ ] **Step 3: Write the renderer**

Create `scripts/ops/divergence_alert.py`. Key points:
- Reuse `backtest.divergence.ExecutionModel.unmet_requirements()` — do **not** re-implement the wording.
- `ExecutionModel` is a frozen dataclass; build it with `ExecutionModel(**{k: v for k, v in payload.items() if k in fields})` so an older report missing a field still constructs.
- Every branch must produce a message. Never return `None` for 1/2/3, even with no report.
- `_fmt_pp(v)` renders `absolute_divergence_pp` (a decimal, 0.01 = 1 pp) as `f"{v * 100:+.2f} pp"`.

```python
#!/usr/bin/env python3
"""Render the Telegram alert text for a divergence-monitor run.

KAN-43. Pure rendering — this module never sends anything; the launchd wrapper
(deploy/launchd/run_divergence.sh) owns delivery via the shared telegram()
helper. Split this way so the message content is unit-testable without a
network stub, and so a rendering failure degrades to the wrapper's generic
fallback text rather than to silence.

Input is the JSON report scripts/divergence_monitor.py already writes plus the
tail of the run's log; the monitor itself is deliberately untouched.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path
from typing import Any

from backtest.divergence import ExecutionModel

DOC_HINT = "Regenerate per docs/operations/backtest-baseline.md"


def _fmt_pp(v: float | None) -> str:
    return f"{v * 100:+.2f} pp" if v is not None else "—"


def _fmt_pct(v: float | None) -> str:
    return f"{v * 100:+.1f}%" if v is not None else "—"


def _execution_model(report: dict[str, Any] | None) -> ExecutionModel | None:
    if not report:
        return None
    payload = report.get("baseline_execution_model")
    if not isinstance(payload, dict) or "fill_model" not in payload:
        return None
    known = {f.name for f in dataclasses.fields(ExecutionModel)}
    return ExecutionModel(**{k: v for k, v in payload.items() if k in known})


def _breach_lines(report: dict[str, Any] | None) -> list[str]:
    lines: list[str] = []
    for r in (report or {}).get("reports", []):
        if r.get("status") != "BREACH":
            continue
        lines.append(
            f"• {r.get('portfolio', '?')}: live {_fmt_pct(r.get('live_return'))} "
            f"vs backtest {_fmt_pct(r.get('backtest_return'))} "
            f"(Δ {_fmt_pp(r.get('absolute_divergence_pp'))}, "
            f"{_fmt_pct(r.get('relative_divergence'))} relative)"
        )
    return lines


def _blind_reasons(report: dict[str, Any] | None) -> list[str]:
    model = _execution_model(report)
    if model is not None and not model.is_like_for_like:
        return [f"• {reason}" for reason in model.unmet_requirements()]
    reasons: list[str] = []
    for r in (report or {}).get("reports", []):
        if r.get("baseline_comparable", True):
            continue
        for note in r.get("notes", []):
            reasons.append(f"• {r.get('portfolio', '?')}: {note}")
    return reasons


def _last_error_line(log_tail: str) -> str:
    errors = [
        line.strip() for line in log_tail.splitlines()
        if line.strip().startswith("ERROR")
    ]
    return errors[-1] if errors else ""


def render_alert(
    exit_code: int,
    report: dict[str, Any] | None,
    log_tail: str = "",
) -> str | None:
    """Return the alert text for this run, or None when nothing should be sent.

    Exit 0 is silence by design — alerting on a healthy day is what trains an
    operator to ignore the channel. Every other code MUST yield a message, even
    when the report is missing or malformed: a renderer that can go quiet
    reintroduces exactly the blindness this story removes.
    """
    if exit_code == 0:
        return None

    if exit_code == 1:
        lines = _breach_lines(report)
        body = "\n".join(lines) if lines else (
            "• (no per-sleeve detail in the report — see the log)"
        )
        return (
            "🚨 Divergence BREACH — live paper equity has diverged from the "
            f"backtest baseline.\n{body}"
        )

    if exit_code == 2:
        detail = _last_error_line(log_tail) or "no ERROR line in the log tail"
        return f"🚨 Divergence monitor HARD ERROR (exit 2).\n{detail}"

    if exit_code == 3:
        reasons = _blind_reasons(report)
        body = "\n".join(reasons) if reasons else (
            "• reason not recorded in the report — see the log"
        )
        return (
            "⚠️ Divergence monitor is BLIND (exit 3): the baseline backtest is "
            "not comparable to live, so every report is forced to NO_DATA. No "
            f"drift detection is running.\n{body}\n{DOC_HINT}"
        )

    return f"🚨 Divergence monitor returned UNEXPECTED exit code {exit_code}."


def _load_report(path: str | None) -> dict[str, Any] | None:
    if not path:
        return None
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _load_log_tail(path: str | None, max_bytes: int = 65536) -> str:
    if not path:
        return ""
    try:
        p = Path(path)
        with p.open("rb") as f:
            size = p.stat().st_size
            if size > max_bytes:
                f.seek(size - max_bytes)
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exit-code", type=int, required=True)
    parser.add_argument("--report", default=None)
    parser.add_argument("--log", default=None)
    args = parser.parse_args(argv)

    text = render_alert(
        args.exit_code,
        _load_report(args.report),
        _load_log_tail(args.log),
    )
    if text:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/deploy/test_divergence_alerting.py -v`
Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add scripts/ops/divergence_alert.py tests/deploy/test_divergence_alerting.py
git commit -m "KAN-43: render divergence alert text from the monitor's own report"
```

---

### Task 2: Shared `telegram()` helper

**Files:**
- Create: `deploy/launchd/lib/telegram.sh`
- Modify: `deploy/launchd/run_divergence.sh`, `run_paper.sh`, `run_pipeline_report.sh`, `run_db_backup.sh`, `run_backtest_refresh.sh`, `gateway_watchdog.sh` (delete each local `telegram()` definition, source the helper instead)
- Test: `tests/deploy/test_divergence_alerting.py` (append)

**Interfaces:**
- Consumes: `algo_secret_into` / `$_ALGO_SECRET_VALUE` / `$ALGO_SECRETS_ERROR` / `algo_alert_local` from `deploy/launchd/secrets.sh` (already sourced by every wrapper).
- Produces: `telegram "<text>"`, using `$ALGO_JOB_LABEL` (set by each wrapper before sourcing) in the "cannot alert" log line and local alert. Always returns 0.

Note the six copies differ today in two ways, and the shared version must take the
**stronger** behaviour of each: timestamps via `ts()` where the wrapper defines one
(`date` otherwise), and `algo_alert_local` on a missing credential (which
`run_paper.sh` and `run_divergence.sh` currently omit — that omission is the
"locked keychain means nothing can alert" hole KAN-16 closed everywhere else).

- [ ] **Step 1: Write the failing tests**

Append to `tests/deploy/test_divergence_alerting.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/deploy/test_divergence_alerting.py -v -k "telegram or wrapper or helper"`
Expected: FAIL — `deploy/launchd/lib/telegram.sh is missing`.

- [ ] **Step 3: Create the shared helper**

```bash
#!/bin/bash
# Shared best-effort Telegram sender for the algo-poc launchd wrappers.
#
# WHY THIS EXISTS
# ---------------
# Six wrappers each carried a hand-copied `telegram()`. They had already
# drifted: four raised `algo_alert_local` when no credential resolved, two did
# not — so on a locked keychain those two failed silently, which is the exact
# hole KAN-16 was opened to close. A fix applied to one copy (adding the
# `-m 10` timeout, say) silently misses the other five.
#
# SOURCED BY PATH FROM THE REPO — `. "$ALGO_DIR/deploy/launchd/lib/telegram.sh"`
# — never from ~/ibc, for the same reason as secrets.sh: exactly one copy of
# the logic, and it cannot drift. It lives under lib/ so deploy.sh's
# `"$SRC"/*.sh` glob does not reach it and cannot plant a decoy copy.
#
# Requires: secrets.sh sourced first (algo_secret_into, algo_alert_local).
# Expects:  $LOG_FILE  — where the "cannot alert" line goes
#           $ALGO_JOB_LABEL — this job's name, e.g. "divergence monitor"
#
# Usage:
#   ALGO_JOB_LABEL="divergence monitor"
#   . "$ALGO_DIR/deploy/launchd/lib/telegram.sh"
#   telegram "🚨 something happened"

ALGO_JOB_LABEL="${ALGO_JOB_LABEL:-algo-poc job}"

# Wrappers that want second-resolution stamps define ts(); the rest get date.
_algo_telegram_ts() {
    if declare -F ts >/dev/null 2>&1; then ts; else date; fi
}

# Best-effort Telegram alert. A missing credential is LOGGED and raised
# locally, never silently swallowed — the old `[ -f "$ENV_FILE" ]` guard was
# FALSE for the 1Password FIFO that replaced .env on 2026-08-12, so every
# alert path returned *success* and stayed quiet for two days.
#
# Always returns 0: the job's own verdict matters more than the delivery of
# its notification, and no caller's exit code may depend on Telegram.
telegram() {
    local token chat
    if ! algo_secret_into TELEGRAM_BOT_TOKEN; then
        echo "$(_algo_telegram_ts): WARNING - cannot send alert: $ALGO_SECRETS_ERROR" >> "$LOG_FILE"
        algo_alert_local "$ALGO_JOB_LABEL cannot alert: $ALGO_SECRETS_ERROR"
        return 0
    fi
    token="$_ALGO_SECRET_VALUE"
    if ! algo_secret_into TELEGRAM_CHAT_ID; then
        echo "$(_algo_telegram_ts): WARNING - cannot send alert: $ALGO_SECRETS_ERROR" >> "$LOG_FILE"
        algo_alert_local "$ALGO_JOB_LABEL cannot alert: $ALGO_SECRETS_ERROR"
        return 0
    fi
    chat="$_ALGO_SECRET_VALUE"
    curl -s -m 10 "https://api.telegram.org/bot${token}/sendMessage" \
        -d chat_id="$chat" --data-urlencode text="$1" >/dev/null 2>&1 || true
    return 0
}
```

- [ ] **Step 4: Rewire all six wrappers**

In each wrapper, delete the local `telegram() { ... }` block (and the comment
paragraph directly above it that described it) and, immediately after the
existing `. "$ALGO_DIR/deploy/launchd/secrets.sh"` line, add:

```bash
# Shared best-effort Telegram sender, sourced by path for the same reason.
ALGO_JOB_LABEL="<job name>"
# shellcheck source=deploy/launchd/lib/telegram.sh
. "$ALGO_DIR/deploy/launchd/lib/telegram.sh"
```

Labels: `run_paper.sh` → `paper run`; `run_divergence.sh` → `divergence monitor`;
`run_pipeline_report.sh` → `pipeline report`; `run_db_backup.sh` → `db backup`;
`run_backtest_refresh.sh` → `backtest refresh`; `gateway_watchdog.sh` → `watchdog`.

Two ordering hazards to respect:
1. `telegram()` writes to `$LOG_FILE`, which several wrappers define *after*
   sourcing `secrets.sh`. Sourcing only defines the function, so this is safe —
   but the `ALGO_JOB_LABEL=` assignment and the source line must both sit before
   the first `telegram` *call*.
2. `run_db_backup.sh`, `run_pipeline_report.sh`, `run_backtest_refresh.sh` and
   `gateway_watchdog.sh` define `ts()`. `_algo_telegram_ts` resolves `ts` at call
   time, so it does not matter whether `ts()` is defined before or after the
   source line.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/deploy/ -v`
Expected: all pass, including the pre-existing `test_launchd_secrets_keychain.py`
and `test_launchd_deploy_hardening.py` suites (they drive `run_paper.sh`
end-to-end, so a broken source line surfaces there).

- [ ] **Step 6: Commit**

```bash
git add deploy/launchd tests/deploy/test_divergence_alerting.py
git commit -m "KAN-43: one shared telegram() helper instead of six copies"
```

---

### Task 3: Wire the wrapper to the renderer and prove the sends

**Files:**
- Modify: `deploy/launchd/run_divergence.sh`
- Test: `tests/deploy/test_divergence_alerting.py` (append)

**Interfaces:**
- Consumes: `scripts/ops/divergence_alert.py` CLI (Task 1), `telegram()` (Task 2).
- Produces: no new symbols. Two new env knobs on the wrapper, overridable **only**
  so the test can drive it (launchd starts jobs with an empty environment, so
  production always takes the default), matching the `ALGO_DIR` precedent in
  `run_paper.sh:6-10`:
  - `ALGO_DIR` (was hardcoded)
  - `ALGO_DIVERGENCE_REPORT` — where the monitor writes its JSON report

- [ ] **Step 1: Write the failing tests**

Append to `tests/deploy/test_divergence_alerting.py`:

```python
RUN_DIVERGENCE = DEPLOY_DIR / "run_divergence.sh"


def _drive_wrapper(tmp_path, exit_code, report_payload=None, curl_exit=0):
    """Run run_divergence.sh end-to-end against a stubbed monitor and curl.

    Everything the wrapper reaches out to is stubbed on PATH: `nc` (the DB port
    wait), `curl` (the send, recorded to a file), `osascript` (the local alert,
    which would otherwise pop a real desktop notification on the developer's
    machine) and `security` (the keychain, forced to miss so the fixture .env
    fallback supplies the credentials).

    The fake python dispatches: the alert renderer is the real thing run under
    this interpreter, everything else is the monitor stub. In production both
    are the same interpreter, so this split is a test artifact only.
    """
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl_log = tmp_path / "curl.log"

    def stub(name, body):
        p = bin_dir / name
        p.write_text(body)
        p.chmod(0o755)
        return p

    stub("nc", "#!/bin/bash\nexit 0\n")
    stub("osascript", "#!/bin/bash\nexit 0\n")
    stub("security", "#!/bin/bash\necho 'could not be found' >&2\nexit 44\n")
    # Record every argument on its own line so the test can assert on the
    # message body exactly as it was passed, not on a re-quoted rendering.
    stub("curl", f"""#!/bin/bash
{{ for a in "$@"; do printf '%s\\n' "$a"; done; printf -- '---END---\\n'; }} >> {curl_log}
exit {curl_exit}
""")

    report = tmp_path / "divergence.json"
    if report_payload is not None:
        report.write_text(json.dumps(report_payload))

    fake_python = stub("fake-python", f"""#!/bin/bash
case "$1" in
  *divergence_alert.py) exec {sys.executable} "$@" ;;
esac
echo "stub monitor ran" >&2
echo "ERROR: stub monitor could not load paper state from DB"
exit {exit_code}
""")

    env_file = tmp_path / "fixture.env"
    env_file.write_text(
        "POSTGRES_PASSWORD=stub-pg\n"
        "TELEGRAM_BOT_TOKEN=stub-token\n"
        "TELEGRAM_CHAT_ID=stub-chat\n"
    )

    env = dict(
        os.environ,
        HOME=str(home),
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        ALGO_DIR=str(REPO),
        ALGO_PYTHON=str(fake_python),
        ALGO_DIVERGENCE_REPORT=str(report),
        ALGO_SECRETS_ENV_FILE=str(env_file),
        ALGO_SECURITY_BIN=str(bin_dir / "security"),
        ALGO_OSASCRIPT_BIN=str(bin_dir / "osascript"),
        ALGO_KEYCHAIN_SERVICE="algo-poc-absent-test-service",
    )
    res = subprocess.run(
        [str(RUN_DIVERGENCE)], capture_output=True, text=True,
        timeout=180, env=env, cwd=str(REPO),
    )
    sends = [
        s for s in curl_log.read_text().split("---END---\n") if s.strip()
    ] if curl_log.exists() else []
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/deploy/test_divergence_alerting.py -v -k "wrapper or sends or exit_code"`
Expected: FAIL — the wrapper ignores `ALGO_DIR`/`ALGO_PYTHON` and still points at
`/Users/huiliang/GitHub/algo-poc/.venv/bin/python`.

- [ ] **Step 3: Modify the wrapper**

Replace `deploy/launchd/run_divergence.sh:19-20`:

```bash
# Overridable only so tests/deploy/test_divergence_alerting.py can drive this
# wrapper end-to-end against stubs — launchd starts jobs with an empty
# environment, so production always takes the defaults. Never export ALGO_DIR
# in a login shell: a manual run would then use whatever tree that points at.
ALGO_DIR="${ALGO_DIR:-/Users/huiliang/GitHub/algo-poc}"
VENV="${ALGO_PYTHON:-$ALGO_DIR/.venv/bin/python}"
```

Add next to the other path definitions:

```bash
# Passed to the monitor explicitly (it would default to the same path) so the
# alert renderer knows exactly which report to read back.
REPORT_FILE="${ALGO_DIVERGENCE_REPORT:-$ALGO_DIR/output/divergence_$(date +%Y%m%d).json}"
```

Add, after the shared-helper source line from Task 2:

```bash
# Render the alert body from the monitor's own JSON report. A rendering failure
# must degrade to the generic text, never to silence — that is the whole point
# of this job. $1 = exit code, $2 = fallback text.
divergence_alert_text() {
    local text
    text=$("$VENV" "$ALGO_DIR/scripts/ops/divergence_alert.py" \
               --exit-code "$1" --report "$REPORT_FILE" --log "$LOG_FILE" \
               2>>"$LOG_FILE") || text=""
    [ -n "$text" ] || text="$2"
    printf '%s' "$text"
}
```

Pass the report path to the monitor:

```bash
"$VENV" scripts/divergence_monitor.py \
    --output "$REPORT_FILE" \
    --prometheus-textfile "$PROM_FILE" \
    >> "$LOG_FILE" 2>&1
EXIT_CODE=$?
```

Replace the three `telegram "..."` calls in the `case` with the rendered text
(exit 0's branch stays send-free):

```bash
    1)
        echo "$(date): ALERT - divergence BREACH (exit 1)" >> "$LOG_FILE"
        telegram "$(divergence_alert_text 1 "🚨 Divergence BREACH ($(date +%F)) — live paper equity has diverged from the backtest baseline. See $LOG_FILE")"
        ;;
    2)
        echo "$(date): PAGE - divergence monitor hard error (exit 2)" >> "$LOG_FILE"
        algo_alert_local "divergence monitor hard error (exit 2) — see $LOG_FILE"
        telegram "$(divergence_alert_text 2 "🚨 Divergence monitor HARD ERROR (exit 2) on $(date +%F). See $LOG_FILE")"
        ;;
    3)
        echo "$(date): ALERT - divergence monitor BLIND: baseline backtest is not" \
             "comparable to live (exit 3). No drift detection is running until the" \
             "baseline is regenerated - see docs/operations/backtest-baseline.md" \
             >> "$LOG_FILE"
        # A blind monitor is an outage, not a pass.
        telegram "$(divergence_alert_text 3 "⚠️ Divergence monitor is BLIND (exit 3): the baseline backtest is not comparable to live. No drift detection is running. Regenerate per docs/operations/backtest-baseline.md")"
        ;;
```

The `*)` unexpected-code branch keeps its existing literal text: there is no
report to render from and the code is by definition unmodelled.

**Hazard:** every appended `$LOG_FILE` line must still land in `$LOG_DIR`, which
`mkdir -p` creates at line 32 — `REPORT_FILE`'s directory is the monitor's
concern (`write_json_report` mkdirs it), not the wrapper's.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/deploy/test_divergence_alerting.py -v`
Expected: all pass.

- [ ] **Step 5: Run the full deploy suite, then the whole suite**

Run: `pytest tests/deploy/ -v` then `pytest -q`
Expected: green. `test_launchd_deploy_hardening.py::test_divergence_wrapper_handles_the_blind_baseline_exit_code`
and the `secrets.sh`-sourcing assertions in `test_launchd_secrets_keychain.py`
both still cover this wrapper — if either goes red the rewrite broke a contract.

- [ ] **Step 6: Commit**

```bash
git add deploy/launchd/run_divergence.sh tests/deploy/test_divergence_alerting.py
git commit -m "KAN-43: divergence alerts carry the breaching sleeves and the blind reason"
```

---

### Task 4: Operator handover note (AC7)

**Files:**
- Modify: `deploy/launchd/README.md`

- [ ] **Step 1: Document the deploy step and the one manual check**

AC7 is `[OPERATOR]`-tagged: deploying via `deploy/launchd/deploy.sh` and confirming a
real BREACH-path message in the Telegram chat is a human step this plan must hand
over, never perform. Add a short subsection to `deploy/launchd/README.md` recording:
- `lib/telegram.sh` is sourced by path from the repo and is deliberately **not**
  deployed to `~/ibc` (same rule as `secrets.sh` and `deadman.sh`);
- after `deploy/launchd/deploy.sh`, verify delivery once with
  `ALGO_JOB_LABEL="divergence monitor" ... telegram "test"` — or, more simply,
  the existing `scripts/ops/send_test_alert.py`;
- the divergence job needs no plist change, so no `launchctl` reload is required.

- [ ] **Step 2: Commit**

```bash
git add deploy/launchd/README.md
git commit -m "KAN-43: document the shared telegram helper's deploy contract"
```

---

## Acceptance-criteria trace

| AC | Where |
|---|---|
| 1 — stubbed exit 1/2/3 each send once with documented content | Task 3 tests `test_breach_sends_...`, `test_hard_error_sends_...`, `test_blind_sends_...` |
| 2 — exit 0 sends nothing | Task 3 `test_exit_zero_sends_nothing` (+ Task 1 `test_exit_zero_renders_nothing`) |
| 3 — exit code preserved | asserted in every Task 3 wrapper test |
| 4 — a failing send does not change the exit code | Task 3 `test_a_failing_send_does_not_change_the_exit_code` |
| 5 — all wrappers source the shared helper, none defines its own | Task 2 `test_no_wrapper_defines_its_own_telegram_copy`, `test_every_wrapper_sources_the_shared_helper` |
| 6 — stale comments and `scripts.ops.notify` gone | already true on `develop` (verified by grep: no hits) — no diff needed |
| 7 — `[OPERATOR]` deploy + one real message | Task 4: documented and handed over, not executed |
