"""Live equity from before a re-baseline must not enter the comparison window.

``equity_snapshots`` carries a four-day block of invalid values:

    2026-07-25 | min sleeve  12,788 | total  96,532   sane
    2026-07-28 | min sleeve  -4,914 | total  31,733   invalid
    2026-07-29 | min sleeve  -4,914 | total  31,733   identical
    2026-07-30 | min sleeve  -4,914 | total  31,733   identical
    2026-07-31 | min sleeve  -4,914 | total  31,733   identical
    (no rows at all for 2026-08-01..08-04)
    2026-08-05 | min sleeve  12,829 | total 101,067   sane

Negative sleeve equity, a total a third of reality, byte-identical across four
days — a frozen book. That window sits between the 2026-07-25 bulk position
close and the **2026-08-01 Path A re-baseline**, when ``execution_fills`` began
recording real fills and the book's accounting became valid.

Nothing stopped the divergence window reaching across that boundary. The window
is 30 days, only ~22 live sessions exist, so it reaches into late July and
slides forward one session per run:

    shadow_20260910 -> window starts 2026-07-21   sane      momentum   +5.3%
    shadow_20260911 -> window starts 2026-07-22   sane
    shadow_20260912 -> window starts 2026-07-28   INVALID   momentum +498.9%

That produced a BREACH on five of six sleeves and an AGGREGATE of +207.4%,
delivered to Telegram. Every figure reproduces exactly from the 07-28 starting
values, so none of it was drift.

**Why KAN-82's guard is not enough.** Refusing a non-positive base catches
quality_value and thematic_momentum. It does *not* catch momentum
(3,822 -> 22,891 = +498.9%) or sector_rotation (6,152 -> 12,595 = +104.7%),
whose bases are positive and merely wrong. Only a boundary knows those are not
comparable.

**Where the boundary comes from.** ``gate_epochs`` is the mechanism the project
intends for this, but it is EMPTY today — epoch v2 has not started (KAN-33). So
the boundary is a configuration fact for now, in the manner of
``divergence.baseline_pin``, and an open epoch takes precedence once one exists.
The two must not disagree silently, which is why the resolver reports its source.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from backtest.divergence import (
    build_report,
    resolve_live_boundary,
    restrict_live_history,
    window_return,
)

# The real 2026-09-12 live series, abbreviated to the dates that matter.
LIVE_MOMENTUM = {
    date(2026, 7, 28): 3822.34,     # corrupt block begins
    date(2026, 7, 29): 3822.34,
    date(2026, 7, 30): 3822.34,
    date(2026, 7, 31): 3822.34,
    date(2026, 8, 5): 22944.28,     # sane again
    date(2026, 8, 8): 22944.28,
    date(2026, 9, 11): 22891.12,
}


def test_points_before_the_boundary_are_dropped():
    kept = restrict_live_history(LIVE_MOMENTUM, date(2026, 8, 1))
    assert min(kept) == date(2026, 8, 5), sorted(kept)
    assert len(kept) == 3, sorted(kept)


def test_the_boundary_is_inclusive_of_its_own_date():
    """A boundary of 2026-08-05 keeps 08-05: the re-baseline date itself is the
    first admissible session, not the last inadmissible one."""
    kept = restrict_live_history(LIVE_MOMENTUM, date(2026, 8, 5))
    assert min(kept) == date(2026, 8, 5)


def test_no_boundary_leaves_the_series_untouched():
    """Unset must not silently alter grading for anyone who has not configured
    it."""
    assert restrict_live_history(LIVE_MOMENTUM, None) == LIVE_MOMENTUM


def test_the_2026_09_12_case_stops_producing_a_three_digit_return():
    """The whole point. momentum's base moves from 3,822.34 to 22,944.28."""
    before = window_return([LIVE_MOMENTUM[d] for d in sorted(LIVE_MOMENTUM)])
    assert before == pytest.approx(4.9888, abs=1e-3), "precondition: the old +498.9%"

    kept = restrict_live_history(LIVE_MOMENTUM, date(2026, 8, 1))
    after = window_return([kept[d] for d in sorted(kept)])
    assert after is not None
    assert abs(after) < 0.10, f"expected a plausible return, got {after:+.1%}"


def test_a_sleeve_graded_within_the_boundary_is_plausible():
    live = restrict_live_history(LIVE_MOMENTUM, date(2026, 8, 1))
    backtest = {d: 60000.0 + i * 100 for i, d in enumerate(sorted(live))}
    report = build_report(
        portfolio="momentum", live=live, backtest=backtest, trades=[], window_days=30,
    )
    assert report.live_return is not None
    assert abs(report.live_return) < 0.10, report.live_return


# ---------------------------------------------------------------------------
# Where the boundary comes from
# ---------------------------------------------------------------------------


def test_the_configured_date_is_used_when_there_is_no_epoch():
    """Today's situation: gate_epochs is empty because epoch v2 has not started."""
    boundary, source = resolve_live_boundary(
        configured=date(2026, 8, 1), epoch_started_at=None
    )
    assert boundary == date(2026, 8, 1)
    assert "config" in source.lower(), source


def test_an_open_epoch_takes_precedence_over_the_configured_date():
    """Once KAN-33 starts epoch v2 the epoch is the authority, and the config
    value becomes history."""
    boundary, source = resolve_live_boundary(
        configured=date(2026, 8, 1),
        epoch_started_at=datetime(2026, 10, 1, 4, 15, tzinfo=timezone.utc),
    )
    assert boundary == date(2026, 10, 1)
    assert "epoch" in source.lower(), source


def test_an_epoch_alone_is_enough():
    boundary, source = resolve_live_boundary(
        configured=None,
        epoch_started_at=datetime(2026, 10, 1, 4, 15, tzinfo=timezone.utc),
    )
    assert boundary == date(2026, 10, 1)
    assert "epoch" in source.lower(), source


def test_neither_means_no_boundary_and_says_so():
    """Unconfigured must be reported, not silently treated as 'from the
    beginning of time' — that is the state this defect lived in."""
    boundary, source = resolve_live_boundary(configured=None, epoch_started_at=None)
    assert boundary is None
    assert source, "the resolver must still name the state"
    assert "none" in source.lower() or "unset" in source.lower(), source


def test_the_source_is_reported_so_the_two_cannot_disagree_silently():
    """An operator reading the report must be able to tell which authority
    produced the window start."""
    _, from_config = resolve_live_boundary(
        configured=date(2026, 8, 1), epoch_started_at=None
    )
    _, from_epoch = resolve_live_boundary(
        configured=date(2026, 8, 1),
        epoch_started_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
    )
    assert from_config != from_epoch, (from_config, from_epoch)
