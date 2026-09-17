"""Tests for ``backtest.divergence``.

The divergence module is pure (no I/O), so tests construct synthetic equity
series and trade lists directly rather than going through the DB or backtest
JSON loaders.
"""
from __future__ import annotations

from datetime import date

import pytest

from backtest.divergence import (
    DEFAULT_ABSOLUTE_WARN_PP,
    DEFAULT_THRESHOLD,
    MIN_RELATIVE_BASE,
    NEXT_OPEN_FILL_MODEL,
    SAME_BAR_FILL_MODEL,
    ExecutionModel,
    PortfolioDivergenceReport,
    aggregate_reports,
    align_and_window,
    any_breach,
    build_report,
    classify_status,
    commission_totals,
    compute_divergence,
    correlation,
    daily_returns,
    execution_model_from_backtest_config,
    filter_trades_to_window,
    slippage_bps,
    window_return,
)
from backtest.membership import COVERAGE_BLOCKED, COVERAGE_MISSING, COVERAGE_OK


# ---------------------------------------------------------------------------
# align_and_window
# ---------------------------------------------------------------------------


def test_align_and_window_intersects_dates_and_takes_last_N() -> None:
    live = {date(2026, 5, 1): 100.0, date(2026, 5, 2): 101.0, date(2026, 5, 3): 102.0}
    bt = {date(2026, 5, 1): 100.0, date(2026, 5, 2): 101.5, date(2026, 5, 4): 103.0}
    dates, lvals, btvals = align_and_window(live, bt, window_days=10)
    # Only 5/1 and 5/2 are shared.
    assert dates == [date(2026, 5, 1), date(2026, 5, 2)]
    assert lvals == [100.0, 101.0]
    assert btvals == [100.0, 101.5]


def test_align_and_window_returns_only_last_N_when_overlap_exceeds_window() -> None:
    live = {date(2026, 5, d): 100.0 + d for d in range(1, 11)}
    bt = {date(2026, 5, d): 100.0 + d * 0.9 for d in range(1, 11)}
    dates, lvals, btvals = align_and_window(live, bt, window_days=3)
    assert len(dates) == 3
    # Most recent 3 days.
    assert dates == [date(2026, 5, 8), date(2026, 5, 9), date(2026, 5, 10)]
    assert lvals == [108.0, 109.0, 110.0]


def test_align_and_window_returns_empty_when_no_overlap() -> None:
    live = {date(2026, 5, 1): 100.0}
    bt = {date(2026, 6, 1): 100.0}
    dates, lvals, btvals = align_and_window(live, bt, window_days=30)
    assert dates == []
    assert lvals == []
    assert btvals == []


# ---------------------------------------------------------------------------
# window_return / daily_returns / correlation
# ---------------------------------------------------------------------------


def test_window_return_simple() -> None:
    assert window_return([100.0, 110.0]) == pytest.approx(0.10)
    assert window_return([100.0, 105.0, 110.0]) == pytest.approx(0.10)


def test_window_return_none_for_too_few_or_zero_start() -> None:
    assert window_return([100.0]) is None
    assert window_return([]) is None
    assert window_return([0.0, 100.0]) is None


def test_daily_returns_skips_zero_denominator_transitions() -> None:
    values = [100.0, 101.0, 0.0, 100.0, 102.0]
    rets = daily_returns(values)
    # 100->101 (+1%), 101->0 (-100%), 0->100 skipped, 100->102 (+2%)
    assert len(rets) == 3
    assert rets[0] == pytest.approx(0.01)
    assert rets[1] == pytest.approx(-1.0)
    assert rets[2] == pytest.approx(0.02)


def test_correlation_perfect_positive() -> None:
    xs = [1.0, 2.0, 3.0, 4.0]
    ys = [2.0, 4.0, 6.0, 8.0]
    assert correlation(xs, ys) == pytest.approx(1.0)


def test_correlation_perfect_negative() -> None:
    xs = [1.0, 2.0, 3.0, 4.0]
    ys = [4.0, 3.0, 2.0, 1.0]
    assert correlation(xs, ys) == pytest.approx(-1.0)


def test_correlation_none_for_constant_series() -> None:
    # A constant series has zero variance; correlation is undefined.
    assert correlation([1.0, 1.0, 1.0], [2.0, 3.0, 4.0]) is None


def test_correlation_none_for_length_mismatch() -> None:
    assert correlation([1.0, 2.0], [1.0, 2.0, 3.0]) is None


def test_correlation_none_for_too_few_observations() -> None:
    assert correlation([1.0], [2.0]) is None


# ---------------------------------------------------------------------------
# compute_divergence / classify_status
# ---------------------------------------------------------------------------


def test_compute_divergence_absolute_and_relative() -> None:
    # live = 12%, bt = 10% -> abs = +2 pp, rel = +0.20
    abs_div, rel_div = compute_divergence(0.12, 0.10)
    assert abs_div == pytest.approx(0.02)
    assert rel_div == pytest.approx(0.20)


def test_compute_divergence_handles_negative_backtest() -> None:
    # live = -5%, bt = -10% -> abs = +5 pp, rel = +0.5
    abs_div, rel_div = compute_divergence(-0.05, -0.10)
    assert abs_div == pytest.approx(0.05)
    assert rel_div == pytest.approx(0.5)


def test_compute_divergence_relative_none_when_backtest_zero() -> None:
    abs_div, rel_div = compute_divergence(0.01, 0.0)
    assert abs_div == pytest.approx(0.01)
    assert rel_div is None


def test_compute_divergence_both_none_when_input_none() -> None:
    assert compute_divergence(None, 0.10) == (None, None)
    assert compute_divergence(0.10, None) == (None, None)


def test_classify_status_ok_when_within_thresholds() -> None:
    # Below both 20% relative and 2.5pp absolute -> OK.
    assert classify_status(relative=0.10, absolute_pp=0.01) == "OK"


def test_classify_status_ignores_a_relative_excess_on_a_negligible_gap() -> None:
    # Was: "relative 25% > 20%, absolute small -> WARNING", deliberately probed
    # at 0.024 to stay just under the absolute warn boundary. That is the shape
    # that paged on 2026-09-17 and the relative axis no longer fires alone on
    # it: 2.4 pp is a gap the absolute axis calls negligible, and a ratio
    # cannot promote a negligible gap into a reportable one.
    assert classify_status(relative=0.25, absolute_pp=0.024) == "OK"


def test_classify_status_warning_on_absolute_exceeded() -> None:
    # Absolute 3 pp > 2.5 pp warn, relative small -> WARNING.
    assert classify_status(relative=0.05, absolute_pp=0.03) == "WARNING"


def test_classify_status_breach_on_relative_exceeded() -> None:
    # Relative 50% > 40% (2x threshold) -> BREACH. The gap must also be one the
    # absolute axis already considers real; 3 pp is (> 2.5 pp warn) without
    # being a breach on its own (< 5 pp), so the relative axis is what decides.
    assert classify_status(relative=0.50, absolute_pp=0.03) == "BREACH"
    assert classify_status(relative=0.05, absolute_pp=0.03) == "WARNING"


def test_classify_status_breach_on_absolute_exceeded() -> None:
    # Absolute 6 pp > 5 pp breach -> BREACH.
    assert classify_status(relative=0.10, absolute_pp=0.06) == "BREACH"


def test_classify_status_no_data_when_both_none() -> None:
    assert classify_status(relative=None, absolute_pp=None) == "NO_DATA"


def test_classify_status_uses_absolute_alone_when_relative_none() -> None:
    # Backtest return was 0, so relative is None — but absolute is large.
    assert classify_status(relative=None, absolute_pp=0.10) == "BREACH"


# ---------------------------------------------------------------------------
# filter_trades_to_window
# ---------------------------------------------------------------------------


def test_filter_trades_includes_only_trades_in_window() -> None:
    trades = [
        {"exit_date": "2026-05-01", "pnl": 100},
        {"exit_date": "2026-05-15", "pnl": 200},
        {"exit_date": "2026-06-01", "pnl": 300},
    ]
    in_window = filter_trades_to_window(trades, date(2026, 5, 10), date(2026, 5, 31))
    assert len(in_window) == 1
    assert in_window[0]["pnl"] == 200


def test_filter_trades_handles_date_objects_and_iso_strings() -> None:
    trades = [
        {"exit_date": date(2026, 5, 5), "pnl": 1},
        {"exit_date": "2026-05-06", "pnl": 2},
    ]
    in_window = filter_trades_to_window(trades, date(2026, 5, 1), date(2026, 5, 10))
    assert len(in_window) == 2


def test_filter_trades_skips_unparseable_dates() -> None:
    trades = [
        {"exit_date": "garbage", "pnl": 1},
        {"exit_date": None, "pnl": 2},
        {"pnl": 3},  # missing exit_date entirely
        {"exit_date": "2026-05-05", "pnl": 4},
    ]
    in_window = filter_trades_to_window(trades, date(2026, 5, 1), date(2026, 5, 31))
    assert len(in_window) == 1
    assert in_window[0]["pnl"] == 4


# ---------------------------------------------------------------------------
# slippage_bps / commission_totals
# ---------------------------------------------------------------------------


def test_slippage_bps_weighted_by_notional() -> None:
    trades = [
        # $10 slippage on $10,000 notional = 10 bps
        {"slippage": 10.0, "quantity": 100, "exit_price": 100.0},
        # $5 slippage on $5,000 notional = 10 bps
        {"slippage": 5.0, "quantity": 50, "exit_price": 100.0},
    ]
    assert slippage_bps(trades) == pytest.approx(10.0)


def test_slippage_bps_returns_none_for_no_qualifying_trades() -> None:
    assert slippage_bps([]) is None
    # Zero notional trades should be filtered out.
    assert slippage_bps([{"slippage": 5.0, "quantity": 0, "exit_price": 100.0}]) is None


def test_slippage_bps_uses_price_if_exit_price_missing() -> None:
    # Some legacy trade dicts use ``price`` instead of ``exit_price``.
    trades = [{"slippage": 10.0, "quantity": 100, "price": 100.0}]
    assert slippage_bps(trades) == pytest.approx(10.0)


def test_commission_totals_returns_realized_and_assumed() -> None:
    # Both orders are small enough that the $1 per-order floor binds, so the
    # assumed cost is 2 orders x $1 per round trip rather than $0.005/share.
    trades = [
        {"commission": 1.0, "quantity": 100},
        {"commission": 0.50, "quantity": 50},
    ]
    realized, assumed = commission_totals(trades)
    assert realized == pytest.approx(1.50)
    assert assumed == pytest.approx(4.0)


# ---------------------------------------------------------------------------
# build_report
# ---------------------------------------------------------------------------


def _equity_series(start: date, days: int, daily_return: float) -> dict[date, float]:
    """Make a deterministic equity series at constant daily return."""
    out = {}
    val = 1000.0
    for i in range(days):
        out[date.fromordinal(start.toordinal() + i)] = val
        val *= 1 + daily_return
    return out


def _equity_series_varied(start: date, days: int, returns: list[float]) -> dict[date, float]:
    """Make an equity series from an explicit per-day return list. Length = days."""
    assert len(returns) == days
    out = {}
    val = 1000.0
    for i in range(days):
        out[date.fromordinal(start.toordinal() + i)] = val
        val *= 1 + returns[i]
    return out


def test_build_report_ok_when_live_matches_backtest() -> None:
    # Use varied returns so the correlation is defined (constant daily returns
    # produce zero-variance daily-return series, for which Pearson correlation
    # is undefined and the module correctly returns None).
    start = date(2026, 4, 28)
    rets = [0.001 + 0.0005 * (i % 3 - 1) for i in range(30)]
    live = _equity_series_varied(start, 30, rets)
    bt = _equity_series_varied(start, 30, rets)
    report = build_report("momentum", live, bt, trades=[], window_days=30)
    assert report.status == "OK"
    assert report.days_compared == 30
    assert report.live_return == pytest.approx(report.backtest_return)
    assert report.absolute_divergence_pp == pytest.approx(0.0)
    assert report.relative_divergence == pytest.approx(0.0)
    assert report.daily_correlation == pytest.approx(1.0)


def test_build_report_breach_when_live_diverges() -> None:
    start = date(2026, 4, 28)
    live = _equity_series(start, 30, 0.0)  # flat
    bt = _equity_series(start, 30, 0.005)  # +0.5%/day -> ~+15.6% over 30 days
    report = build_report("momentum", live, bt, trades=[], window_days=30)
    # Live flat vs +15.6% expected — both abs (>5 pp) and rel (>40%) breach.
    assert report.status == "BREACH"
    assert report.absolute_divergence_pp < -0.05
    assert "breach" in " ".join(report.notes).lower()


def test_build_report_no_data_status_when_no_overlap() -> None:
    live = {date(2026, 1, 1): 1000.0}
    bt = {date(2026, 6, 1): 1000.0}
    report = build_report("momentum", live, bt, trades=[], window_days=30)
    assert report.status == "NO_DATA"
    assert report.days_compared == 0
    assert report.live_return is None


def test_build_report_notes_when_partial_window() -> None:
    start = date(2026, 5, 1)
    live = _equity_series(start, 5, 0.001)  # only 5 days of live history
    bt = _equity_series(start, 5, 0.001)
    report = build_report("momentum", live, bt, trades=[], window_days=30)
    assert report.days_compared == 5
    assert any("Only 5 overlapping days" in n for n in report.notes)


def test_build_report_counts_trades_in_window_and_aggregates_slippage() -> None:
    start = date(2026, 5, 1)
    live = _equity_series(start, 10, 0.001)
    bt = _equity_series(start, 10, 0.001)
    trades = [
        {
            "exit_date": "2026-05-05",
            "quantity": 100,
            "exit_price": 50.0,
            "slippage": 5.0,
            "commission": 0.50,
        },
        # Outside the window (exit_date is before our start).
        {
            "exit_date": "2026-04-30",
            "quantity": 50,
            "exit_price": 100.0,
            "slippage": 10.0,
            "commission": 0.25,
        },
    ]
    report = build_report("momentum", live, bt, trades=trades, window_days=10)
    assert report.live_trades_in_window == 1
    assert report.realized_slippage_total == pytest.approx(5.0)
    # 100 shares * $50 = $5000 notional; $5 slip = 10 bps.
    assert report.realized_slippage_bps == pytest.approx(10.0)
    assert report.realized_commission_total == pytest.approx(0.50)


# ---------------------------------------------------------------------------
# aggregate_reports / any_breach
# ---------------------------------------------------------------------------


def test_aggregate_reports_builds_aggregate_report() -> None:
    start = date(2026, 5, 1)
    live_total = _equity_series(start, 10, 0.002)
    bt_total = _equity_series(start, 10, 0.0015)
    report = aggregate_reports(
        reports=[],
        live_total=live_total,
        backtest_total=bt_total,
        all_trades=[],
        window_days=10,
    )
    assert report.portfolio == "AGGREGATE"
    assert report.days_compared == 10
    # Live grew faster; expect positive abs divergence.
    assert report.absolute_divergence_pp > 0


def test_any_breach_true_if_any_report_breaches() -> None:
    reports = [
        PortfolioDivergenceReport(
            portfolio="a", window_start=None, window_end=None,
            days_compared=0, live_return=None, backtest_return=None,
            absolute_divergence_pp=None, relative_divergence=None,
            daily_correlation=None, live_trades_in_window=0,
            realized_slippage_total=0, realized_slippage_bps=None,
            realized_commission_total=0, assumed_commission_total=0,
            status="OK",
        ),
        PortfolioDivergenceReport(
            portfolio="b", window_start=None, window_end=None,
            days_compared=0, live_return=None, backtest_return=None,
            absolute_divergence_pp=None, relative_divergence=None,
            daily_correlation=None, live_trades_in_window=0,
            realized_slippage_total=0, realized_slippage_bps=None,
            realized_commission_total=0, assumed_commission_total=0,
            status="BREACH",
        ),
    ]
    assert any_breach(reports) is True


def test_any_breach_false_if_no_report_breaches() -> None:
    reports = [
        PortfolioDivergenceReport(
            portfolio="a", window_start=None, window_end=None,
            days_compared=0, live_return=None, backtest_return=None,
            absolute_divergence_pp=None, relative_divergence=None,
            daily_correlation=None, live_trades_in_window=0,
            realized_slippage_total=0, realized_slippage_bps=None,
            realized_commission_total=0, assumed_commission_total=0,
            status="WARNING",
        ),
    ]
    assert any_breach(reports) is False


# ---------------------------------------------------------------------------
# ExecutionModel / like-for-like baseline
# ---------------------------------------------------------------------------


class TestExecutionModel:
    """Finding 4.6: the monitor baselined against an unachievable backtest.

    Comparing live fills to a same-bar-fill backtest means live trails "by
    construction", so a real degradation is indistinguishable from the
    baseline's own optimism.
    """

    def test_next_open_backtest_with_real_costs_is_comparable(self):
        model = ExecutionModel(
            fill_model=NEXT_OPEN_FILL_MODEL,
            slippage_bps=10.0,
            commission_per_share=0.005,
            commission_minimum=1.0,
            point_in_time_universe=True,
            coverage_state=COVERAGE_OK,
        )
        assert model.is_like_for_like is True

    def test_same_bar_backtest_is_not_comparable(self):
        model = ExecutionModel(
            fill_model=SAME_BAR_FILL_MODEL,
            slippage_bps=10.0,
            commission_per_share=0.005,
            commission_minimum=1.0,
        )
        assert model.is_like_for_like is False

    def test_missing_commission_floor_is_not_comparable(self):
        model = ExecutionModel(
            fill_model=NEXT_OPEN_FILL_MODEL,
            slippage_bps=10.0,
            commission_per_share=0.005,
            commission_minimum=0.0,
        )
        assert model.is_like_for_like is False

    def test_config_without_a_fill_model_reads_as_legacy_same_bar(self):
        """Backtests saved before the rebaseline declared no fill model."""
        model = execution_model_from_backtest_config(
            {"slippage_bps": 10, "commission_per_share": 0.005}
        )
        assert model.fill_model == SAME_BAR_FILL_MODEL
        assert model.is_like_for_like is False

    def test_config_round_trips_the_declared_execution_model(self):
        model = execution_model_from_backtest_config({
            "fill_model": NEXT_OPEN_FILL_MODEL,
            "slippage_bps": 15,
            "commission_per_share": 0.007,
            "commission_minimum": 1.0,
            "point_in_time_universe": True,
            "coverage": {"state": COVERAGE_OK},
        })
        assert model.fill_model == NEXT_OPEN_FILL_MODEL
        assert model.slippage_bps == pytest.approx(15.0)
        assert model.commission_per_share == pytest.approx(0.007)
        assert model.commission_minimum == pytest.approx(1.0)
        assert model.is_like_for_like is True

    def test_missing_config_reads_as_legacy(self):
        assert execution_model_from_backtest_config(None).is_like_for_like is False


class TestCommissionFloorInAssumption:
    def test_assumed_commission_applies_the_per_order_floor(self):
        """Small live orders pay IB's $1 minimum, not $0.005/share."""
        model = ExecutionModel(
            fill_model=NEXT_OPEN_FILL_MODEL,
            slippage_bps=10.0,
            commission_per_share=0.005,
            commission_minimum=1.0,
        )
        # 4 shares: $0.02/side under the per-share rate, $1/side with the floor.
        trades = [{"commission": 2.0, "quantity": 4}]

        realized, assumed = commission_totals(trades, execution_model=model)

        assert realized == pytest.approx(2.0)
        assert assumed == pytest.approx(2.0)

    def test_large_orders_still_pay_the_per_share_rate(self):
        model = ExecutionModel(
            fill_model=NEXT_OPEN_FILL_MODEL,
            slippage_bps=10.0,
            commission_per_share=0.005,
            commission_minimum=1.0,
        )
        trades = [{"commission": 5.0, "quantity": 1_000}]

        _, assumed = commission_totals(trades, execution_model=model)

        assert assumed == pytest.approx(1_000 * 0.005 * 2)


class TestBuildReportBaselineComparability:
    def _series(self, start: date, days: int, daily: float) -> dict[date, float]:
        return _equity_series(start, days, daily)

    def test_report_records_the_baseline_execution_model(self):
        start = date(2026, 5, 1)
        model = ExecutionModel(
            fill_model=NEXT_OPEN_FILL_MODEL,
            slippage_bps=10.0,
            commission_per_share=0.005,
            commission_minimum=1.0,
            point_in_time_universe=True,
            coverage_state=COVERAGE_OK,
        )
        report = build_report(
            "momentum",
            self._series(start, 10, 0.001),
            self._series(start, 10, 0.001),
            trades=[],
            window_days=10,
            execution_model=model,
        )
        assert report.baseline_fill_model == NEXT_OPEN_FILL_MODEL
        assert report.baseline_comparable is True
        assert report.status == "OK"

    def test_same_bar_baseline_reports_no_data_instead_of_a_false_ok(self):
        start = date(2026, 5, 1)
        model = ExecutionModel(
            fill_model=SAME_BAR_FILL_MODEL,
            slippage_bps=10.0,
            commission_per_share=0.005,
            commission_minimum=0.0,
        )
        report = build_report(
            "momentum",
            self._series(start, 10, 0.001),
            self._series(start, 10, 0.005),
            trades=[],
            window_days=10,
            execution_model=model,
        )
        assert report.baseline_comparable is False
        assert report.status == "NO_DATA"
        assert any("not like-for-like" in note for note in report.notes)
        assert any(SAME_BAR_FILL_MODEL in note for note in report.notes)
        # The arithmetic is still reported so the operator can see the gap.
        assert report.live_return is not None
        assert report.backtest_return is not None

    def test_omitting_the_execution_model_keeps_the_report_usable(self):
        """Callers that never learned about baselines still get a report."""
        start = date(2026, 5, 1)
        report = build_report(
            "momentum",
            self._series(start, 10, 0.001),
            self._series(start, 10, 0.001),
            trades=[],
            window_days=10,
        )
        assert report.status == "OK"
        assert report.baseline_comparable is True

    def test_aggregate_report_carries_the_execution_model(self):
        start = date(2026, 5, 1)
        model = ExecutionModel(
            fill_model=SAME_BAR_FILL_MODEL,
            slippage_bps=10.0,
            commission_per_share=0.005,
            commission_minimum=0.0,
        )
        report = aggregate_reports(
            reports=[],
            live_total=self._series(start, 10, 0.002),
            backtest_total=self._series(start, 10, 0.0015),
            all_trades=[],
            window_days=10,
            execution_model=model,
        )
        assert report.portfolio == "AGGREGATE"
        assert report.baseline_comparable is False


class TestSurvivorshipBiasedBaselineIsNotComparable:
    """A next-open backtest over a survivorship-biased universe is still unfair.

    `--universe-snapshots` is opt-in, so the most likely re-run is one that
    fixed the fills and the costs but kept the static present-day ticker list.
    That baseline is inflated by winner pre-selection and live cannot match it,
    so the gate has to fail it too.
    """

    def _model(self, **overrides):
        kwargs = dict(
            fill_model=NEXT_OPEN_FILL_MODEL,
            slippage_bps=10.0,
            commission_per_share=0.005,
            commission_minimum=1.0,
            point_in_time_universe=True,
            coverage_state=COVERAGE_OK,
        )
        kwargs.update(overrides)
        return ExecutionModel(**kwargs)

    def test_static_universe_is_not_comparable(self):
        assert self._model(point_in_time_universe=False).is_like_for_like is False

    def test_point_in_time_universe_is_required_alongside_fills_and_costs(self):
        assert self._model().is_like_for_like is True

    def test_config_missing_the_flag_fails_safe(self):
        model = execution_model_from_backtest_config({
            "fill_model": NEXT_OPEN_FILL_MODEL,
            "commission_minimum": 1.0,
        })
        assert model.point_in_time_universe is False
        assert model.is_like_for_like is False

    def test_config_flag_is_read(self):
        model = execution_model_from_backtest_config({
            "fill_model": NEXT_OPEN_FILL_MODEL,
            "commission_minimum": 1.0,
            "point_in_time_universe": True,
            "coverage": {"state": COVERAGE_OK},
        })
        assert model.is_like_for_like is True

    def test_default_field_value_fails_safe(self):
        """Constructing a model without opting in must not claim comparability."""
        bare = ExecutionModel(fill_model=NEXT_OPEN_FILL_MODEL, commission_minimum=1.0)
        assert bare.point_in_time_universe is False

    def test_report_forces_no_data_for_a_static_universe_baseline(self):
        start = date(2026, 5, 1)
        report = build_report(
            "momentum",
            _equity_series(start, 10, 0.001),
            _equity_series(start, 10, 0.001),
            trades=[],
            window_days=10,
            execution_model=self._model(point_in_time_universe=False),
        )
        assert report.baseline_comparable is False
        assert report.status == "NO_DATA"
        assert any("survivorship" in note.lower() for note in report.notes)


class TestCoverageFloorIsPartOfComparability:
    """A point-in-time universe you cannot price is survivorship bias again.

    Story KAN-22 / direction doc D14. The names whose bars fail to pull are
    disproportionately the delistings, so an unbounded exclusion rate rebuilds
    exactly the bias `--universe-snapshots` was added to remove — one pipe
    stage later, where nothing was looking.
    """

    def _model(self, **overrides):
        kwargs = dict(
            fill_model=NEXT_OPEN_FILL_MODEL,
            slippage_bps=10.0,
            commission_per_share=0.005,
            commission_minimum=1.0,
            point_in_time_universe=True,
            coverage_state=COVERAGE_OK,
        )
        kwargs.update(overrides)
        return ExecutionModel(**kwargs)

    def test_blocked_coverage_is_not_comparable(self):
        assert self._model(coverage_state=COVERAGE_BLOCKED).is_like_for_like is False

    def test_unmeasured_coverage_is_not_comparable(self):
        assert self._model(coverage_state=COVERAGE_MISSING).is_like_for_like is False

    def test_default_field_value_fails_safe(self):
        """A model built without thinking about coverage must not claim it."""
        bare = ExecutionModel(fill_model=NEXT_OPEN_FILL_MODEL, commission_minimum=1.0)
        assert bare.coverage_state == COVERAGE_MISSING

    def test_full_coverage_does_not_block_an_otherwise_good_baseline(self):
        """Regression guard: the new requirement only ever subtracts."""
        assert self._model().is_like_for_like is True

    def test_unmet_requirements_names_coverage_in_both_failing_states(self):
        blocked = self._model(coverage_state=COVERAGE_BLOCKED).unmet_requirements()
        missing = self._model(coverage_state=COVERAGE_MISSING).unmet_requirements()

        assert any("coverage" in reason.lower() for reason in blocked)
        assert any(COVERAGE_BLOCKED in reason for reason in blocked)
        assert any("coverage" in reason.lower() for reason in missing)
        assert any(COVERAGE_MISSING in reason for reason in missing)
        # A good baseline says nothing about coverage at all.
        assert self._model().unmet_requirements() == []

    def test_config_without_a_coverage_block_reads_as_missing(self):
        model = execution_model_from_backtest_config({
            "fill_model": NEXT_OPEN_FILL_MODEL,
            "commission_minimum": 1.0,
            "point_in_time_universe": True,
        })
        assert model.coverage_state == COVERAGE_MISSING
        assert model.is_like_for_like is False

    def test_config_carries_the_blocked_state_through(self):
        model = execution_model_from_backtest_config({
            "fill_model": NEXT_OPEN_FILL_MODEL,
            "commission_minimum": 1.0,
            "point_in_time_universe": True,
            "coverage": {
                "state": COVERAGE_BLOCKED,
                "excluded_pct": 8.4,
                "total_membership_days": 1000,
            },
        })
        assert model.coverage_state == COVERAGE_BLOCKED
        assert model.is_like_for_like is False

    def test_report_forces_no_data_for_a_blocked_coverage_baseline(self):
        start = date(2026, 5, 1)
        report = build_report(
            "momentum",
            _equity_series(start, 10, 0.001),
            _equity_series(start, 10, 0.001),
            trades=[],
            window_days=10,
            execution_model=self._model(coverage_state=COVERAGE_BLOCKED),
        )
        assert report.baseline_comparable is False
        assert report.status == "NO_DATA"
        assert any("coverage" in note.lower() for note in report.notes)


# ---------------------------------------------------------------------------
# The relative ratio needs a baseline worth dividing by (2026-09-16)
# ---------------------------------------------------------------------------
# The guard was `abs(bt_ret) < 1e-9` — exact zero only — so any baseline that
# merely rounded near zero still produced a ratio. On 2026-09-16, with every
# sleeve inside a single percentage point of its shadow, the monitor breached
# on all four:
#
#   sector_rotation  Δ -0.82 pp  ->   -42.2%  (baseline +1.9%)
#   tail_risk_hedge  Δ +0.18 pp  ->   +93.8%  (baseline -0.2%)
#   AGGREGATE        Δ -0.15 pp  ->  -425.2%  (baseline +0.0%)
#
# Fifteen basis points reported as -425% is arithmetic, not tracking error.


def test_a_baseline_below_the_floor_yields_no_relative() -> None:
    """0.2% over the window is arithmetically non-zero and still a useless
    denominator — the exact case 1e-9 let through."""
    absolute, relative = compute_divergence(-0.002, -0.004)
    assert absolute == pytest.approx(0.002)
    assert relative is None


def test_a_baseline_above_the_floor_still_yields_a_relative() -> None:
    """The floor must not disable the relative axis on a real move."""
    absolute, relative = compute_divergence(0.15, 0.10)
    assert absolute == pytest.approx(0.05)
    assert relative == pytest.approx(0.5)


def test_the_floor_is_the_absolute_warn_threshold() -> None:
    """Tied to an existing constant rather than invented: a ratio against a
    baseline smaller than the gap we already call negligible is meaningless."""
    assert MIN_RELATIVE_BASE == DEFAULT_ABSOLUTE_WARN_PP


def test_the_2026_09_16_breaches_become_ok() -> None:
    """Every one of the four, with its real figures. All were sub-1 pp."""
    for live, backtest in (
        (-0.012, -0.024),   # momentum         Δ +1.17 pp, was +49.2%
        (0.011, 0.019),     # sector_rotation  Δ -0.82 pp, was -42.2%
        (-0.0001, -0.002),  # tail_risk_hedge  Δ +0.18 pp, was +93.8%
        (-0.001, 0.0005),   # AGGREGATE        Δ -0.15 pp, was -425.2%
    ):
        absolute, relative = compute_divergence(live, backtest)
        assert relative is None, (live, backtest)
        assert classify_status(relative, absolute) == "OK", (live, backtest)


def test_a_real_divergence_on_a_flat_baseline_still_breaches() -> None:
    """The floor must not become a blind spot. With no relative axis the
    absolute one governs, and it is unchanged: 5 pp is still a breach however
    little the baseline moved."""
    absolute, relative = compute_divergence(0.07, 0.001)
    assert relative is None
    assert classify_status(relative, absolute) == "BREACH"


def test_a_moderate_divergence_on_a_flat_baseline_still_warns() -> None:
    absolute, relative = compute_divergence(0.03, 0.001)
    assert relative is None
    assert classify_status(relative, absolute) == "WARNING"


def test_the_write_off_scale_divergence_would_still_have_breached() -> None:
    """Sanity: the -35.96 pp gap this monitor was reporting before the ledger
    was repaired breaches on the absolute axis alone, so the floor could never
    have hidden it."""
    absolute, relative = compute_divergence(-0.340, 0.019)
    assert relative is None
    assert classify_status(relative, absolute) == "BREACH"


# ---------------------------------------------------------------------------
# MIN_RELATIVE_BASE removed the absurd ratios but left a discontinuity at its
# own edge, and the monitor paged on it the very next morning (2026-09-17):
# `momentum` was -2.8% over the window, a hair ABOVE the 2.5% floor, so the
# ratio was taken and a 1.53 pp gap read as +54.7% -> BREACH.
#
#   baseline 2.49%  ->  relative is None, so a breach needs 5.0 pp
#   baseline 2.51%  ->  ratio is taken, so a breach needs 1.0 pp
#
# A hair of baseline movement moved the breach threshold five-fold. The floor
# decides whether the ratio EXISTS; it cannot also decide whether a gap is big
# enough to act on. So the relative axis no longer fires alone: it may escalate
# a gap that already clears the negligible bar, and may not manufacture one.


def test_the_momentum_breach_of_2026_09_17_becomes_ok() -> None:
    """The real figures: -2.8% baseline, live -1.27%, a 1.53 pp gap."""
    absolute, relative = compute_divergence(-0.0127, -0.028)
    assert absolute == pytest.approx(0.0153)
    assert relative == pytest.approx(0.546, abs=0.01)
    assert classify_status(relative, absolute) == "OK"


def test_the_floor_edge_is_not_a_cliff() -> None:
    """The same 1.5 pp gap either side of the floor must classify the same.
    Before, one was OK and the other BREACH."""
    below = compute_divergence(-0.0099, -0.0249)
    above = compute_divergence(-0.0101, -0.0251)
    assert classify_status(*reversed(below)) == classify_status(*reversed(above))
    assert classify_status(*reversed(above)) == "OK"


def test_the_relative_axis_still_escalates_a_real_gap() -> None:
    """Suppression must not disarm the axis. A 3.5 pp gap on an 8% baseline is
    43% off track: the absolute axis alone would only warn, the relative one
    correctly makes it a breach."""
    absolute, relative = compute_divergence(0.045, 0.080)
    assert relative == pytest.approx(-0.4375)
    assert classify_status(None, absolute) == "WARNING"
    assert classify_status(relative, absolute) == "BREACH"


def test_the_relative_axis_still_warns_on_a_real_gap() -> None:
    absolute, relative = compute_divergence(0.070, 0.100)
    assert classify_status(relative, absolute) == "WARNING"


def test_a_negligible_gap_cannot_breach_however_large_the_ratio() -> None:
    """The general property behind the fix, stated once."""
    for live, backtest in (
        (0.001, 0.030),   # 2.9 pp on a 3% baseline -> -96.7%
        (-0.010, 0.005),  # 1.5 pp on a 0.5% baseline
        (0.040, 0.026),   # 1.4 pp just above the floor
    ):
        absolute, relative = compute_divergence(live, backtest)
        if abs(absolute) <= DEFAULT_ABSOLUTE_WARN_PP:
            assert classify_status(relative, absolute) == "OK", (live, backtest)
