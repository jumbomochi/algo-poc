"""KAN-43: a divergence BREACH must notify someone.

Two layers: the pure renderer (``scripts/ops/divergence_alert.py``) and the
launchd wrapper driven end-to-end with ``curl`` stubbed, so the assertion is on
the actual send, not on grepping the script.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

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
