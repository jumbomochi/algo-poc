"""Periods in which a sleeve's evidence was produced from missing or stale data.

The sibling of :mod:`shared.absent_sessions` (KAN-67). That register records
sessions on which no graded run happened at all. This one records sessions on
which the runs DID happen, wrote rows that look exactly like every other row,
and were nonetheless not evidence about the strategy — because the data the
strategy decides on was not there.

THE DECISION (KAN-109, operator decision 2026-10-08)
----------------------------------------------------
See :data:`DECISION`. In short: ``quality_value`` and ``earnings_drift`` live
and shadow evidence is recorded as data-degraded from the date each cache went
stale, and as empty from US session 2026-09-23 until fresh caches ship
(KAN-110). Nothing in the database is rewritten; the readers that grade or
summarise evidence (the divergence monitor, the epoch report, the go-live gate
and the weekly digest) name the gap wherever their period overlaps it, so a
verdict from inside it is not read as a strategy result.

HOW THE DATES WERE DERIVED
--------------------------
From the only caches that ever existed, ``~/GitHub/algo-poc/data/cache/``
(both files dated 2026-03-26, read-only), and the paper logs:

* ``fundamentals.json`` — 97 tickers, report periods 2024-06-30 to 2026-02-28,
  5-7 quarters each. 82 of 97 tickers' latest period is 2025-12-31. The next
  quarter (2026-03-31) became available on 2026-05-15 under the 45-day filing
  lag the point-in-time lookup applies, and the cache never had it: STALE from
  2026-05-15. (KAN-109's period check, with its default 137-day bound, would
  first have fired on 2026-05-18.)
* ``earnings.json`` — 98 tickers; the last announcement with a reported EPS is
  2026-03-19. Every announcement after the 2026-03-26 fetch is present only as
  a scheduled date with no reported EPS (NaN surprise). The first is
  2026-04-10: STALE from 2026-04-10. Worse than stale until 2026-06-19: the
  signal function compared ``nan < 5.0``, which is false, so each scheduled
  announcement passed the surprise threshold and became an ENTRY. Live paper
  trading began after the last such event (2026-06-18, lookup window to
  06-20), so no live row carries one — but every backtest run on that cache
  does (see :data:`BACKTEST_COVERAGE`). Fixed by KAN-109.
* Both caches are gitignored, so they never reached the deploy clone (KAN-72).
  The cut-over was 2026-09-24 02:08 SGT; the 04:15 SGT run that morning was
  the first from the clone, and it valued US session 2026-09-23. From that
  session both caches loaded as ``{}`` with no message: EMPTY.

All dates are US sessions (the session a run valued, KAN-103), like
:mod:`shared.absent_sessions`.

WHAT THIS REGISTER DOES NOT DO
------------------------------
It changes no grade and no row. A divergence verdict inside a gap is still the
verdict that was recorded; a gap does not pause, extend or excuse anything the
ladder computes. Like the absence register, it classifies and reports — if
adding a line here could change a score, a bad period could be laundered by
editing a file.

Closing an open entry (``end=None``) is part of the change that delivers fresh
caches: set ``end`` to the last session the empty cache was traded on.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

__all__ = [
    "BACKTEST_COVERAGE",
    "DECISION",
    "KNOWN_DATA_GAPS",
    "BacktestCoverage",
    "DataGap",
    "data_gaps_in",
    "describe_gaps",
]

#: The cache had data, but data a fresh fetch would have replaced.
STALE = "stale"
#: The cache did not exist where the run looked; the sleeve had no data at all.
EMPTY = "empty"


@dataclass(frozen=True)
class DataGap:
    """One sleeve's evidence, over one period, produced from bad data."""

    sleeve: str
    #: ``fundamentals`` or ``earnings`` — the cache the sleeve reads.
    cache: str
    kind: str
    #: First US session affected, inclusive.
    start: date
    #: Last US session affected, inclusive. ``None`` while the gap is still
    #: open (the fix has not shipped).
    end: date | None
    cause: str
    reference: str

    def overlaps(self, start: date, end: date) -> bool:
        return self.start <= end and (self.end is None or self.end >= start)

    def period(self) -> str:
        return f"{self.start.isoformat()}..{self.end.isoformat() if self.end else 'open'}"

    def describe(self) -> str:
        return (
            f"{self.sleeve} {self.kind.upper()} {self.period()} "
            f"({self.cache}: {self.cause} — {self.reference})"
        )


@dataclass(frozen=True)
class BacktestCoverage:
    """What history a cache could ever serve a backtest. Not a gap in the
    live record — a fact about every backtest that read the cache."""

    sleeve: str
    cache: str
    #: First session on which the lookup could return anything at all.
    first_available: date
    #: First session on which every ticker in the cache had data.
    fully_covered_from: date
    last_data: date
    summary: str


DECISION = """\
KAN-109, decided 2026-10-08: quality_value and earnings_drift live and shadow
evidence is data-degraded from the date each cache went stale, and empty from
US session 2026-09-23 (the first run from the deploy clone, SGT 2026-09-24)
until fresh caches ship (KAN-110). Recorded, not rewritten: no equity
snapshot, divergence verdict or backtest artifact is changed.

quality_value (fundamentals.json, fetched 2026-03-26): STALE from 2026-05-15,
when the 2026-03-31 quarter became available under the 45-day filing lag and
the cache never had it; EMPTY from 2026-09-23.

earnings_drift (earnings.json, fetched 2026-03-26): STALE from 2026-04-10, the
first announcement the cache held with no reported EPS; EMPTY from 2026-09-23.
From 2026-04-10 to 2026-06-19 those NaN surprises passed the 5% threshold
(nan < 5.0 is false) and became entries in every backtest run on the cache —
36 earnings_drift trades in the pinned baseline backtest_multi_20260915_102125
entered 2026-04-13..2026-06-22 on a NaN surprise. Live paper trading began
after the last one, so no live row carries one.

Backtests read the same caches and could only ever see their coverage:
fundamentals from 2024-08-14 (report periods 2024-06-30..2026-02-28, 97
tickers, 5-7 quarters each), earnings from 2020 (all 98 tickers from
2020-06-25). Every 10-year evaluation of these two sleeves — including KAN-33's
D10 sleeve evaluation and the pinned baseline — was computed with
quality_value unable to trade before 2024-08-14 and earnings_drift before
2020. Those evaluations are not changed; they are on record as partial.
"""


#: Ascending by (sleeve, start). Dates are US sessions.
KNOWN_DATA_GAPS: tuple[DataGap, ...] = (
    DataGap(
        sleeve="earnings_drift",
        cache="earnings",
        kind=STALE,
        start=date(2026, 4, 10),
        end=date(2026, 9, 22),
        cause=(
            "cache fetched once on 2026-03-26; last reported EPS 2026-03-19; "
            "every later announcement present only as a scheduled date with a "
            "NaN surprise, which the signal function entered on until "
            "2026-06-19 and which carried no event at all after it"
        ),
        reference="KAN-109",
    ),
    DataGap(
        sleeve="earnings_drift",
        cache="earnings",
        kind=EMPTY,
        start=date(2026, 9, 23),
        end=None,
        cause=(
            "gitignored cache never reached the deploy clone (KAN-72); "
            "loaded as {} with no message"
        ),
        reference="KAN-109",
    ),
    DataGap(
        sleeve="quality_value",
        cache="fundamentals",
        kind=STALE,
        start=date(2026, 5, 15),
        end=date(2026, 9, 22),
        cause=(
            "cache fetched once on 2026-03-26, newest period 2026-02-28 (82 "
            "of 97 tickers at 2025-12-31); the 2026-03-31 quarter was "
            "available from 2026-05-15 and never fetched"
        ),
        reference="KAN-109",
    ),
    DataGap(
        sleeve="quality_value",
        cache="fundamentals",
        kind=EMPTY,
        start=date(2026, 9, 23),
        end=None,
        cause=(
            "gitignored cache never reached the deploy clone (KAN-72); "
            "loaded as {} with no message"
        ),
        reference="KAN-109",
    ),
)


#: What the 2026-03-26 caches could serve any backtest that read them.
BACKTEST_COVERAGE: tuple[BacktestCoverage, ...] = (
    BacktestCoverage(
        sleeve="quality_value",
        cache="fundamentals",
        # 2024-06-30 + the 45-day filing lag.
        first_available=date(2024, 8, 14),
        # The latest first period, 2025-01-31, + 45 days.
        fully_covered_from=date(2025, 3, 17),
        last_data=date(2026, 2, 28),
        summary=(
            "97 tickers, report periods 2024-06-30..2026-02-28, 5-7 quarters "
            "each: no fundamentals at all before 2024-08-14"
        ),
    ),
    BacktestCoverage(
        sleeve="earnings_drift",
        cache="earnings",
        # One ticker carries 9 sparse events 2007-10-17..2009; the other 97
        # start 2020-01-30..2020-06-25.
        first_available=date(2020, 1, 30),
        fully_covered_from=date(2020, 6, 25),
        last_data=date(2026, 3, 19),
        summary=(
            "98 tickers, reported announcements 2020-01-30..2026-03-19 (one "
            "ticker also has 9 events 2007-2009): no events before 2020"
        ),
    ),
)


def data_gaps_in(
    start: date, end: date, *, sleeves: list[str] | set[str] | None = None
) -> list[DataGap]:
    """Registered gaps overlapping ``[start, end]``, optionally for some sleeves."""
    return [
        gap
        for gap in KNOWN_DATA_GAPS
        if gap.overlaps(start, end) and (sleeves is None or gap.sleeve in sleeves)
    ]


def describe_gaps(gaps: list[DataGap]) -> str:
    """One line for a reader: which sleeves, which periods, and where it is
    decided."""
    return (
        "data gap on record — "
        + "; ".join(gap.describe() for gap in gaps)
        + ". Verdicts in these periods are not strategy evidence "
        "(shared/data_gaps.py)."
    )
