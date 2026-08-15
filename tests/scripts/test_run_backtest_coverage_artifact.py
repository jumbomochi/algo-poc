"""End-to-end: a point-in-time backtest records its universe coverage.

Story KAN-22 / direction doc D14. The unit tests pin the arithmetic; this one
pins the wiring — that a real ``scripts/run_backtest.py`` run over a membership
snapshot writes ``config.coverage`` into the results artifact, and that the
divergence monitor's reader agrees with what it finds there.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from backtest.divergence import execution_model_from_backtest_config
from backtest.membership import COVERAGE_BLOCKED, COVERAGE_OK
from scripts import run_backtest

# Long enough to clear the 200-day MA the regime detector needs and the 126-day
# momentum lookback, so the sleeves actually run rather than idling.
SESSIONS = 320
FIRST_SESSION = date(2023, 1, 3)


def _sessions() -> list[date]:
    return [FIRST_SESSION + timedelta(days=i) for i in range(SESSIONS)]


def _bars(sessions: list[date], *, drift: float = 0.001) -> list[dict]:
    price = 100.0
    out = []
    for d in sessions:
        price *= 1 + drift
        out.append({
            "date": d.isoformat(),
            "open": round(price, 4),
            "high": round(price * 1.01, 4),
            "low": round(price * 0.99, 4),
            "close": round(price, 4),
            "volume": 1_000_000,
        })
    return out


def _run(tmp_path: Path, monkeypatch, *, priced: dict[str, list[date]]) -> dict:
    """Run the backtest over a two-name index and return the saved artifact."""
    sessions = _sessions()
    members = sorted(priced)
    snapshots = tmp_path / "membership.json"
    snapshots.write_text(json.dumps({FIRST_SESSION.isoformat(): members}))

    bars_path = tmp_path / "bars.json"
    bars_path.write_text(json.dumps({
        "bars": {t: _bars(days) for t, days in priced.items()}
    }))

    out_dir = tmp_path / "output"
    out_dir.mkdir()
    monkeypatch.setattr(run_backtest.sys, "argv", [
        "run_backtest.py",
        "--bars-from-json", str(bars_path),
        "--universe-snapshots", str(snapshots),
        "--output-dir", str(out_dir),
        "--years", "2",
        "--capital", "100000",
    ])
    run_backtest.main()

    artifacts = sorted(out_dir.glob("backtest_*.json"))
    assert artifacts, f"no results artifact written to {out_dir}"
    return json.loads(artifacts[-1].read_text())


def test_a_fully_priced_run_records_ok_coverage(tmp_path: Path, monkeypatch):
    sessions = _sessions()
    artifact = _run(
        tmp_path, monkeypatch, priced={"AAA": sessions, "BBB": sessions}
    )

    coverage = artifact["config"]["coverage"]
    assert set(coverage) == {
        "total_membership_days",
        "excluded_membership_days",
        "excluded_pct",
        "excluded_tickers",
        "floor_pct",
        "state",
    }
    assert coverage["total_membership_days"] == 2 * SESSIONS
    assert coverage["excluded_membership_days"] == 0
    assert coverage["excluded_pct"] == pytest.approx(0.0)
    assert coverage["excluded_tickers"] == {}
    assert coverage["state"] == COVERAGE_OK
    assert execution_model_from_backtest_config(
        artifact["config"]
    ).is_like_for_like is True


def test_a_member_that_cannot_be_priced_blocks_the_baseline(
    tmp_path: Path, monkeypatch
):
    """BBB stops printing a third of the way in — a delisting we cannot price."""
    sessions = _sessions()
    artifact = _run(
        tmp_path,
        monkeypatch,
        priced={"AAA": sessions, "BBB": sessions[: SESSIONS // 3]},
    )

    coverage = artifact["config"]["coverage"]
    assert coverage["excluded_tickers"]["BBB"] == SESSIONS - SESSIONS // 3
    assert coverage["state"] == COVERAGE_BLOCKED
    assert execution_model_from_backtest_config(
        artifact["config"]
    ).is_like_for_like is False
