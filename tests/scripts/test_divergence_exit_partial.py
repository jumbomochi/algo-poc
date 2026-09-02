"""Telling an outage apart from a degradation at the exit-code layer.

Before this, ``any(not r.baseline_comparable ...)`` sent the whole run to
exit 3, whose alert reads "the monitor is BLIND ... no drift detection is
running". That is true when nothing could be graded and false — alarmingly so —
when five of six sleeves graded fine and one did not. And a BREACH outranks
everything, so a run that both breached and left half the book ungraded told
the operator only about the breach.

Exit 5 is the middle state: some sleeves graded, some did not. The earlier
objection to adding it (that precedence 5 > 4 would make staleness unreachable)
does not apply on the shadow path, where the baseline-age check is skipped
because a nightly rebuild has no meaningful file age.
"""
from __future__ import annotations

from datetime import date

import pytest

from backtest.divergence import PortfolioDivergenceReport
from scripts.divergence_monitor import (
    EXIT_BASELINE_NOT_COMPARABLE,
    EXIT_BREACH,
    EXIT_OK,
    EXIT_PARTIALLY_GRADED,
    exit_code_for,
)


def _r(portfolio: str, status: str, *, comparable: bool = True):
    return PortfolioDivergenceReport(
        portfolio=portfolio,
        window_start=date(2026, 8, 3), window_end=date(2026, 8, 7),
        days_compared=5, live_return=0.01, backtest_return=0.01,
        absolute_divergence_pp=0.0, relative_divergence=0.0,
        daily_correlation=1.0, live_trades_in_window=0,
        realized_slippage_total=0.0, realized_slippage_bps=None,
        realized_commission_total=0.0, assumed_commission_total=0.0,
        status=status, baseline_comparable=comparable,
    )


def test_every_sleeve_graded_is_ok() -> None:
    assert exit_code_for([_r("momentum", "OK"), _r("sector_rotation", "OK")]) == EXIT_OK


def test_no_sleeve_gradeable_is_a_full_outage() -> None:
    """Exit 3 keeps its meaning: nothing is being watched."""
    reports = [
        _r("momentum", "NO_DATA", comparable=False),
        _r("sector_rotation", "NO_DATA", comparable=False),
    ]

    assert exit_code_for(reports) == EXIT_BASELINE_NOT_COMPARABLE


def test_some_sleeves_graded_is_a_degradation_not_an_outage() -> None:
    """The case that used to claim 'no drift detection is running'."""
    reports = [
        _r("momentum", "OK"),
        _r("sector_rotation", "NO_DATA", comparable=False),
    ]

    assert exit_code_for(reports) == EXIT_PARTIALLY_GRADED


def test_a_breach_still_outranks_a_partial() -> None:
    """A breach is the louder signal and must not be downgraded. The ungraded
    sleeves reach the operator through the alert body instead."""
    reports = [
        _r("momentum", "BREACH"),
        _r("sector_rotation", "NO_DATA", comparable=False),
    ]

    assert exit_code_for(reports) == EXIT_BREACH


def test_the_aggregate_does_not_decide_the_exit_code() -> None:
    """AGGREGATE is a derived roll-up (D15). Letting it vote would report a
    degradation because the derived row could not be computed, even though
    every real sleeve graded."""
    reports = [
        _r("momentum", "OK"),
        _r("sector_rotation", "OK"),
        _r("AGGREGATE", "NO_DATA", comparable=False),
    ]

    assert exit_code_for(reports) == EXIT_OK


def test_a_genuine_no_data_on_a_comparable_feed_is_not_a_fault() -> None:
    """No overlapping live history yet is a young book, not a degradation."""
    reports = [_r("momentum", "NO_DATA", comparable=True)]

    assert exit_code_for(reports) == EXIT_OK


def test_partial_outranks_nothing_else_being_wrong() -> None:
    assert exit_code_for([_r("m", "WARNING"), _r("s", "NO_DATA", comparable=False)]) == (
        EXIT_PARTIALLY_GRADED
    )
