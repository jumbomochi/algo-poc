"""The divergence baseline must be reported stale by something other than the refresh.

``run_backtest_refresh.sh:246`` already says "divergence baseline is getting
stale" — but it says it *from inside the failing run*, so it cannot fire for the
case that matters most: a run that never starts. On 2026-08-11 the host booted
after the 05:00 slot, launchd did not re-fire the missed job, and nothing said a
word. The dead-man switch is the mechanism designed for that and could not help
either, because ``ALGO_DEADMAN_REFRESH_URL`` only pings on a *successful*
refresh — it cannot arm itself until the job it watches is already healthy, and
an unpinged healthchecks.io check never alerts (KAN-65).

Net effect, measured on 2026-09-08: the newest artifact was
``backtest_multi_20260825_102450.json``, fourteen days old, and the daily
pipeline report did not mention the baseline at all — grepping it for
stale|baseline|refresh matched one line, ``local.algo-backtest-refresh: loaded``,
which is KAN-64's launchd wiring section and says nothing about the artifact.

These tests drive the shipped ``lib/baseline_age.sh`` in bash rather than a
paraphrase of it, with the clock injected via ``ALGO_NOW_EPOCH`` so "nine days
old" is asserted by driving it rather than by waiting.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
LIB = REPO / "deploy/launchd/lib/baseline_age.sh"
REPORT = REPO / "deploy/launchd/run_pipeline_report.sh"
OPS_DOC = REPO / "docs/operations/dead-man-switches.md"

NOW = 1_757_000_000  # a fixed epoch; every age below is derived from it
DAY = 86_400


def _run(output_dir: Path, *, now: int = NOW, stale_days: int | None = None) -> dict[str, str]:
    """Source the shipped lib, run the check, and return the variables it set."""
    stale = f'ALGO_BASELINE_STALE_DAYS={stale_days}\n' if stale_days is not None else ""
    body = (
        f'ALGO_BASELINE_DIR="{output_dir}"\n'
        f"ALGO_NOW_EPOCH={now}\n"
        f"{stale}"
        f'. "{LIB}"\n'
        "algo_baseline_age_check\n"
        'printf "FILE=%s\\n" "$ALGO_BASELINE_FILE"\n'
        'printf "AGE=%s\\n" "$ALGO_BASELINE_AGE_DAYS"\n'
        'printf "STATUS=%s\\n" "$ALGO_BASELINE_STATUS"\n'
        'printf "DETAIL=%s\\n" "$ALGO_BASELINE_DETAIL"\n'
        'printf "ALERT=%s\\n" "$(algo_baseline_alert_body)"\n'
    )
    res = subprocess.run(
        ["bash", "-c", body], capture_output=True, text=True, timeout=60,
    )
    assert res.returncode == 0, res.stderr
    out: dict[str, str] = {}
    for line in res.stdout.splitlines():
        key, _, value = line.partition("=")
        out[key] = value
    return out


def _artifact(output_dir: Path, name: str, *, age_days: float) -> Path:
    path = output_dir / name
    path.write_text("{}")
    stamp = NOW - int(age_days * DAY)
    os.utime(path, (stamp, stamp))
    return path


@pytest.fixture()
def output_dir(tmp_path) -> Path:
    d = tmp_path / "output"
    d.mkdir()
    return d


def test_a_fresh_baseline_is_reported_and_does_not_alert(output_dir):
    """A healthy week must stay quiet, or the alert trains people to ignore it."""
    _artifact(output_dir, "backtest_multi_20260901_050000.json", age_days=2)
    got = _run(output_dir)
    assert got["STATUS"] == "fresh"
    assert got["AGE"] == "2"
    assert got["FILE"] == "backtest_multi_20260901_050000.json"
    assert got["ALERT"] == "", got


def test_a_stale_baseline_alerts_and_names_its_age(output_dir):
    """The 2026-09-08 state: fourteen days old and nothing said so."""
    _artifact(output_dir, "backtest_multi_20260825_102450.json", age_days=14)
    got = _run(output_dir)
    assert got["STATUS"] == "stale"
    assert got["AGE"] == "14"
    assert "14d" in got["ALERT"], got["ALERT"]
    assert "backtest_multi_20260825_102450.json" in got["ALERT"]


def test_the_threshold_boundary_is_inclusive(output_dir):
    """Eight days is one missed Tuesday plus a day of slack — at eight it is
    stale, at seven it is not. Pinned so a later edit cannot quietly widen it."""
    _artifact(output_dir, "backtest_multi_a.json", age_days=7)
    assert _run(output_dir)["STATUS"] == "fresh"
    _artifact(output_dir, "backtest_multi_a.json", age_days=8)
    assert _run(output_dir)["STATUS"] == "stale"


def test_no_baseline_at_all_is_distinct_from_a_stale_one_and_still_alerts(output_dir):
    """Absence of evidence must not render as freshness."""
    got = _run(output_dir)
    assert got["STATUS"] == "absent"
    assert got["FILE"] == ""
    assert got["ALERT"] != ""
    assert "MISSING" in got["ALERT"], got["ALERT"]
    assert "STALE" not in got["ALERT"], got["ALERT"]


def test_the_newest_artifact_wins_regardless_of_filename_order(output_dir):
    """Chosen by mtime, not by the date in the name: the two disagree whenever a
    refresh is re-run by hand, and the question is when one was last produced."""
    _artifact(output_dir, "backtest_multi_20260901_050000.json", age_days=20)
    _artifact(output_dir, "backtest_multi_20260728_053111.json", age_days=1)
    got = _run(output_dir)
    assert got["FILE"] == "backtest_multi_20260728_053111.json", got
    assert got["STATUS"] == "fresh"


def test_a_non_baseline_json_is_not_mistaken_for_one(output_dir):
    """output/ also holds divergence and shadow artifacts, written daily. Taking
    one of those as the baseline would make a stale baseline look permanently
    fresh — the exact false-negative this check exists to prevent."""
    _artifact(output_dir, "divergence_20260908.json", age_days=0)
    _artifact(output_dir, "shadow_20260908.json", age_days=0)
    _artifact(output_dir, "backtest_multi_20260825_102450.json", age_days=14)
    got = _run(output_dir)
    assert got["STATUS"] == "stale", got
    assert got["FILE"] == "backtest_multi_20260825_102450.json"


def test_the_mtime_helper_never_trusts_a_gnu_filesystem_dump(tmp_path):
    """`stat -f %m` on GNU coreutils prints a four-line filesystem dump to
    stdout while exiting 1. A helper that validates the exit code rather than
    the shape hands back that dump, and every later comparison silently reads as
    garbage — which is how a monitor goes quiet. Same guard as the watchdog's."""
    target = tmp_path / "f"
    target.write_text("x")
    res = subprocess.run(
        ["bash", "-c", f'. "{LIB}"\nalgo_mtime "{target}"'],
        capture_output=True, text=True, timeout=60,
    )
    assert res.stdout.strip().isdigit(), res.stdout
    assert len(res.stdout.strip().splitlines()) == 1, res.stdout


def test_the_daily_report_runs_the_check_and_escalates_through_both_paths():
    """A log line reproduces the failure. KAN-64 settled this: a failure mode
    whose nature is silence must not be reported into a file nobody opens."""
    text = REPORT.read_text()
    assert "lib/baseline_age.sh" in text, "run_pipeline_report.sh does not source the lib"
    assert "algo_baseline_age_check" in text, "the report never runs the check"
    body = re.search(
        r"BASELINE_MSG=\$\(algo_baseline_alert_body\).*?\bfi\b",
        text,
        re.S,
    )
    assert body, "the report does not route the baseline alert body anywhere"
    assert "algo_alert_local" in body.group(0), body.group(0)
    assert "telegram" in body.group(0), body.group(0)


def test_the_threshold_is_written_down_where_the_operator_reads_it():
    """The cadence table is the thing an operator configures from; a threshold
    that lives only in shell is one nobody can sanity-check against it."""
    # Demands all three in ONE paragraph, not merely somewhere in the file: the
    # doc already said "baseline" and "8 days" in unrelated table rows, so a
    # whole-document assertion passed against a runbook that described no
    # staleness check at all. Paragraph rather than line, because prose wraps.
    paragraphs = re.split(r"\n\s*\n", OPS_DOC.read_text().lower())
    hits = [
        p for p in paragraphs
        if "baseline" in p and "stale" in p and re.search(r"\b8\b", p)
    ]
    assert hits, "the runbook does not state the baseline staleness threshold"
