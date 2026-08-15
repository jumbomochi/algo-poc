"""How much of the point-in-time universe the baseline could actually price.

Scoring a backtest against a :class:`~shared.universe.MembershipCalendar`
removes survivorship bias only if the historical members can be priced. Any
name whose bars cannot be pulled is silently skipped by the runner — and a
silently-skipped delisting is survivorship bias walking back in through the
side door, because the names that go missing are exactly the ones that failed.

This module makes that leakage measurable and caps it. Coverage is counted in
**membership-days** — the sum over sessions of how many constituents the index
had that day — so a name that left the index in 2019 costs fewer days than one
present throughout. Exclusions above the floor mark the baseline ``BLOCKED``,
which ``backtest.divergence`` then treats as not-like-for-like, so a degraded
baseline is refused rather than quietly scored (direction doc D14).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from typing import Collection, Iterable, Mapping, Sequence

from shared.universe import MembershipCalendar

# Exclusions at or below this share of membership-days are tolerable; above it
# the baseline is not a point-in-time baseline in any meaningful sense.
DEFAULT_COVERAGE_FLOOR_PCT = 5.0

# Coverage states. ``MISSING`` is never produced by :func:`measure_coverage` —
# it is what a reader assigns to an artifact that declares no coverage block at
# all, so absence reads as unsafe rather than as a pass.
COVERAGE_OK = "OK"
COVERAGE_BLOCKED = "BLOCKED"
COVERAGE_MISSING = "MISSING"

# The floor is inclusive ("≤ 5%"), so an exclusion that lands exactly on it
# must pass even when the division leaves a float-representation crumb behind.
_FLOOR_EPSILON = 1e-9


@dataclass(frozen=True)
class CoverageReport:
    """What fraction of point-in-time membership the baseline could price.

    Serialised into the results artifact under ``config.coverage`` so the
    divergence monitor can gate on it without re-reading the bars.
    """

    total_membership_days: int
    excluded_membership_days: int
    excluded_pct: float
    excluded_tickers: dict[str, int]
    floor_pct: float
    state: str

    def to_dict(self) -> dict:
        """JSON-safe mapping for the results artifact's provenance block."""
        return asdict(self)


def priced_days_from_bars(
    bars_by_ticker: Mapping[str, Iterable[Mapping]],
) -> dict[str, set[date]]:
    """Sessions each ticker printed a bar for, from loaded backtest bars.

    A ticker absent from ``bars_by_ticker`` — the fetch failed, or the cache
    never had it — simply has no priced sessions, which is what makes its whole
    tenure count against the floor.
    """
    return {
        ticker: {bar["date"] for bar in bars}
        for ticker, bars in bars_by_ticker.items()
    }


def measure_coverage(
    membership: MembershipCalendar,
    *,
    sessions: Sequence[date],
    priced_tickers: Mapping[str, Collection[date]] | Collection[str],
    floor_pct: float = DEFAULT_COVERAGE_FLOOR_PCT,
) -> CoverageReport:
    """Measure priceable coverage of ``membership`` over ``sessions``.

    Args:
        membership: The point-in-time calendar the backtest was scored against.
            Only index constituents count — the ``always`` instruments (the
            sector/thematic ETFs) are not members, so they are not in the
            denominator.
        sessions: The trading sessions the backtest replayed. Sessions before
            the calendar's first snapshot have no members and contribute
            nothing to either side of the ratio.
        priced_tickers: Either a mapping of ticker -> the sessions it can be
            priced on (see :func:`priced_days_from_bars`), or a flat collection
            of tickers priceable on every session. The mapping form is the
            accurate one: a name delisting mid-window is priceable up to its
            last bar and not after, and only a per-session count charges it for
            the right number of days.
        floor_pct: Exclusion share at or below which the baseline is ``OK``.

    Returns:
        A :class:`CoverageReport`. ``state`` is ``BLOCKED`` above the floor and
        also when the window contains no membership-days at all — a run that
        never overlapped the membership history verified nothing, which is not
        the same as verifying everything.
    """
    is_priced = _priced_predicate(priced_tickers)

    total = 0
    excluded_by_ticker: dict[str, int] = {}
    for session in sessions:
        members = membership.members_as_of(session)
        total += len(members)
        for ticker in members:
            if not is_priced(ticker, session):
                excluded_by_ticker[ticker] = excluded_by_ticker.get(ticker, 0) + 1

    excluded = sum(excluded_by_ticker.values())
    excluded_pct = (excluded / total * 100.0) if total else 0.0
    within_floor = total > 0 and excluded_pct <= floor_pct + _FLOOR_EPSILON

    return CoverageReport(
        total_membership_days=total,
        excluded_membership_days=excluded,
        excluded_pct=excluded_pct,
        excluded_tickers=dict(sorted(excluded_by_ticker.items())),
        floor_pct=floor_pct,
        state=COVERAGE_OK if within_floor else COVERAGE_BLOCKED,
    )


def _priced_predicate(
    priced_tickers: Mapping[str, Collection[date]] | Collection[str],
):
    """Normalise both accepted shapes of ``priced_tickers`` into a predicate."""
    if isinstance(priced_tickers, Mapping):
        priced_days = {
            ticker: set(days) for ticker, days in priced_tickers.items()
        }
        return lambda ticker, day: day in priced_days.get(ticker, frozenset())
    everywhere = frozenset(priced_tickers)
    return lambda ticker, _day: ticker in everywhere
