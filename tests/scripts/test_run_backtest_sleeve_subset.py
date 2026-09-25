"""KAN-60: a sleeve subset runs at full capital, to an exact path.

Rung 0 runs one sleeve (D8, rung0-economics §9). The baseline for it has to be
a momentum-only run at the rung's whole capital, written somewhere the weekly
refresh's globs never see.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from scripts import run_backtest

SESSIONS = 320
FIRST_SESSION = date(2023, 1, 3)


def _bars(n: int, drift: float = 0.001) -> list[dict]:
    price, out = 100.0, []
    for i in range(n):
        price *= 1 + drift
        d = FIRST_SESSION + timedelta(days=i)
        out.append({"date": d.isoformat(), "open": round(price, 4),
                    "high": round(price * 1.01, 4), "low": round(price * 0.99, 4),
                    "close": round(price, 4), "volume": 1_000_000})
    return out


def _run(tmp_path: Path, monkeypatch, *extra: str) -> Path:
    snapshots = tmp_path / "membership.json"
    snapshots.write_text(json.dumps({FIRST_SESSION.isoformat(): ["AAA", "BBB"]}))
    bars = tmp_path / "bars.json"
    bars.write_text(json.dumps({"bars": {"AAA": _bars(SESSIONS), "BBB": _bars(SESSIONS)}}))
    out_dir = tmp_path / "output"
    out_dir.mkdir()
    monkeypatch.setattr(run_backtest.sys, "argv", [
        "run_backtest.py", "--bars-from-json", str(bars),
        "--universe-snapshots", str(snapshots), "--output-dir", str(out_dir),
        "--years", "2", *extra,
    ])
    run_backtest.main()
    return out_dir


def test_the_allocation_table_is_the_six_live_fractions() -> None:
    """Default runs stay byte-identical only if the table IS the old literals."""
    assert run_backtest.SLEEVE_ALLOCATIONS == {
        "momentum": 0.2308, "sector_rotation": 0.1538,
        "thematic_momentum": 0.1410, "quality_value": 0.1538,
        "earnings_drift": 0.1923, "tail_risk_hedge": 0.1283,
    }
    assert run_backtest.sleeve_capital_fractions(None) == run_backtest.SLEEVE_ALLOCATIONS


def test_a_single_sleeve_gets_the_whole_capital() -> None:
    assert run_backtest.sleeve_capital_fractions(["momentum"]) == {"momentum": 1.0}


def test_a_subset_is_renormalised_over_itself() -> None:
    fractions = run_backtest.sleeve_capital_fractions(["momentum", "sector_rotation"])
    assert sum(fractions.values()) == pytest.approx(1.0)
    assert fractions["momentum"] == pytest.approx(0.2308 / (0.2308 + 0.1538))


def test_an_unknown_sleeve_is_refused(tmp_path, monkeypatch) -> None:
    with pytest.raises(SystemExit):
        _run(tmp_path, monkeypatch, "--sleeves", "mean_reversion")


def test_momentum_only_writes_the_multi_envelope_at_the_exact_path(
    tmp_path, monkeypatch
) -> None:
    target = tmp_path / "output" / "baselines" / "rung0_momentum_20260925.json"
    out_dir = _run(tmp_path, monkeypatch, "--capital", "3700", "--whole-shares",
                   "--sleeves", "momentum", "--output", str(target))

    assert target.exists(), "--output must create its parent and write there"
    assert not list(out_dir.glob("backtest_*.json")), (
        "an --output run must not also drop a backtest_* file the refresh globs see"
    )
    artifact = json.loads(target.read_text())
    assert set(artifact) == {"config", "portfolios", "aggregate", "bars"}
    assert set(artifact["portfolios"]) == {"momentum"}
    assert artifact["portfolios"]["momentum"]["config"]["capital"] == pytest.approx(3700.0)
    assert artifact["config"]["portfolios"] == {"momentum": pytest.approx(3700.0)}
    assert artifact["config"]["whole_shares"] is True
    assert artifact["config"]["commission_minimum"] == pytest.approx(1.0)


def test_a_default_run_is_unchanged(tmp_path, monkeypatch) -> None:
    out_dir = _run(tmp_path, monkeypatch, "--capital", "100000")
    [artifact_path] = out_dir.glob("backtest_multi_*.json")
    artifact = json.loads(artifact_path.read_text())
    assert set(artifact["portfolios"]) == set(run_backtest.SLEEVE_ALLOCATIONS)
    assert artifact["config"]["portfolios"]["momentum"] == pytest.approx(23080.0)
