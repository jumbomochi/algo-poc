"""The baseline check must judge the PINNED baseline, not the newest artifact.

KAN-71 shipped a daily staleness check that picked the newest
``output/backtest_multi_*.json`` by mtime. On 2026-09-10 the weekly refresh
succeeded for the first time in three weeks and the check went quiet:

    status: fresh
    detail: backtest_multi_20260910_235624.json is 0d old
    alert : []

Nothing fired. Meanwhile the baseline the divergence monitor actually grades
against — ``config/default.yaml``'s ``divergence.baseline_pin`` — was still
``backtest_multi_20260819_183451.json``, 23 days old, and the artifact that
silenced the alert carried ``config.coverage.state = BLOCKED`` at 17.39%
excluded, so it could never be pinned.

``scripts/ops/baseline_pin.py`` already states the rule that was broken, in its
own docstring: *the baseline of record is a configuration fact, not whatever
``output/backtest_multi_*.json`` happens to sort last*. The seam existed, worked,
and was not used.

The two facts coincided until a refresh succeeded while producing something
unpinnable — which is precisely the case most worth reporting.

**What alerts, and why it is split that way.** The pin's age is a *state*: it is
reported every day and alerts once it passes the monitor's comparison window,
past which live and backtest stop overlapping meaningfully. An unusable refresh
is an *event*: it alerts on the day it is produced and is reported as state
thereafter. Alerting daily on an unusable baseline would fire every morning
until a data vendor exists for delisted history (KAN-59/KAN-60), which is how an
alert gets ignored — the failure this whole tranche keeps rediscovering.

A newer, usable artifact sitting unpinned is NOT an alert. Re-pinning is a
deliberate act (KAN-51) and that is the normal, correct state after any refresh.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
LIB = REPO / "deploy/launchd/lib/baseline_age.sh"
REPORT = REPO / "deploy/launchd/run_pipeline_report.sh"
OPS_DOC = REPO / "docs/operations/dead-man-switches.md"

NOW = 1_757_000_000
DAY = 86_400


def _artifact(path: Path, *, age_days: float, state: str = "OK",
              excluded_pct: float = 2.0) -> Path:
    """A baseline artifact carrying the coverage block the real ones do.

    Written config-first and pretty-printed, because that is the shape the
    reader has to cope with: the real files are ~240MB, so the coverage state
    must be recoverable from the first few KB rather than by parsing the whole
    document in a daily report.
    """
    path.write_text(json.dumps({
        "config": {
            "tickers": ["AAPL", "MSFT"],
            "coverage": {
                "total_membership_days": 1266885,
                "excluded_membership_days": 220355,
                "excluded_pct": excluded_pct,
                "floor_pct": 5.0,
                "state": state,
            },
        },
        "portfolios": {},
    }, indent=2))
    stamp = NOW - int(age_days * DAY)
    os.utime(path, (stamp, stamp))
    return path


def _run(output_dir: Path, *, pin: Path | str | None, now: int = NOW,
         pin_max_days: int | None = None, config: str | None = None) -> dict[str, str]:
    """Source the shipped lib and run the check against a controlled tree."""
    extra = f"ALGO_BASELINE_PIN_MAX_DAYS={pin_max_days}\n" if pin_max_days else ""
    body = (
        f'ALGO_DIR="{REPO}"\n'
        f'ALGO_BASELINE_DIR="{output_dir}"\n'
        f"ALGO_NOW_EPOCH={now}\n"
        f'ALGO_PYTHON="{sys.executable}"\n'
        + (f'ALGO_BASELINE_CONFIG="{config}"\n' if config else "")
        + f"{extra}"
        + f'. "{LIB}"\n'
        "algo_baseline_age_check\n"
        'printf "STATUS=%s\\n" "$ALGO_BASELINE_STATUS"\n'
        'printf "DETAIL=%s\\n" "$ALGO_BASELINE_DETAIL"\n'
        'printf "ALERT=%s\\n" "$(algo_baseline_alert_body)"\n'
    )
    env = dict(os.environ)
    if pin is None:
        env.pop("ALGO_BASELINE_PIN", None)
        env["ALGO_BASELINE_PIN"] = ""      # unpinned
    else:
        env["ALGO_BASELINE_PIN"] = str(pin)
    res = subprocess.run(["bash", "-c", body], capture_output=True, text=True,
                         timeout=90, env=env)
    assert res.returncode == 0, res.stderr
    out: dict[str, str] = {}
    for line in res.stdout.splitlines():
        k, _, v = line.partition("=")
        out[k] = v
    return out


@pytest.fixture()
def output_dir(tmp_path) -> Path:
    d = tmp_path / "output"
    d.mkdir()
    return d


def test_the_pin_is_judged_not_the_newest_artifact(output_dir):
    """The 2026-09-10 defect. A fresh artifact must not silence a stale pin."""
    pin = _artifact(output_dir / "backtest_multi_20260819_183451.json",
                    age_days=23, state="BLOCKED", excluded_pct=11.28)
    _artifact(output_dir / "backtest_multi_20260910_235624.json",
              age_days=0, state="BLOCKED", excluded_pct=17.39)
    got = _run(output_dir, pin=pin, pin_max_days=20)
    assert got["STATUS"] != "ok", got
    assert "backtest_multi_20260819" in got["DETAIL"], got["DETAIL"]
    assert got["ALERT"] != "", got


def test_the_body_reports_both_the_pin_and_the_newest(output_dir):
    """Either number alone misleads: "pin 23d" hides that a refresh ran, and
    "newest 0d" hides that it cannot be used."""
    pin = _artifact(output_dir / "backtest_multi_20260819_183451.json",
                    age_days=23, state="BLOCKED", excluded_pct=11.28)
    _artifact(output_dir / "backtest_multi_20260910_235624.json",
              age_days=0, state="BLOCKED", excluded_pct=17.39)
    got = _run(output_dir, pin=pin)
    assert "backtest_multi_20260819" in got["DETAIL"]
    assert "backtest_multi_20260910" in got["DETAIL"]
    assert "23d" in got["DETAIL"], got["DETAIL"]


def test_a_refresh_that_produced_an_unusable_baseline_alerts(output_dir):
    """2026-09-10: the refresh exited 0, pinged its dead-man, and logged
    "coverage is BLOCKED" into a file nobody opens. Every external signal
    reported a healthy weekly refresh."""
    pin = _artifact(output_dir / "backtest_multi_20260819_183451.json",
                    age_days=23, state="BLOCKED", excluded_pct=11.28)
    _artifact(output_dir / "backtest_multi_20260910_235624.json",
              age_days=0, state="BLOCKED", excluded_pct=17.39)
    got = _run(output_dir, pin=pin, pin_max_days=90)   # pin not yet stale
    assert got["STATUS"] == "unusable", got
    assert "BLOCKED" in got["ALERT"], got["ALERT"]
    assert "17.39" in got["ALERT"] or "17" in got["ALERT"], got["ALERT"]


def test_an_old_unusable_artifact_is_reported_but_does_not_alert_daily(output_dir):
    """The event is "a refresh produced something unpinnable". Repeating it every
    morning until a data vendor exists is how an alert gets ignored."""
    pin = _artifact(output_dir / "backtest_multi_20260819_183451.json",
                    age_days=10, state="BLOCKED", excluded_pct=11.28)
    _artifact(output_dir / "backtest_multi_20260901_000000.json",
              age_days=9, state="BLOCKED", excluded_pct=17.39)
    got = _run(output_dir, pin=pin, pin_max_days=90)
    assert got["ALERT"] == "", got
    assert "BLOCKED" in got["DETAIL"], got["DETAIL"]


def test_a_usable_refresh_newer_than_the_pin_is_quiet(output_dir):
    """Re-pinning is deliberate (KAN-51); a newer usable artifact sitting
    unpinned is the normal state after every refresh."""
    pin = _artifact(output_dir / "backtest_multi_20260819_183451.json",
                    age_days=10, state="OK")
    _artifact(output_dir / "backtest_multi_20260910_235624.json",
              age_days=0, state="OK")
    got = _run(output_dir, pin=pin, pin_max_days=90)
    assert got["STATUS"] == "ok", got
    assert got["ALERT"] == "", got


def test_a_pin_past_the_window_alerts(output_dir):
    """Past the monitor's comparison window live and backtest stop overlapping
    meaningfully — which the monitor already hints at with "Only 22 overlapping
    days available (requested 30)"."""
    pin = _artifact(output_dir / "backtest_multi_old.json", age_days=45, state="OK")
    got = _run(output_dir, pin=pin, pin_max_days=30)
    assert got["STATUS"] == "stale", got
    assert "45d" in got["ALERT"], got["ALERT"]


def test_a_pin_inside_the_window_does_not_alert(output_dir):
    pin = _artifact(output_dir / "backtest_multi_old.json", age_days=29, state="OK")
    got = _run(output_dir, pin=pin, pin_max_days=30)
    assert got["ALERT"] == "", got


def test_a_pinned_file_that_does_not_exist_alerts_distinctly(output_dir):
    """The monitor will exit 3 (BLIND) on its next run; the report should say so
    first, and say something different from "the pin is old"."""
    got = _run(output_dir, pin=output_dir / "backtest_multi_gone.json")
    assert got["STATUS"] == "missing", got
    assert got["ALERT"] != ""
    assert "stale" not in got["ALERT"].lower(), got["ALERT"]


def test_a_pin_that_cannot_be_resolved_alerts(output_dir, tmp_path):
    """Absence of evidence must not render as freshness — the rule this lib was
    written for and then broke. Covers both an unreadable config and one with no
    divergence.baseline_pin: resolve_pin returns None for each, deliberately,
    so that deciding what that MEANS belongs to one place."""
    _artifact(output_dir / "backtest_multi_20260910_235624.json", age_days=0)
    got = _run(output_dir, pin="", config=str(tmp_path / "nonexistent.yaml"))
    assert got["STATUS"] == "unresolved", got
    assert got["ALERT"] != "", got


def test_the_coverage_state_is_read_without_parsing_the_whole_artifact(output_dir):
    """The real artifacts are ~240MB. A daily report that json.loads one would
    cost seconds and gigabytes; the coverage block sits in the first few KB."""
    # Comments are stripped first: an earlier version of this test matched the
    # lib's own explanation of why it does NOT parse, which is the opposite of
    # what it is checking.
    code = "\n".join(
        l for l in LIB.read_text().splitlines() if not l.lstrip().startswith("#")
    )
    assert "head -c" in code, (
        "the reader does not bound how much of the artifact it reads"
    )
    assert "json.load" not in code and "json.loads" not in code


def test_the_daily_report_still_routes_the_alert_through_both_paths():
    text = REPORT.read_text()
    assert "algo_baseline_age_check" in text
    import re
    body = re.search(r"BASELINE_MSG=\$\(algo_baseline_alert_body\).*?\bfi\b", text, re.S)
    assert body, "the report does not route the baseline alert body anywhere"
    assert "algo_alert_local" in body.group(0)
    assert "telegram" in body.group(0)


def test_the_runbook_describes_the_pin_not_the_newest_artifact():
    """The doc described the newest-artifact behaviour as intended, which is
    what made the defect look like a feature. It must now state the rule that
    was broken: the pin is judged, the newest artifact is not.

    Asserted as "one paragraph names both", rather than on the word "stale" —
    an earlier version of this test demanded that word and would have been
    satisfied by the very sentence it was meant to replace.
    """
    paragraphs = OPS_DOC.read_text().lower().split("\n\n")
    hits = [p for p in paragraphs if "baseline_pin" in p and "newest" in p]
    assert hits, (
        "the runbook does not state that the PIN is judged rather than the "
        "newest artifact"
    )
