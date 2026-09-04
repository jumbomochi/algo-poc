"""Applying per-sleeve comparability to a shadow-scored report.

The existing pinned-artifact path forces NO_DATA through
``build_report``'s ``execution_model``. The shadow path has no execution model
to gate on — all four of its requirements are satisfied by construction — so the
refusal comes from :class:`SleeveComparability` instead.

The figures survive the refusal. ``docs/operations/divergence-monitor.md``
already sets that contract for the pinned path: "the arithmetic is still printed
so the gap is visible". A refusal that blanked the numbers would leave an
operator unable to see how far apart the curves were.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from backtest.divergence import PortfolioDivergenceReport
from backtest.sleeve_comparability import SleeveComparability
from scripts.divergence_monitor import apply_shadow_comparability


SESSIONS = [date(2026, 8, 3) + timedelta(days=i) for i in range(4)]


def _report(status="OK"):
    return PortfolioDivergenceReport(
        portfolio="momentum",
        window_start=SESSIONS[0],
        window_end=SESSIONS[-1],
        days_compared=4,
        live_return=0.05,
        backtest_return=0.01,
        absolute_divergence_pp=0.04,
        relative_divergence=4.0,
        daily_correlation=0.9,
        live_trades_in_window=3,
        realized_slippage_total=1.0,
        realized_slippage_bps=5.0,
        realized_commission_total=3.0,
        assumed_commission_total=2.0,
        status=status,
        notes=["pre-existing note"],
    )


def _verdict(**overrides):
    kwargs = dict(
        sleeve="momentum",
        run_date=SESSIONS[-1],
        shadow_produced_on=SESSIONS[-1],
        graded_session=SESSIONS[-1],
        overlapping_sessions=30,
    )
    kwargs.update(overrides)
    return SleeveComparability(**kwargs)


def test_a_comparable_sleeve_keeps_its_verdict() -> None:
    out = apply_shadow_comparability(_report("BREACH"), _verdict())

    assert out.status == "BREACH"
    assert out.baseline_comparable is True


def test_an_incomparable_sleeve_is_forced_to_no_data() -> None:
    out = apply_shadow_comparability(
        _report("BREACH"), _verdict(shadow_produced_on=SESSIONS[0])
    )

    assert out.status == "NO_DATA"
    assert out.baseline_comparable is False


def test_the_reasons_reach_the_notes() -> None:
    """The operator reads notes at 04:45; a bare NO_DATA explains nothing."""
    out = apply_shadow_comparability(
        _report(), _verdict(shadow_produced_on=SESSIONS[0])
    )

    assert any("stale" in n for n in out.notes)


def test_pre_existing_notes_are_kept() -> None:
    out = apply_shadow_comparability(
        _report(), _verdict(shadow_produced_on=SESSIONS[0])
    )

    assert "pre-existing note" in out.notes


def test_the_arithmetic_survives_the_refusal() -> None:
    """Same contract the pinned path already sets: refuse to grade, but still
    show the gap, or nobody can judge how far apart the curves were."""
    out = apply_shadow_comparability(
        _report(), _verdict(shadow_produced_on=SESSIONS[0])
    )

    assert out.live_return == pytest.approx(0.05)
    assert out.backtest_return == pytest.approx(0.01)
    assert out.absolute_divergence_pp == pytest.approx(0.04)


def test_every_reason_is_attached_not_just_the_first() -> None:
    out = apply_shadow_comparability(
        _report(),
        _verdict(shadow_produced_on=SESSIONS[0], overlapping_sessions=1),
    )

    assert sum(1 for n in out.notes if "stale" in n or "overlap" in n) == 2
