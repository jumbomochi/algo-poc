"""KAN-109: run_backtest refuses to backtest a sleeve on missing or stale data.

Before, it printed "WARNING: No fundamentals cache found" and carried on, so
from 2026-09-29 the weekly refresh produced baselines in which quality_value
and earnings_drift could not trade, and nothing downstream could tell. Now:

* a sleeve whose cache is absent is refused BEFORE the (hours-long) bar fetch;
* one whose cache is stale as of the newest bar is refused after it;
* ``--allow-degraded-data`` runs anyway and marks the artifact;
* a run that does not include those sleeves never needs either cache;
* a fresh cache produces an artifact with no mark at all.

Also here: the NaN-surprise entry the stale earnings cache produced 36 times
in the pinned baseline.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts import run_backtest
from shared.data_cache import write_cache_document

FIRST_SESSION = date(2023, 1, 3)
SESSIONS = 320
LAST_SESSION = FIRST_SESSION + timedelta(days=SESSIONS - 1)


def _bars() -> list[dict]:
    price, out = 100.0, []
    for i in range(SESSIONS):
        price *= 1.001
        out.append({"date": (FIRST_SESSION + timedelta(days=i)).isoformat(),
                    "open": round(price, 4), "high": round(price * 1.01, 4),
                    "low": round(price * 0.99, 4), "close": round(price, 4),
                    "volume": 1_000_000})
    return out


@pytest.fixture
def cache_dir(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "cache"
    path.mkdir()
    monkeypatch.setenv("ALGO_DATA_CACHE_DIR", str(path))
    return path


def _run(tmp_path: Path, monkeypatch, *extra: str) -> Path:
    snapshots = tmp_path / "membership.json"
    snapshots.write_text(json.dumps({FIRST_SESSION.isoformat(): ["AAA", "BBB"]}))
    bars = tmp_path / "bars.json"
    bars.write_text(json.dumps({"bars": {"AAA": _bars(), "BBB": _bars()}}))
    out_dir = tmp_path / "output"
    out_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(run_backtest.sys, "argv", [
        "run_backtest.py", "--bars-from-json", str(bars),
        "--universe-snapshots", str(snapshots), "--output-dir", str(out_dir),
        "--years", "2", *extra,
    ])
    run_backtest.main()
    return out_dir


def _artifact(out_dir: Path) -> dict:
    [path] = out_dir.glob("backtest_multi_*.json")
    return json.loads(path.read_text())


def _write_fresh(cache_dir: Path, fetched_at: datetime) -> None:
    write_cache_document(
        cache_dir / "fundamentals.json",
        {"AAA": [{"report_date": (LAST_SESSION - timedelta(days=60)).isoformat(),
                  "roe": 0.2, "debt_equity": 0.5, "profit_margin": 0.2}]},
        fetched_at=fetched_at,
    )
    write_cache_document(
        cache_dir / "earnings.json",
        {"AAA": [{"earnings_date": FIRST_SESSION.isoformat(), "surprise_pct": 6.0}]},
        fetched_at=fetched_at,
    )


def test_absent_caches_are_refused_before_any_bar_is_fetched(tmp_path, monkeypatch, cache_dir, capsys):
    def no_fetch(**kwargs):
        raise AssertionError("fetched bars for a run that must be refused")

    monkeypatch.setattr(run_backtest, "fetch_bars_from_ib", no_fetch)
    monkeypatch.setattr(run_backtest.sys, "argv", ["run_backtest.py", "--years", "1"])

    with pytest.raises(SystemExit) as exit_info:
        run_backtest.main()

    assert exit_info.value.code == run_backtest.EXIT_DATA_DEGRADED == 3
    out = capsys.readouterr().out
    assert "refusing to backtest" in out
    assert "quality_value: fundamentals cache MISSING" in out
    assert "earnings_drift: earnings cache MISSING" in out


def test_stale_caches_are_refused_after_the_fetch(tmp_path, monkeypatch, cache_dir, capsys):
    """Fetched long before the last bar: the end of the backtest is uncovered."""
    _write_fresh(cache_dir, datetime(2023, 6, 1, tzinfo=timezone.utc))

    with pytest.raises(SystemExit) as exit_info:
        _run(tmp_path, monkeypatch)

    assert exit_info.value.code == 3
    assert not list((tmp_path / "output").glob("backtest_multi_*.json"))
    assert "STALE" in capsys.readouterr().out


def test_allow_degraded_data_runs_and_marks_the_artifact(tmp_path, monkeypatch, cache_dir, capsys):
    out_dir = _run(tmp_path, monkeypatch, "--allow-degraded-data")

    marked = _artifact(out_dir)["config"]["data_degraded"]
    assert set(marked) == {"earnings_drift", "quality_value"}
    assert "MISSING" in marked["quality_value"]
    # The token run_backtest_refresh.sh greps, in a shape it can parse.
    assert "DATA_DEGRADED_ARTIFACT: earnings_drift,quality_value " in capsys.readouterr().out


def test_a_run_without_the_data_sleeves_needs_no_cache(tmp_path, monkeypatch, cache_dir):
    out_dir = _run(tmp_path, monkeypatch, "--sleeves", "momentum")
    assert "data_degraded" not in _artifact(out_dir)["config"]


def test_fresh_caches_leave_the_artifact_unmarked(tmp_path, monkeypatch, cache_dir):
    """Fresh as of the newest bar, not as of today: a historical window is
    fully served by a cache fetched after it ends."""
    _write_fresh(cache_dir, datetime.combine(LAST_SESSION, datetime.min.time(),
                                             tzinfo=timezone.utc))
    out_dir = _run(tmp_path, monkeypatch)
    assert "data_degraded" not in _artifact(out_dir)["config"]


def test_a_nan_surprise_is_not_an_entry():
    """A scheduled announcement with no reported EPS: ``nan < 5.0`` is False,
    so it used to pass the threshold. 36 such entries sit in the pinned
    baseline (2026-04-13..06-22)."""
    bars = [{"date": FIRST_SESSION + timedelta(days=i), "close": 100.0,
             "open": 100.0, "high": 100.0, "low": 100.0, "volume": 1}
            for i in range(6)]
    on = bars[-1]["date"]

    def fn_for(surprise):
        return run_backtest.make_earnings_drift_signals_fn(
            earnings_lookup=lambda t, d: {"surprise_pct": surprise} if d == on else None,
        )

    assert fn_for(float("nan"))("AAA", bars) is None
    assert fn_for(None)("AAA", bars) is None
    assert fn_for(4.0)("AAA", bars) is None
    signal = fn_for(8.0)("AAA", bars)
    assert signal is not None and signal["action"] == "buy"
