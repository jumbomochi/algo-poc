"""A non-positive starting equity is not a denominator.

``window_return`` is ``last / first - 1`` and guarded ``values[0] == 0``. It did
not guard ``< 0``, so a negative base produced a number that is arithmetically
valid, semantically meaningless, and **sign flipped** — a sleeve that gained
money reported as having lost catastrophically.

The 2026-09-12 04:45 run delivered this to Telegram as a BREACH:

    quality_value:     live -1105.8% vs backtest -0.5%
    thematic_momentum: live  -396.0% vs backtest +3.9%

Both sleeves *gained* over the window:

    quality_value      -1,537.94 -> 15,468.14   =>  15468/(-1538) - 1 = -11.06
    thematic_momentum  -4,914.17 -> 14,546.15   =>  14546/(-4914) - 1 =  -3.96

The negative bases come from ``equity_snapshots`` rows written between the
2026-07-25 bulk position close and the 2026-08-01 Path A re-baseline, when the
book's accounting was not yet valid. Where they come from is KAN-83's problem.
This module is about the monitor rendering them as a confident percentage
instead of refusing them.

Why that matters on its own: the reported figure is wrong in *sign*, so the
reader's natural conclusion — "quality_value has blown up" — is the opposite of
the truth. It is also written to ``divergence_daily`` as gate evidence, so the
false verdict outlives the alert.

``window_return``'s own docstring already says "None if degenerate". A negative
base is degenerate; the implementation simply did not agree with its contract.

Series here are explicit rather than drawn from live data, so these tests stay
meaningful after KAN-83 changes which dates enter the window.
"""

from __future__ import annotations

from datetime import date

import pytest

from backtest.divergence import (
    build_report,
    daily_returns,
    window_return,
)


def test_a_negative_start_is_degenerate_not_a_percentage():
    """The quality_value case: -1537.94 -> 15468.14 reported as -1105.8%."""
    assert window_return([-1537.94, 15468.14]) is None


def test_a_negative_start_does_not_flip_the_sign_of_a_gain():
    """The specific harm. Rendering a gain as a catastrophic loss sends the
    reader to the strategy when the fault is in the data."""
    naive = 14546.15 / -4914.17 - 1.0
    assert naive < -3.0, "precondition: the old arithmetic produced a large negative"
    assert window_return([-4914.17, 14546.15]) is None


def test_zero_start_still_returns_none():
    """Existing behaviour, kept."""
    assert window_return([0.0, 100.0]) is None


def test_a_positive_window_is_unchanged():
    """The guard must not alter ordinary grading."""
    assert window_return([100.0, 110.0]) == pytest.approx(0.10)
    assert window_return([21812.38, 22891.12]) == pytest.approx(0.0494, abs=1e-4)


def test_a_small_but_positive_start_still_computes():
    """momentum on 2026-09-12 started at 3,822.34 — wrong, but positive. This
    guard deliberately does NOT catch it; only a boundary knows that value is
    not comparable, which is KAN-83. Pinned so the two stories stay distinct."""
    assert window_return([3822.34, 22891.12]) == pytest.approx(4.9888, abs=1e-3)


def test_daily_returns_skips_a_non_positive_transition():
    """Correlation is computed from these. The 2026-09-12 report showed
    correlations (+0.081, -0.034) derived across a negative point, which are
    meaningless for the same reason the totals were."""
    out = daily_returns([-100.0, 50.0, 60.0])
    assert out == [pytest.approx(0.2)], out


def test_daily_returns_is_unchanged_for_a_positive_series():
    assert daily_returns([100.0, 110.0, 121.0]) == [
        pytest.approx(0.10), pytest.approx(0.10)
    ]


def _series(values: list[float]) -> dict[date, float]:
    return {date(2026, 8, 1 + i): v for i, v in enumerate(values)}


def test_a_sleeve_with_a_negative_base_reports_no_data_with_a_named_reason():
    """NO_DATA, not BREACH — and the note must send the reader to the equity
    data rather than to the strategy."""
    report = build_report(
        portfolio="quality_value",
        live=_series([-1537.94, 8000.0, 15468.14]),
        backtest=_series([17646.38, 17700.0, 17750.65]),
        trades=[],
        window_days=30,
    )
    assert report.status == "NO_DATA", report.status
    assert report.live_return is None
    blob = " ".join(report.notes).lower()
    assert "equity" in blob, report.notes
    assert any(tok in blob for tok in ("-1537", "1537.94", "non-positive", "negative")), report.notes


def test_a_sleeve_with_a_positive_base_still_grades_normally():
    report = build_report(
        portfolio="momentum",
        live=_series([20000.0, 20500.0, 21000.0]),
        backtest=_series([20000.0, 20100.0, 20200.0]),
        trades=[],
        window_days=30,
    )
    assert report.status in {"OK", "WARNING", "BREACH"}, report.status
    assert report.live_return == pytest.approx(0.05)
