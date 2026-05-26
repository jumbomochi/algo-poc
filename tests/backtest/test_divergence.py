"""Tests for ``backtest.divergence``.

The divergence module is pure (no I/O), so tests construct synthetic equity
series and trade lists directly rather than going through the DB or backtest
JSON loaders.
"""
from __future__ import annotations

from datetime import date

import pytest

from backtest.divergence import (
    DEFAULT_THRESHOLD,
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
    filter_trades_to_window,
    slippage_bps,
    window_return,
)


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


def test_classify_status_warning_on_relative_exceeded() -> None:
    # Relative 25% > 20% warn threshold, absolute small -> WARNING.
    # absolute = 0.025 is at the warn boundary; use 0.024 to stay below.
    assert classify_status(relative=0.25, absolute_pp=0.024) == "WARNING"


def test_classify_status_warning_on_absolute_exceeded() -> None:
    # Absolute 3 pp > 2.5 pp warn, relative small -> WARNING.
    assert classify_status(relative=0.05, absolute_pp=0.03) == "WARNING"


def test_classify_status_breach_on_relative_exceeded() -> None:
    # Relative 50% > 40% (2x threshold) -> BREACH.
    assert classify_status(relative=0.50, absolute_pp=0.02) == "BREACH"


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
    # 100 shares + 50 shares = 150 shares total; round trip is 300 fills * $0.005
    trades = [
        {"commission": 1.0, "quantity": 100},
        {"commission": 0.50, "quantity": 50},
    ]
    realized, assumed = commission_totals(trades)
    assert realized == pytest.approx(1.50)
    assert assumed == pytest.approx(150 * 0.005 * 2)  # = $1.50


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
