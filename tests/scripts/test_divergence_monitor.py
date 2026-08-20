"""Integration tests for ``scripts/divergence_monitor.py``.

The pure math is covered in ``tests/backtest/test_divergence.py``. This file
covers the I/O layer: backtest-JSON parsing, DB loaders against an in-memory
SQLite, and the end-to-end orchestration.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from scripts.divergence_monitor import (
    BASELINE_PIN_MISSING,
    BASELINE_SHAPE_MISMATCH,
    BASELINE_STALE_WARNING,
    DEFAULT_MAX_BASELINE_AGE_DAYS,
    EXIT_BASELINE_NOT_COMPARABLE,
    EXIT_BASELINE_STALE,
    EXIT_BREACH,
    EXIT_ERROR,
    EXIT_OK,
    baseline_age,
    exit_code_for,
    find_latest_backtest_json,
    load_backtest_execution_model,
    load_backtest_equity_series,
    load_live_aggregate_series,
    load_live_equity_series,
    write_json_report,
)
from scripts.paper_state import PaperTradingState
from shared.models.base import Base
from shared.models.equity_snapshot import EquitySnapshot
from shared.models.portfolio import Trade
from shared.models.portfolio_config import PortfolioConfig
from shared.universe import DRILL_PORTFOLIO


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    session = factory()
    yield session
    session.close()


def _seed_state(session: Session, start: date | None = None) -> PaperTradingState:
    """Seed a paper-trading state with two portfolios and a week of snapshots.

    ``start`` moves the seven-day window; the default keeps the fixed historical
    dates every other test asserts on. Tests that care about *when* the window
    ends relative to today pass it explicitly.
    """
    state = PaperTradingState.create_new(
        portfolio_capitals={"momentum": 23080.0, "sector_rotation": 15380.0},
        session=session,
    )

    # Seven days of equity snapshots, +0.2%/day for momentum, +0.1%/day for sector_rotation.
    base = start or date(2026, 5, 19)
    now = datetime.now(timezone.utc)
    mom_v = 23080.0
    sec_v = 15380.0
    for i in range(7):
        d = date.fromordinal(base.toordinal() + i)
        session.add(EquitySnapshot(
            portfolio="momentum", date=d,
            equity=mom_v, cash=mom_v, market_value=0.0, created_at=now,
        ))
        session.add(EquitySnapshot(
            portfolio="sector_rotation", date=d,
            equity=sec_v, cash=sec_v, market_value=0.0, created_at=now,
        ))
        mom_v *= 1.002
        sec_v *= 1.001
    session.flush()
    return state


def _write_backtest_json(
    tmp_path: Path, label: str = "20260525_000000", start: date | None = None
) -> Path:
    """Write a minimal valid backtest results JSON for the loader to parse."""
    # Seven dates matching the seeded equity snapshots.
    base = start or date(2026, 5, 19)
    dates = [date.fromordinal(base.toordinal() + i).isoformat() for i in range(7)]
    # portfolio_values is len(dates) + 1; first element is pre-day-0 initial capital.
    mom_pv = [23080.0] + [23080.0 * (1.002 ** (i + 1)) for i in range(7)]
    sec_pv = [15380.0] + [15380.0 * (1.001 ** (i + 1)) for i in range(7)]
    agg_pv = [a + b for a, b in zip(mom_pv, sec_pv)]
    data = {
        "config": {
            "initial_capital": 38460.0,
            "portfolios": {"momentum": 23080.0, "sector_rotation": 15380.0},
        },
        "portfolios": {
            "momentum": {
                "config": {"capital": 23080.0},
                "trades": [],
                "portfolio_values": mom_pv,
                "dates": dates,
                "metrics": {"total_return": 0.0141},
            },
            "sector_rotation": {
                "config": {"capital": 15380.0},
                "trades": [],
                "portfolio_values": sec_pv,
                "dates": dates,
                "metrics": {"total_return": 0.0070},
            },
        },
        "aggregate": {
            "portfolio_values": agg_pv,
            "trades": [],
            "dates": dates,
            "metrics": {"total_return": 0.0113},
        },
        "bars": {},
    }
    path = tmp_path / f"backtest_multi_{label}.json"
    path.write_text(json.dumps(data))
    return path


# ---------------------------------------------------------------------------
# Loader tests
# ---------------------------------------------------------------------------


def test_find_latest_backtest_json_picks_alphabetically_last(tmp_path: Path):
    """File names embed YYYYMMDD_HHMMSS so alphabetical == chronological."""
    (tmp_path / "backtest_multi_20260101_120000.json").write_text("{}")
    (tmp_path / "backtest_multi_20260601_120000.json").write_text("{}")
    (tmp_path / "backtest_multi_20260301_120000.json").write_text("{}")
    latest = find_latest_backtest_json(output_dir=str(tmp_path))
    assert latest is not None
    assert latest.endswith("20260601_120000.json")


def test_find_latest_backtest_json_returns_none_when_empty(tmp_path: Path):
    assert find_latest_backtest_json(output_dir=str(tmp_path)) is None


def test_load_backtest_equity_series_drops_initial_capital(tmp_path: Path):
    """``portfolio_values[0]`` is pre-day-0 capital — must NOT be aligned with dates[0]."""
    path = _write_backtest_json(tmp_path)
    per_p, agg = load_backtest_equity_series(str(path))

    assert set(per_p.keys()) == {"momentum", "sector_rotation"}

    mom = per_p["momentum"]
    # 7 dates, 8 portfolio_values -> 7 dated values after dropping the first.
    assert len(mom) == 7

    # First dated value should be END-of-day value for date 0, not the initial capital.
    first_date = date(2026, 5, 19)
    assert mom[first_date] == pytest.approx(23080.0 * 1.002)  # +0.2% after first day

    # Aggregate equals sum of sleeves on every date.
    for d in mom:
        assert agg[d] == pytest.approx(mom[d] + per_p["sector_rotation"][d])


def test_load_live_equity_series_returns_dict_of_dates_to_equity(db_session: Session):
    state = _seed_state(db_session)
    live = load_live_equity_series(state, "momentum")
    assert len(live) == 7
    assert min(live.keys()) == date(2026, 5, 19)
    assert max(live.keys()) == date(2026, 5, 25)
    # First snapshot was the unmutated initial capital.
    assert live[date(2026, 5, 19)] == pytest.approx(23080.0)


def test_load_live_aggregate_series_sums_per_portfolio(db_session: Session):
    _seed_state(db_session)
    state = PaperTradingState.load(db_session)
    per_p = {
        name: load_live_equity_series(state, name)
        for name in state.get_portfolio_names()
    }
    agg = load_live_aggregate_series(per_p)
    assert len(agg) == 7
    for d, v in agg.items():
        assert v == pytest.approx(per_p["momentum"][d] + per_p["sector_rotation"][d])


def test_load_live_aggregate_series_intersects_dates_only():
    """If one portfolio is missing a date, the aggregate should also skip it."""
    p = {
        "a": {date(2026, 5, 1): 100.0, date(2026, 5, 2): 110.0},
        "b": {date(2026, 5, 1): 200.0},  # missing 5/2
    }
    agg = load_live_aggregate_series(p)
    assert list(agg.keys()) == [date(2026, 5, 1)]
    assert agg[date(2026, 5, 1)] == pytest.approx(300.0)


# ---------------------------------------------------------------------------
# End-to-end orchestration
# ---------------------------------------------------------------------------


def test_end_to_end_produces_ok_reports_when_live_matches_backtest(
    db_session: Session, tmp_path: Path
):
    """Live = backtest (same growth) ⇒ all reports OK and exit 0 from any_breach."""
    from backtest.divergence import any_breach, build_report

    _seed_state(db_session)
    state = PaperTradingState.load(db_session)
    bt_path = _write_backtest_json(tmp_path)
    bt_per_p, bt_agg = load_backtest_equity_series(str(bt_path))

    live_per_p = {
        name: load_live_equity_series(state, name)
        for name in state.get_portfolio_names()
    }

    reports = []
    for name in state.get_portfolio_names():
        if name not in bt_per_p:
            continue
        report = build_report(
            portfolio=name,
            live=live_per_p[name],
            backtest=bt_per_p[name],
            trades=state.get_trades(name),
            window_days=30,
        )
        reports.append(report)

    assert all(r.status in ("OK", "WARNING") for r in reports)
    assert not any_breach(reports)
    # Live and backtest were synthesized identically — divergence should be ~0.
    for r in reports:
        assert r.absolute_divergence_pp == pytest.approx(0.0, abs=1e-9)


def test_write_json_report_round_trip(tmp_path: Path):
    """The JSON output should be valid and contain ISO-date strings."""
    from backtest.divergence import PortfolioDivergenceReport
    report = PortfolioDivergenceReport(
        portfolio="momentum",
        window_start=date(2026, 5, 1),
        window_end=date(2026, 5, 30),
        days_compared=22,
        live_return=0.05,
        backtest_return=0.045,
        absolute_divergence_pp=0.005,
        relative_divergence=0.111,
        daily_correlation=0.93,
        live_trades_in_window=12,
        realized_slippage_total=8.50,
        realized_slippage_bps=9.2,
        realized_commission_total=1.25,
        assumed_commission_total=1.20,
        status="OK",
        notes=[],
    )
    output_path = tmp_path / "div.json"
    write_json_report(
        [report], str(output_path),
        backtest_path="dummy.json",
        window_days=30,
        threshold=0.20,
    )

    loaded = json.loads(output_path.read_text())
    assert loaded["window_days"] == 30
    assert loaded["threshold"] == 0.20
    assert len(loaded["reports"]) == 1
    r = loaded["reports"][0]
    assert r["portfolio"] == "momentum"
    assert r["window_start"] == "2026-05-01"
    assert r["window_end"] == "2026-05-30"
    assert r["status"] == "OK"


# ---------------------------------------------------------------------------
# Baseline execution model
# ---------------------------------------------------------------------------


def test_load_execution_model_reads_the_declared_fill_model(tmp_path: Path):
    path = _write_backtest_json(tmp_path)
    data = json.loads(path.read_text())
    data["config"].update({
        "fill_model": "next_open",
        "slippage_bps": 10,
        "commission_per_share": 0.005,
        "commission_minimum": 1.0,
        "point_in_time_universe": True,
        "coverage": {"state": "OK", "excluded_pct": 1.2},
    })
    path.write_text(json.dumps(data))

    model = load_backtest_execution_model(str(path))

    assert model.fill_model == "next_open"
    assert model.is_like_for_like is True


def test_load_execution_model_refuses_a_baseline_that_lost_its_delistings(
    tmp_path: Path,
):
    """A PIT baseline whose members could not be priced is still not comparable."""
    path = _write_backtest_json(tmp_path)
    data = json.loads(path.read_text())
    data["config"].update({
        "fill_model": "next_open",
        "slippage_bps": 10,
        "commission_per_share": 0.005,
        "commission_minimum": 1.0,
        "point_in_time_universe": True,
        "coverage": {"state": "BLOCKED", "excluded_pct": 9.7},
    })
    path.write_text(json.dumps(data))

    model = load_backtest_execution_model(str(path))

    assert model.is_like_for_like is False
    assert exit_code_for([], model) == EXIT_BASELINE_NOT_COMPARABLE


def test_load_execution_model_treats_a_pre_rebaseline_backtest_as_same_bar(
    tmp_path: Path,
):
    """A backtest JSON with no fill_model predates the rebaseline."""
    path = _write_backtest_json(tmp_path)

    model = load_backtest_execution_model(str(path))

    assert model.fill_model == "same_bar"
    assert model.is_like_for_like is False


def test_main_exits_three_against_a_blocked_coverage_baseline(
    tmp_path: Path, monkeypatch, capsys
):
    """End-to-end: a lossy point-in-time baseline is refused, loudly.

    The artifact is otherwise perfect — next-open fills, a commission floor, a
    point-in-time universe — so this pins that the coverage floor alone drives
    the run to exit 3 rather than scoring numbers it should not trust.
    """
    from scripts import divergence_monitor

    db_url = f"sqlite:///{tmp_path / 'blocked.db'}"
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    _seed_state(session)
    session.commit()
    session.close()

    bt_path = _write_backtest_json(tmp_path)
    data = json.loads(bt_path.read_text())
    data["config"].update({
        "fill_model": "next_open",
        "commission_minimum": 1.0,
        "point_in_time_universe": True,
        "coverage": {
            "state": "BLOCKED",
            "excluded_pct": 9.7,
            "total_membership_days": 12_000,
            "excluded_membership_days": 1_164,
            "excluded_tickers": {"DEAD": 1_164},
            "floor_pct": 5.0,
        },
    })
    bt_path.write_text(json.dumps(data))

    report_path = tmp_path / "divergence.json"
    monkeypatch.setattr(divergence_monitor.sys, "argv", [
        "divergence_monitor.py",
        "--backtest", str(bt_path),
        "--db-url", db_url,
        "--window", "5",
        "--output", str(report_path),
    ])

    code = divergence_monitor.main()

    assert code == EXIT_BASELINE_NOT_COMPARABLE
    assert "coverage" in capsys.readouterr().out.lower()
    statuses = {p["status"] for p in json.loads(report_path.read_text())["reports"]}
    assert statuses == {"NO_DATA"}


# ---------------------------------------------------------------------------
# Exit-code contract
# ---------------------------------------------------------------------------


def _report(status: str, comparable: bool = True) -> "PortfolioDivergenceReport":
    from backtest.divergence import PortfolioDivergenceReport

    return PortfolioDivergenceReport(
        portfolio="momentum", window_start=None, window_end=None,
        days_compared=0, live_return=None, backtest_return=None,
        absolute_divergence_pp=None, relative_divergence=None,
        daily_correlation=None, live_trades_in_window=0,
        realized_slippage_total=0.0, realized_slippage_bps=None,
        realized_commission_total=0.0, assumed_commission_total=0.0,
        status=status, baseline_comparable=comparable,
    )


def _model(comparable: bool):
    from backtest.divergence import ExecutionModel

    return ExecutionModel(
        fill_model="next_open" if comparable else "same_bar",
        commission_minimum=1.0 if comparable else 0.0,
        point_in_time_universe=comparable,
        coverage_state="OK" if comparable else "BLOCKED",
    )


class TestExitCode:
    """A blind monitor must not look like a healthy one.

    The deployed wrapper logs "Divergence monitor OK (exit 0)", so a forced
    NO_DATA from a stale baseline previously read as a clean daily run.
    """

    def test_all_ok_exits_zero(self):
        code = exit_code_for([_report("OK")], _model(comparable=True))
        assert code == EXIT_OK == 0

    def test_warning_exits_zero(self):
        assert exit_code_for([_report("WARNING")], _model(comparable=True)) == EXIT_OK

    def test_breach_exits_one(self):
        code = exit_code_for([_report("BREACH")], _model(comparable=True))
        assert code == EXIT_BREACH == 1

    def test_non_comparable_baseline_gets_its_own_code(self):
        code = exit_code_for(
            [_report("NO_DATA", comparable=False)], _model(comparable=False)
        )
        assert code == EXIT_BASELINE_NOT_COMPARABLE
        assert code not in (EXIT_OK, EXIT_BREACH, EXIT_ERROR)

    def test_genuine_no_data_on_a_good_baseline_still_exits_zero(self):
        """No overlapping live history yet is not a monitor fault."""
        code = exit_code_for(
            [_report("NO_DATA", comparable=True)], _model(comparable=True)
        )
        assert code == EXIT_OK

    def test_breach_outranks_a_non_comparable_baseline(self):
        code = exit_code_for(
            [_report("BREACH"), _report("NO_DATA", comparable=False)],
            _model(comparable=False),
        )
        assert code == EXIT_BREACH


@pytest.mark.parametrize(
    "code", [EXIT_BASELINE_NOT_COMPARABLE, EXIT_BASELINE_STALE]
)
def test_wrapper_documents_and_branches_on_every_exit_code(code):
    """deploy/launchd/run_divergence.sh must handle the code the script returns."""
    from pathlib import Path as _Path

    script = _Path("deploy/launchd/run_divergence.sh").read_text()
    assert f"{code} = " in script  # contract comment
    assert f"    {code})" in script  # case branch


# ---------------------------------------------------------------------------
# Baseline staleness (KAN-56)
# ---------------------------------------------------------------------------


def _baseline(tmp_path: Path, name: str, mtime_days_ago: int | None = None) -> Path:
    """An artifact on disk with the given name, optionally back-dated."""
    path = tmp_path / name
    path.write_text("{}")
    if mtime_days_ago is not None:
        stamp = (
            datetime.now(timezone.utc) - timedelta(days=mtime_days_ago)
        ).timestamp()
        os.utime(path, (stamp, stamp))
    return path


class TestBaselineStaleness:
    """The check nobody was running.

    The weekly refresh last succeeded on 2026-07-28; by 2026-08-18 the monitor
    had spent three weeks scoring live equity against a three-week-old baseline
    and reporting numbers with a straight face. Whichever job failed — and two
    different ones did — the artifact's own age is the signal that survives,
    because it is read at the point of consumption rather than at the point of
    production.
    """

    def test_a_three_week_old_baseline_is_stale(self, tmp_path: Path):
        _baseline(tmp_path, "backtest_multi_20260728_053111.json")
        age = baseline_age(
            str(tmp_path / "backtest_multi_20260728_053111.json"),
            max_age_days=14,
            today=date(2026, 8, 18),
        )
        assert age.age_days == 21
        assert age.is_stale

    def test_a_one_day_old_baseline_is_not_stale(self, tmp_path: Path):
        _baseline(tmp_path, "backtest_multi_20260817_053111.json")
        age = baseline_age(
            str(tmp_path / "backtest_multi_20260817_053111.json"),
            max_age_days=14,
            today=date(2026, 8, 18),
        )
        assert age.age_days == 1
        assert not age.is_stale

    def test_the_default_threshold_is_two_missed_refreshes(self):
        """The refresh is weekly, so 14 days means it has failed twice — one
        miss is a bad week, two is a broken job."""
        assert DEFAULT_MAX_BASELINE_AGE_DAYS == 14

    def test_the_threshold_is_configurable(self, tmp_path: Path):
        path = str(_baseline(tmp_path, "backtest_multi_20260810_000000.json"))
        eight_days = dict(today=date(2026, 8, 18))
        assert not baseline_age(path, max_age_days=14, **eight_days).is_stale
        assert baseline_age(path, max_age_days=7, **eight_days).is_stale

    def test_exactly_at_the_threshold_is_not_yet_stale(self, tmp_path: Path):
        path = str(_baseline(tmp_path, "backtest_multi_20260804_000000.json"))
        age = baseline_age(path, max_age_days=14, today=date(2026, 8, 18))
        assert age.age_days == 14
        assert not age.is_stale

    def test_the_age_comes_from_the_filename_not_the_mtime(self, tmp_path: Path):
        """The name records when the backtest was *run*; the mtime records when
        the file was last touched. A restored backup, an rsync or a `cp -r` of
        output/ rewrites the mtime and would silently make a stale baseline
        look fresh — the exact direction of error that must not happen."""
        path = str(
            _baseline(
                tmp_path, "backtest_multi_20260728_053111.json", mtime_days_ago=0
            )
        )
        age = baseline_age(path, max_age_days=14, today=date(2026, 8, 18))
        assert age.source == "filename"
        assert age.age_days == 21
        assert age.is_stale

    def test_an_unparseable_name_falls_back_to_the_file_mtime(self, tmp_path: Path):
        """`--backtest` accepts any path, including a hand-named one."""
        path = str(_baseline(tmp_path, "my-baseline.json", mtime_days_ago=30))
        age = baseline_age(path, max_age_days=14)
        assert age.source == "mtime"
        assert age.age_days is not None and age.age_days >= 29
        assert age.is_stale

    def test_an_unreadable_artifact_reports_an_unknown_age_and_is_not_stale(
        self, tmp_path: Path
    ):
        """Never invent staleness from a missing file: the caller has already
        hard-errored on that path, and guessing here would turn an absent
        artifact into a wrong verdict about a present one."""
        age = baseline_age(str(tmp_path / "gone.json"), max_age_days=14)
        assert age.age_days is None
        assert age.source == "unknown"
        assert not age.is_stale

    def test_the_warning_is_named_and_carries_the_numbers(self, tmp_path: Path):
        """"Named" is the point: an operator grepping the daily log, and the
        Telegram renderer reading the JSON report, both key off one token that
        does not drift with the prose around it."""
        path = str(_baseline(tmp_path, "backtest_multi_20260728_053111.json"))
        warning = baseline_age(
            path, max_age_days=14, today=date(2026, 8, 18)
        ).warning_line()
        assert BASELINE_STALE_WARNING in warning
        assert "21" in warning
        assert "14" in warning
        assert "backtest_multi_20260728_053111.json" in warning

    def test_a_fresh_baseline_has_no_warning_line(self, tmp_path: Path):
        path = str(_baseline(tmp_path, "backtest_multi_20260817_000000.json"))
        age = baseline_age(path, max_age_days=14, today=date(2026, 8, 18))
        assert age.warning_line() is None


class TestStaleExitCode:
    """Staleness has to reach a human, and on this host the only path that
    does is the exit code: `run_divergence.sh` sends its Telegram from the
    exit code, and no Prometheus/Alertmanager is running here to evaluate a
    gauge. A warning that only lands in ~/ibc/logs is the silence this story
    exists to remove.
    """

    def _stale(self, stale: bool):
        from scripts.divergence_monitor import BaselineAge

        return BaselineAge(
            path="output/backtest_multi_20260728_053111.json",
            age_days=21 if stale else 1,
            max_age_days=14,
            source="filename",
        )

    def test_a_stale_baseline_gets_its_own_exit_code(self):
        code = exit_code_for(
            [_report("OK")], _model(comparable=True), baseline=self._stale(True)
        )
        assert code == EXIT_BASELINE_STALE == 4
        assert code not in (
            EXIT_OK, EXIT_BREACH, EXIT_ERROR, EXIT_BASELINE_NOT_COMPARABLE
        )

    def test_a_fresh_baseline_still_exits_zero(self):
        code = exit_code_for(
            [_report("OK")], _model(comparable=True), baseline=self._stale(False)
        )
        assert code == EXIT_OK

    def test_omitting_the_baseline_keeps_the_old_contract(self):
        """Every existing caller passes two positional arguments."""
        assert exit_code_for([_report("OK")], _model(comparable=True)) == EXIT_OK

    def test_a_breach_outranks_a_stale_baseline(self):
        """A breach is the louder signal and its alert names the sleeves; the
        staleness rides along in the report rather than displacing it."""
        code = exit_code_for(
            [_report("BREACH")], _model(comparable=True), baseline=self._stale(True)
        )
        assert code == EXIT_BREACH

    def test_a_blind_baseline_outranks_a_stale_one(self):
        """BLIND means no drift detection is running at all; stale means it is
        running against old expectations. The worse outage wins."""
        code = exit_code_for(
            [_report("NO_DATA", comparable=False)],
            _model(comparable=False),
            baseline=self._stale(True),
        )
        assert code == EXIT_BASELINE_NOT_COMPARABLE


def test_the_json_report_records_the_baseline_age(tmp_path: Path):
    """The wrapper's alert renderer reads this file, not the console."""
    from scripts.divergence_monitor import BaselineAge

    out = tmp_path / "divergence.json"
    write_json_report(
        [_report("OK")],
        str(out),
        backtest_path="output/backtest_multi_20260728_053111.json",
        window_days=30,
        threshold=0.25,
        baseline=BaselineAge(
            path="output/backtest_multi_20260728_053111.json",
            age_days=21,
            max_age_days=14,
            source="filename",
        ),
    )
    payload = json.loads(out.read_text())
    assert payload["baseline_staleness"] == {
        "age_days": 21,
        "max_age_days": 14,
        "source": "filename",
        "stale": True,
    }


# ---------------------------------------------------------------------------
# Drill / synthetic portfolio exclusion (KAN-24)
# ---------------------------------------------------------------------------


def _seed_drill_sleeve(session: Session) -> None:
    """Add a funded __drill__ sleeve with its own equity history.

    A drill needs cash, so it needs a PortfolioConfig row — which means
    get_portfolio_names() returns it and the monitor sees it.
    """
    now = datetime.now(timezone.utc)
    session.add(PortfolioConfig(
        portfolio=DRILL_PORTFOLIO, capital=500.0, cash=500.0,
        created_at=now, updated_at=now,
    ))
    base = date(2026, 5, 19)
    value = 500.0
    for i in range(7):
        session.add(EquitySnapshot(
            portfolio=DRILL_PORTFOLIO,
            date=date.fromordinal(base.toordinal() + i),
            equity=value, cash=value, market_value=0.0, created_at=now,
        ))
        value *= 0.98  # a losing drill: would breach if it were ever scored
    session.flush()


def _write_backtest_json_with_drill_sleeve(tmp_path: Path) -> Path:
    """Backtest JSON that *does* contain a __drill__ sleeve.

    This is what makes the exclusion testable. Today the monitor happens to skip
    a drill sleeve via the "not present in backtest" branch; with a same-named
    baseline sleeve present that incidental protection disappears and only an
    explicit filter keeps the drill out of the scoring.
    """
    path = _write_backtest_json(tmp_path)
    data = json.loads(path.read_text())
    dates = data["portfolios"]["momentum"]["dates"]
    data["portfolios"][DRILL_PORTFOLIO] = {
        "config": {"capital": 500.0},
        "trades": [],
        "portfolio_values": [500.0] * (len(dates) + 1),
        "dates": dates,
        "metrics": {"total_return": 0.0},
    }
    path.write_text(json.dumps(data))
    return path


def test_monitor_skips_drill_portfolio_even_when_it_is_scoreable(
    tmp_path: Path, monkeypatch, capsys
):
    """AC3: __drill__ is skipped explicitly, with a logged reason.

    Both halves matter: the drill is present in the DB *and* in the baseline, so
    it is fully scoreable — and it must still never appear in a report.
    """
    from scripts import divergence_monitor

    db_path = tmp_path / "paper.db"
    db_url = f"sqlite:///{db_path}"
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    _seed_state(session)
    _seed_drill_sleeve(session)
    session.commit()
    session.close()

    bt_path = _write_backtest_json_with_drill_sleeve(tmp_path)
    report_path = tmp_path / "divergence.json"
    monkeypatch.setattr(divergence_monitor.sys, "argv", [
        "divergence_monitor.py",
        "--backtest", str(bt_path),
        "--db-url", db_url,
        "--window", "5",
        "--output", str(report_path),
    ])

    divergence_monitor.main()

    out = capsys.readouterr().out
    assert f"Skipping '{DRILL_PORTFOLIO}'" in out
    assert "excluded portfolio" in out
    assert "drill-evidence-isolation.md" in out

    scored = {p["portfolio"] for p in json.loads(report_path.read_text())["reports"]}
    assert DRILL_PORTFOLIO not in scored
    assert "momentum" in scored


def _run_monitor(tmp_path: Path, monkeypatch, *, name: str, with_drill: bool) -> dict:
    """Run main() against a fresh DB and return the parsed JSON report."""
    from scripts import divergence_monitor

    db_url = f"sqlite:///{tmp_path / (name + '.db')}"
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    _seed_state(session)
    if with_drill:
        _seed_drill_sleeve(session)
    session.commit()
    session.close()

    report_path = tmp_path / f"divergence_{name}.json"
    monkeypatch.setattr(divergence_monitor.sys, "argv", [
        "divergence_monitor.py",
        "--backtest", str(_write_backtest_json_with_drill_sleeve(tmp_path)),
        "--db-url", db_url,
        "--window", "5",
        "--output", str(report_path),
    ])
    divergence_monitor.main()
    return json.loads(report_path.read_text())


def test_drill_equity_is_absent_from_the_aggregate_report(
    tmp_path: Path, monkeypatch, capsys
):
    """The aggregate is the graded book's — adding a drill must change nothing.

    The seeded drill loses 2%/day, so if its series reached the aggregate the
    numbers would move. Comparing the two runs asserts exclusion end to end
    rather than re-deriving the expected figure.
    """
    without = _run_monitor(tmp_path, monkeypatch, name="graded", with_drill=False)
    with_drill = _run_monitor(tmp_path, monkeypatch, name="drilled", with_drill=True)
    capsys.readouterr()

    def _aggregate(payload: dict) -> dict:
        agg = next(r for r in payload["reports"] if r["portfolio"] == "AGGREGATE")
        return {k: v for k, v in agg.items() if k != "notes"}

    assert _aggregate(with_drill) == _aggregate(without)
    assert {r["portfolio"] for r in with_drill["reports"]} == {
        r["portfolio"] for r in without["reports"]
    }


# ---------------------------------------------------------------------------
# Evidence persistence (KAN-27)
# ---------------------------------------------------------------------------


def _comparable_backtest(
    tmp_path: Path,
    label: str = "20260525_000000",
    start: date | None = None,
    momentum_daily: float | None = None,
) -> Path:
    """A baseline the monitor is willing to grade against.

    The bare ``_write_backtest_json`` declares no execution model, which reads
    as pre-rebaseline (same-bar fills) and forces every verdict to NO_DATA.
    ``momentum_daily`` slows the momentum sleeve's backtest growth so live
    diverges from it far enough to BREACH.
    """
    path = _write_backtest_json(tmp_path, label=label, start=start)
    data = json.loads(path.read_text())
    data["config"].update({
        "fill_model": "next_open",
        "commission_minimum": 1.0,
        "point_in_time_universe": True,
        "coverage": {
            "state": "OK",
            "excluded_pct": 0.0,
            "total_membership_days": 12_000,
            "excluded_membership_days": 0,
            "excluded_tickers": {},
            "floor_pct": 5.0,
        },
    })
    if momentum_daily is not None:
        data["portfolios"]["momentum"]["portfolio_values"] = (
            [23080.0] + [23080.0 * (momentum_daily ** (i + 1)) for i in range(7)]
        )
    path.write_text(json.dumps(data))
    return path


def _evidence_db(
    tmp_path: Path,
    name: str,
    *,
    start: date | None = None,
    with_divergence_table: bool = True,
) -> str:
    """Create a seeded sqlite paper DB and return its URL.

    ``with_divergence_table=False`` builds every table EXCEPT ``divergence_daily``
    — a real un-migrated store, so the failure path is exercised by SQLAlchemy
    raising for real rather than by a stubbed writer.
    """
    from shared.models.evidence import DivergenceDaily

    db_url = f"sqlite:///{tmp_path / (name + '.db')}"
    engine = create_engine(db_url)
    tables = None
    if not with_divergence_table:
        tables = [
            t for t in Base.metadata.sorted_tables
            if t.name != DivergenceDaily.__tablename__
        ]
    Base.metadata.create_all(engine, tables=tables)
    session = sessionmaker(bind=engine)()
    _seed_state(session, start=start)
    session.commit()
    session.close()
    return db_url


def _run_monitor_main(
    monkeypatch, *, backtest: Path, db_url: str, output: Path, window: str = "5",
    extra: list[str] | None = None,
) -> int:
    from scripts import divergence_monitor

    extra = list(extra or [])
    # These fixtures pin a fixed filename stamp (``20260525_000000``), so every
    # one of them is permanently "stale" and would exit 4 on the staleness
    # check rather than on the contract each test is actually about. Turned off
    # here and exercised deliberately in TestStaleExitCode / the end-to-end
    # case below, rather than allowed to leak into unrelated assertions.
    if "--max-baseline-age-days" not in extra:
        extra += ["--max-baseline-age-days", "0"]

    monkeypatch.setattr(divergence_monitor.sys, "argv", [
        "divergence_monitor.py",
        "--backtest", str(backtest),
        "--db-url", db_url,
        "--window", window,
        "--output", str(output),
    ] + extra)
    return divergence_monitor.main()


def _capture_alerts(monkeypatch) -> list[tuple[str, dict]]:
    """Record what the monitor would publish to ``stream:alerts``.

    Every store-failure test installs this: without it the run would open a
    real Redis connection from the developer's config, which is both slow and
    capable of publishing a test alert onto the live stream.
    """
    from scripts import divergence_monitor

    published: list[tuple[str, dict]] = []

    class _FakeRedis:
        def xadd(self, stream, payload):
            published.append((stream, payload))

        def close(self):
            pass

    monkeypatch.setattr(
        divergence_monitor, "_redis_from_url", lambda url, **kw: _FakeRedis()
    )
    return published


def _verdict_rows(db_url: str) -> list:
    from shared.models.evidence import DivergenceDaily

    session = sessionmaker(bind=create_engine(db_url))()
    try:
        return list(
            session.query(DivergenceDaily)
            .order_by(DivergenceDaily.sleeve, DivergenceDaily.session_date)
            .all()
        )
    finally:
        session.close()


def test_one_verdict_row_per_scored_sleeve(tmp_path: Path, monkeypatch, capsys):
    """AC1: one row per included sleeve, statuses matching the report verbatim."""
    db_url = _evidence_db(tmp_path, "rows")
    report_path = tmp_path / "divergence.json"

    code = _run_monitor_main(
        monkeypatch,
        backtest=_comparable_backtest(tmp_path),
        db_url=db_url,
        output=report_path,
    )
    capsys.readouterr()

    assert code == EXIT_OK
    rows = _verdict_rows(db_url)
    reported = {
        r["portfolio"]: r["status"]
        for r in json.loads(report_path.read_text())["reports"]
        if r["portfolio"] != "AGGREGATE"
    }
    assert {row.sleeve: row.status for row in rows} == reported
    assert len(rows) == len(reported) == 2
    assert {row.baseline_id for row in rows} == {"backtest_multi_20260525_000000.json"}
    assert {row.window_sessions for row in rows} == {5}


def test_a_no_data_verdict_is_recorded_rather_than_skipped(
    tmp_path: Path, monkeypatch, capsys
):
    """AC2: "the monitor ran and could not judge" is an observation, not absence.

    Absence means the monitor did not run at all; KAN-26's pause-clock
    arithmetic distinguishes the two, so a NO_DATA verdict must leave a row.
    """
    db_url = _evidence_db(tmp_path, "nodata")
    # No declared execution model ⇒ pre-rebaseline baseline ⇒ every verdict NO_DATA.
    code = _run_monitor_main(
        monkeypatch,
        backtest=_write_backtest_json(tmp_path),
        db_url=db_url,
        output=tmp_path / "divergence.json",
    )
    capsys.readouterr()

    assert code == EXIT_BASELINE_NOT_COMPARABLE
    rows = _verdict_rows(db_url)
    assert len(rows) == 2
    assert {row.status for row in rows} == {"NO_DATA"}


def test_a_rerun_on_the_same_baseline_updates_rather_than_duplicates(
    tmp_path: Path, monkeypatch, capsys
):
    """AC3: the unique constraint is honoured by an upsert, not by a crash.

    The re-run is given a changed live series, so this pins that the row is
    actually rewritten. Asserting only the row count would pass just as well
    against a writer that silently did nothing on the second run.
    """
    db_url = _evidence_db(tmp_path, "idempotent")
    backtest = _comparable_backtest(tmp_path, momentum_daily=1.0005)
    report_path = tmp_path / "divergence.json"

    _run_monitor_main(
        monkeypatch, backtest=backtest, db_url=db_url, output=report_path
    )
    first = {row.sleeve: row for row in _verdict_rows(db_url)}
    assert first["momentum"].status == "BREACH"

    # A correction lands: momentum's live series now tracks the baseline.
    session = sessionmaker(bind=create_engine(db_url))()
    snapshots = session.query(EquitySnapshot).filter(
        EquitySnapshot.portfolio == "momentum"
    ).order_by(EquitySnapshot.date).all()
    for i, snapshot in enumerate(snapshots):
        snapshot.equity = 23080.0 * (1.0005 ** i)
    session.commit()
    session.close()

    _run_monitor_main(
        monkeypatch, backtest=backtest, db_url=db_url, output=report_path
    )
    capsys.readouterr()
    second = {row.sleeve: row for row in _verdict_rows(db_url)}

    assert len(second) == len(first) == 2
    assert second["momentum"].id == first["momentum"].id  # updated, not re-inserted
    assert second["momentum"].status == "OK"
    assert second["momentum"].metric_value != first["momentum"].metric_value
    assert second["momentum"].threshold == first["momentum"].threshold


def test_an_ad_hoc_rerun_cannot_overwrite_a_differently_pinned_verdict(
    tmp_path: Path, monkeypatch, capsys
):
    """A verdict is only meaningful under the window/threshold it was scored at.

    The runbook invites ad-hoc runs with a different --window/--threshold, and
    those land on the same (sleeve, session_date, baseline_id) key as the
    canonical 04:45 run — window_end does not move with --window. Letting one
    overwrite the other would let an exploratory command silently clear a
    firing breach streak that the capital ladder gates on, with no trace.
    """
    db_url = _evidence_db(tmp_path, "pinned")
    backtest = _comparable_backtest(tmp_path, momentum_daily=1.0005)
    report_path = tmp_path / "divergence.json"

    canonical = _run_monitor_main(
        monkeypatch, backtest=backtest, db_url=db_url, output=report_path
    )
    assert canonical == EXIT_BREACH
    before = {row.sleeve: (row.status, row.threshold) for row in _verdict_rows(db_url)}
    assert before["momentum"] == ("BREACH", pytest.approx(0.20))

    # The same session, scored at a threshold no divergence could breach.
    ad_hoc = _run_monitor_main(
        monkeypatch, backtest=backtest, db_url=db_url, output=report_path,
        extra=["--portfolio", "momentum", "--threshold", "9.0"],
    )
    out = capsys.readouterr().out

    assert ad_hoc == EXIT_OK  # the ad-hoc run reports its own verdict as usual
    after = {row.sleeve: (row.status, row.threshold) for row in _verdict_rows(db_url)}
    assert after == before
    assert "not recording 'momentum'" in out.lower()
    assert "differently-pinned" in out.lower()


def test_a_different_baseline_inserts_its_own_rows(
    tmp_path: Path, monkeypatch, capsys
):
    """AC3: a verdict is only interpretable against the baseline that produced it."""
    db_url = _evidence_db(tmp_path, "rebaselined")
    report_path = tmp_path / "divergence.json"

    _run_monitor_main(
        monkeypatch,
        backtest=_comparable_backtest(tmp_path, label="20260525_000000"),
        db_url=db_url,
        output=report_path,
    )
    _run_monitor_main(
        monkeypatch,
        backtest=_comparable_backtest(tmp_path, label="20260601_120000"),
        db_url=db_url,
        output=report_path,
    )
    capsys.readouterr()

    rows = _verdict_rows(db_url)
    assert len(rows) == 4
    assert {row.baseline_id for row in rows} == {
        "backtest_multi_20260525_000000.json",
        "backtest_multi_20260601_120000.json",
    }


def test_session_date_is_the_last_aligned_session_not_today(
    tmp_path: Path, monkeypatch, capsys
):
    """AC4: the row dates the session it describes, not the day it was written.

    The monitor runs after the close and can run late (a Saturday catch-up
    reports Friday), so stamping ``date.today()`` would silently mis-date the
    evidence the epoch clock counts.
    """
    last_session = date.fromordinal(date.today().toordinal() - 2)
    window_start = date.fromordinal(last_session.toordinal() - 6)
    db_url = _evidence_db(tmp_path, "dated", start=window_start)

    _run_monitor_main(
        monkeypatch,
        backtest=_comparable_backtest(tmp_path, start=window_start),
        db_url=db_url,
        output=tmp_path / "divergence.json",
    )
    capsys.readouterr()

    rows = _verdict_rows(db_url)
    assert rows
    assert {row.session_date for row in rows} == {last_session}
    assert date.today() not in {row.session_date for row in rows}


def test_the_aggregate_rollup_is_not_persisted(tmp_path: Path, monkeypatch, capsys):
    """AC5: AGGREGATE is derived truth (D15) — the digest recomputes it."""
    db_url = _evidence_db(tmp_path, "aggregate")
    report_path = tmp_path / "divergence.json"

    _run_monitor_main(
        monkeypatch,
        backtest=_comparable_backtest(tmp_path),
        db_url=db_url,
        output=report_path,
    )
    capsys.readouterr()

    reported = {r["portfolio"] for r in json.loads(report_path.read_text())["reports"]}
    assert "AGGREGATE" in reported  # the report still shows it
    assert {row.sleeve for row in _verdict_rows(db_url)} == {
        "momentum", "sector_rotation",
    }


def test_a_verdict_with_no_overlapping_window_writes_no_row(
    tmp_path: Path, monkeypatch, capsys
):
    """No aligned session ⇒ no session to date the row by ⇒ absence, not a placeholder.

    ``divergence_daily.session_date`` is the session a verdict covers. When live
    and the baseline share no dates there is no such session, and inventing
    today's would claim a verdict for a session that was never scored — which
    the blindness rule reads as evidence the monitor was working.
    """
    db_url = _evidence_db(tmp_path, "disjoint")
    backtest = _comparable_backtest(tmp_path, start=date(2025, 1, 6))

    code = _run_monitor_main(
        monkeypatch,
        backtest=backtest,
        db_url=db_url,
        output=tmp_path / "divergence.json",
    )
    out = capsys.readouterr().out

    assert code == EXIT_OK  # a genuine NO_DATA on a good baseline is not a fault
    assert _verdict_rows(db_url) == []
    assert "no aligned session" in out.lower()


def test_a_store_write_failure_leaves_the_exit_code_untouched(
    tmp_path: Path, monkeypatch, capsys
):
    """AC6: a store outage must never mask the verdict the wrapper branches on."""
    _capture_alerts(monkeypatch)
    db_url = _evidence_db(tmp_path, "unmigrated", with_divergence_table=False)

    code = _run_monitor_main(
        monkeypatch,
        backtest=_comparable_backtest(tmp_path),
        db_url=db_url,
        output=tmp_path / "divergence.json",
    )
    out = capsys.readouterr().out

    assert code == EXIT_OK
    assert "could not persist" in out.lower()


def test_a_breach_still_exits_one_when_the_store_write_fails(
    tmp_path: Path, monkeypatch, capsys
):
    """AC6: the louder signal survives — exit 1 is what pages the operator."""
    _capture_alerts(monkeypatch)
    db_url = _evidence_db(tmp_path, "breach", with_divergence_table=False)

    code = _run_monitor_main(
        monkeypatch,
        backtest=_comparable_backtest(tmp_path, momentum_daily=1.0005),
        db_url=db_url,
        output=tmp_path / "divergence.json",
    )
    out = capsys.readouterr().out

    assert code == EXIT_BREACH
    assert "could not persist" in out.lower()


def test_a_store_write_failure_raises_a_high_priority_alert(
    tmp_path: Path, monkeypatch, capsys
):
    """AC6: exit 0 is silent by design, so the alert is the only operator signal."""
    published = _capture_alerts(monkeypatch)
    db_url = _evidence_db(tmp_path, "alerting", with_divergence_table=False)

    _run_monitor_main(
        monkeypatch,
        backtest=_comparable_backtest(tmp_path),
        db_url=db_url,
        output=tmp_path / "divergence.json",
    )
    capsys.readouterr()

    assert len(published) == 1, published
    stream, payload = published[0]
    assert stream == "stream:alerts"
    assert payload["priority"] == "high"
    assert "divergence" in payload["message"].lower()


def test_the_alert_never_leaks_the_database_password(tmp_path: Path, monkeypatch):
    """The alert fans out to Telegram; the DSN in a DB error carries the password."""
    from scripts import divergence_monitor

    published = _capture_alerts(monkeypatch)
    divergence_monitor.emit_persist_failure_alert(
        RuntimeError("could not connect to postgresql://algo:hunter2@db:5432/algo_poc"),
        redis_url="redis://:secret@localhost:6379/0",
    )

    assert published
    assert "hunter2" not in published[0][1]["message"]


def test_a_dead_alert_channel_does_not_break_the_run(
    tmp_path: Path, monkeypatch, capsys
):
    """Best-effort by construction: the exit code is the guaranteed signal."""
    from scripts import divergence_monitor

    def _explode(url, **kwargs):
        raise ConnectionError("redis is down")

    monkeypatch.setattr(divergence_monitor, "_redis_from_url", _explode)
    db_url = _evidence_db(tmp_path, "deadalerts", with_divergence_table=False)

    code = _run_monitor_main(
        monkeypatch,
        backtest=_comparable_backtest(tmp_path),
        db_url=db_url,
        output=tmp_path / "divergence.json",
    )
    out = capsys.readouterr().out

    assert code == EXIT_OK
    assert "could not raise" in out.lower()


def test_the_persist_connection_is_bounded_by_a_statement_timeout():
    """The 04:45 job must not hang on the store.

    The verdict reaches the operator only after the process exits — the wrapper
    sends the Telegram message from the exit code — so an INSERT blocked on a
    lock (a migration mid-flight, a half-open connection) would swallow a
    BREACH entirely. Bounding the write turns that into a reported failure.
    """
    from scripts import divergence_monitor

    kwargs = divergence_monitor.persist_engine_kwargs(
        "postgresql://algo:pw@localhost:55432/algo_poc"
    )
    assert (
        f"statement_timeout={divergence_monitor.PERSIST_STATEMENT_TIMEOUT_MS}"
        in kwargs["connect_args"]["options"]
    )


def test_a_non_postgres_url_gets_no_postgres_only_connect_args():
    """sqlite (the test store) has no statement_timeout — passing one would fail."""
    from scripts import divergence_monitor

    assert divergence_monitor.persist_engine_kwargs("sqlite:///paper.db") == {}


# ---------------------------------------------------------------------------
# Baseline staleness, end to end through main() (KAN-56)
# ---------------------------------------------------------------------------


def test_main_exits_four_and_warns_against_a_stale_baseline(
    tmp_path: Path, monkeypatch, capsys
):
    """The whole story in one assertion: a comparable baseline whose artifact
    stopped being refreshed must not read as a clean daily run. Between
    2026-07-28 and 2026-08-18 it did, for three weeks."""
    _capture_alerts(monkeypatch)
    code = _run_monitor_main(
        monkeypatch,
        backtest=_comparable_backtest(tmp_path, label="20260525_000000"),
        db_url=_evidence_db(tmp_path, "stale"),
        output=tmp_path / "divergence.json",
        extra=["--max-baseline-age-days", "14"],
    )
    out = capsys.readouterr().out

    assert code == EXIT_BASELINE_STALE
    assert BASELINE_STALE_WARNING in out
    assert "backtest_multi_20260525_000000.json" in out


def test_main_exits_zero_against_a_fresh_baseline(tmp_path: Path, monkeypatch, capsys):
    """The other half of the contract — the check must not fire every day."""
    _capture_alerts(monkeypatch)
    today = date.today().strftime("%Y%m%d")
    code = _run_monitor_main(
        monkeypatch,
        backtest=_comparable_backtest(tmp_path, label=f"{today}_000000"),
        db_url=_evidence_db(tmp_path, "fresh"),
        output=tmp_path / "divergence.json",
        extra=["--max-baseline-age-days", "14"],
    )
    out = capsys.readouterr().out

    assert code == EXIT_OK
    assert BASELINE_STALE_WARNING not in out


def test_the_stale_warning_reaches_the_json_report_main_writes(
    tmp_path: Path, monkeypatch, capsys
):
    """The launchd wrapper's Telegram renderer reads the report, not stdout."""
    _capture_alerts(monkeypatch)
    report = tmp_path / "divergence.json"
    _run_monitor_main(
        monkeypatch,
        backtest=_comparable_backtest(tmp_path, label="20260525_000000"),
        db_url=_evidence_db(tmp_path, "stalereport"),
        output=report,
        extra=["--max-baseline-age-days", "14"],
    )
    capsys.readouterr()

    staleness = json.loads(report.read_text())["baseline_staleness"]
    assert staleness["stale"] is True
    assert staleness["source"] == "filename"
    assert staleness["max_age_days"] == 14


def test_the_staleness_check_can_be_turned_off_for_an_ad_hoc_run(
    tmp_path: Path, monkeypatch, capsys
):
    """Scoring deliberately against an old baseline is a legitimate manual
    operation; it must not be forced through a non-zero exit."""
    _capture_alerts(monkeypatch)
    code = _run_monitor_main(
        monkeypatch,
        backtest=_comparable_backtest(tmp_path, label="20260525_000000"),
        db_url=_evidence_db(tmp_path, "adhoc"),
        output=tmp_path / "divergence.json",
        extra=["--max-baseline-age-days", "0"],
    )
    capsys.readouterr()
    assert code == EXIT_OK


# ---------------------------------------------------------------------------
# The pinned baseline (KAN-51)
#
# Recency selection is not a pin. `find_latest_backtest_json` picks whatever
# sorts last in output/, and the weekly refresh writes a new artifact every
# Tuesday — so the baseline the gate evidence is measured against changed by
# itself, weekly, with nothing recording that it had. `--pinned` says "the
# --backtest path IS the baseline of record": there is no fallback, and both
# ways the pin can be wrong (absent, or describing a differently-shaped book)
# stop the run with a named reason instead of producing a verdict.
# ---------------------------------------------------------------------------


def _live_and_baseline(tmp_path: Path, name: str, *, label: str = "20260525_000000"):
    """A matched pair: a seeded sqlite book and a baseline of the same shape."""
    return _comparable_backtest(tmp_path, label=label), _evidence_db(tmp_path, name)


def test_a_pin_is_used_even_when_a_newer_artifact_exists(
    tmp_path: Path, monkeypatch, capsys
):
    """The regression the pin exists to prevent.

    A newer `backtest_multi_*.json` in output/ is exactly what the Tuesday
    refresh produces. Under recency it would silently become the baseline; under
    the pin it is ignored.
    """
    pinned, db_url = _live_and_baseline(tmp_path, "pinned", label="20260525_000000")
    newer = _comparable_backtest(tmp_path, label="20260901_000000")
    # Stand where the monitor would find the newer one by recency.
    output_dir = tmp_path / "cwd" / "output"
    output_dir.mkdir(parents=True)
    (output_dir / newer.name).write_text(newer.read_text())
    monkeypatch.chdir(tmp_path / "cwd")

    report = tmp_path / "divergence.json"
    code = _run_monitor_main(
        monkeypatch, backtest=pinned, db_url=db_url, output=report,
        extra=["--pinned"],
    )

    assert code == EXIT_OK, capsys.readouterr().out
    assert json.loads(report.read_text())["backtest_source"] == str(pinned)


def test_an_absent_pin_exits_three_and_names_itself(
    tmp_path: Path, monkeypatch, capsys
):
    """AC3. Not exit 2, and above all not a recency fallback: a job that ran,
    reported success and meant nothing is the 2026-08-13 failure pattern."""
    _, db_url = _live_and_baseline(tmp_path, "absent")
    # A perfectly good artifact sits in output/ — the point is it is not used.
    output_dir = tmp_path / "cwd" / "output"
    output_dir.mkdir(parents=True)
    recent = _comparable_backtest(tmp_path, label="20260901_000000")
    (output_dir / recent.name).write_text(recent.read_text())
    monkeypatch.chdir(tmp_path / "cwd")

    report = tmp_path / "divergence.json"
    code = _run_monitor_main(
        monkeypatch, backtest=tmp_path / "never_written.json", db_url=db_url,
        output=report, extra=["--pinned"],
    )

    assert code == EXIT_BASELINE_NOT_COMPARABLE
    out = capsys.readouterr().out
    assert BASELINE_PIN_MISSING in out, out
    assert "never_written.json" in out, out
    assert not report.exists(), "an unusable pin must not leave a verdict behind"


def test_pinned_with_no_path_at_all_exits_three(tmp_path: Path, monkeypatch, capsys):
    """The wrapper substitutes the resolver's output straight into --backtest, so
    an unconfigured pin arrives as an empty string. It must land here, not on
    the recency path."""
    from scripts import divergence_monitor

    _, db_url = _live_and_baseline(tmp_path, "nopath")
    output_dir = tmp_path / "cwd" / "output"
    output_dir.mkdir(parents=True)
    recent = _comparable_backtest(tmp_path, label="20260901_000000")
    (output_dir / recent.name).write_text(recent.read_text())
    monkeypatch.chdir(tmp_path / "cwd")

    monkeypatch.setattr(divergence_monitor.sys, "argv", [
        "divergence_monitor.py",
        "--backtest", "",
        "--pinned",
        "--db-url", db_url,
        "--max-baseline-age-days", "0",
        "--no-output",
    ])

    assert divergence_monitor.main() == EXIT_BASELINE_NOT_COMPARABLE
    assert BASELINE_PIN_MISSING in capsys.readouterr().out


def test_an_absent_backtest_without_the_pin_still_exits_two(
    tmp_path: Path, monkeypatch, capsys
):
    """AC8: the unpinned ad-hoc contract is untouched. A bad --backtest path is
    an argument error (2), not the baseline-not-comparable outage (3)."""
    _, db_url = _live_and_baseline(tmp_path, "adhoc")

    code = _run_monitor_main(
        monkeypatch, backtest=tmp_path / "absent.json", db_url=db_url,
        output=tmp_path / "divergence.json",
    )

    assert code == EXIT_ERROR
    assert BASELINE_PIN_MISSING not in capsys.readouterr().out


def test_recency_still_resolves_the_baseline_for_an_unpinned_run(
    tmp_path: Path, monkeypatch, capsys
):
    """Recency stays the documented default for ad-hoc local runs; only the
    nightly job is pinned."""
    from scripts import divergence_monitor

    db_url = _evidence_db(tmp_path, "recency")
    newest = _comparable_backtest(tmp_path, label="20260901_000000")
    output_dir = tmp_path / "cwd" / "output"
    output_dir.mkdir(parents=True)
    (output_dir / newest.name).write_text(newest.read_text())
    monkeypatch.chdir(tmp_path / "cwd")

    report = tmp_path / "divergence.json"
    monkeypatch.setattr(divergence_monitor.sys, "argv", [
        "divergence_monitor.py",
        "--db-url", db_url,
        "--window", "5",
        "--max-baseline-age-days", "0",
        "--output", str(report),
    ])

    assert divergence_monitor.main() == EXIT_OK, capsys.readouterr().out
    assert Path(json.loads(report.read_text())["backtest_source"]).name == newest.name


# --- shape mismatch (AC4) --------------------------------------------------


def _drop_sleeve(path: Path, sleeve: str) -> Path:
    data = json.loads(path.read_text())
    del data["portfolios"][sleeve]
    path.write_text(json.dumps(data))
    return path


def _add_sleeve(path: Path, sleeve: str) -> Path:
    data = json.loads(path.read_text())
    template = next(iter(data["portfolios"].values()))
    data["portfolios"][sleeve] = json.loads(json.dumps(template))
    path.write_text(json.dumps(data))
    return path


def test_a_pin_missing_a_live_sleeve_exits_three_without_a_verdict(
    tmp_path: Path, monkeypatch, capsys
):
    """AC4. `is_like_for_like` cannot catch this: the execution model is fine,
    the shapes are not. The aggregate sums only dates present in every
    portfolio, so a two-sleeve live book scored against a one-sleeve baseline is
    arithmetic, not a comparison."""
    pinned, db_url = _live_and_baseline(tmp_path, "shape_missing")
    _drop_sleeve(pinned, "sector_rotation")

    report = tmp_path / "divergence.json"
    code = _run_monitor_main(
        monkeypatch, backtest=pinned, db_url=db_url, output=report,
        extra=["--pinned"],
    )

    assert code == EXIT_BASELINE_NOT_COMPARABLE
    out = capsys.readouterr().out
    assert BASELINE_SHAPE_MISMATCH in out, out
    assert "sector_rotation" in out, out
    assert not report.exists()
    assert _verdict_rows(db_url) == []


def test_a_pin_carrying_an_extra_sleeve_exits_three(
    tmp_path: Path, monkeypatch, capsys
):
    """The Rung-0 case in the direction doc: a one-sleeve live book against a
    six-sleeve baseline. Detected from the baseline's side too, because that is
    the direction a rung change moves in."""
    pinned, db_url = _live_and_baseline(tmp_path, "shape_extra")
    _add_sleeve(pinned, "earnings_drift")

    code = _run_monitor_main(
        monkeypatch, backtest=pinned, db_url=db_url,
        output=tmp_path / "divergence.json", extra=["--pinned"],
    )

    assert code == EXIT_BASELINE_NOT_COMPARABLE
    out = capsys.readouterr().out
    assert BASELINE_SHAPE_MISMATCH in out, out
    assert "earnings_drift" in out, out


def test_the_shape_check_is_not_masked_by_a_single_portfolio_run(
    tmp_path: Path, monkeypatch, capsys
):
    """`--portfolio` narrows what is *scored*, never what is *compared* — else
    an ad-hoc run could report a clean sleeve out of a book the baseline does
    not describe."""
    pinned, db_url = _live_and_baseline(tmp_path, "shape_scoped")
    _drop_sleeve(pinned, "sector_rotation")

    code = _run_monitor_main(
        monkeypatch, backtest=pinned, db_url=db_url,
        output=tmp_path / "divergence.json",
        extra=["--pinned", "--portfolio", "momentum"],
    )

    assert code == EXIT_BASELINE_NOT_COMPARABLE
    assert BASELINE_SHAPE_MISMATCH in capsys.readouterr().out


def test_an_unpinned_run_still_tolerates_a_sleeve_the_baseline_lacks(
    tmp_path: Path, monkeypatch, capsys
):
    """AC8 again: the ad-hoc path keeps skipping-with-a-warning. Making the
    shape fatal everywhere would break every local run against an older
    artifact, which is not what this story buys."""
    adhoc, db_url = _live_and_baseline(tmp_path, "shape_adhoc")
    _drop_sleeve(adhoc, "sector_rotation")

    code = _run_monitor_main(
        monkeypatch, backtest=adhoc, db_url=db_url,
        output=tmp_path / "divergence.json",
    )

    out = capsys.readouterr().out
    assert code == EXIT_OK, out
    assert "Skipping 'sector_rotation'" in out
    assert BASELINE_SHAPE_MISMATCH not in out


def test_a_matching_pin_stamps_every_verdict_with_the_pinned_baseline_id(
    tmp_path: Path, monkeypatch, capsys
):
    """AC5. The evidence store already carries baseline_id; what the pin adds is
    that the value is the artifact of record, so rows judged against a different
    book shape are distinguishable by that column rather than merged into it."""
    pinned, db_url = _live_and_baseline(tmp_path, "stamped")

    code = _run_monitor_main(
        monkeypatch, backtest=pinned, db_url=db_url,
        output=tmp_path / "divergence.json", extra=["--pinned"],
    )

    assert code == EXIT_OK, capsys.readouterr().out
    rows = _verdict_rows(db_url)
    assert {r.sleeve for r in rows} == {"momentum", "sector_rotation"}
    assert {r.baseline_id for r in rows} == {pinned.name}


def test_shape_mismatch_ignores_synthetic_sleeves(
    tmp_path: Path, monkeypatch, capsys
):
    """A drill books into `__drill__`, which is in the live tables and can never
    be in a baseline. Counting it would make every pinned run a mismatch — the
    exclusion contract in docs/operations/drill-evidence-isolation.md applies
    here like everywhere else."""
    pinned, db_url = _live_and_baseline(tmp_path, "synthetic")
    session = sessionmaker(bind=create_engine(db_url))()
    now = datetime.now(timezone.utc)
    session.add(PortfolioConfig(
        portfolio=DRILL_PORTFOLIO, capital=100.0, cash=100.0,
        created_at=now, updated_at=now,
    ))
    session.commit()
    session.close()

    code = _run_monitor_main(
        monkeypatch, backtest=pinned, db_url=db_url,
        output=tmp_path / "divergence.json", extra=["--pinned"],
    )

    out = capsys.readouterr().out
    assert code == EXIT_OK, out
    assert BASELINE_SHAPE_MISMATCH not in out
