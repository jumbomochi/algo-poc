"""Which US session an equity snapshot valued (KAN-103).

``equity_snapshots.date`` is the SGT wall-clock date of the run that wrote the
row. The last daily bar IB returns after the close is the US session that just
closed, so the row dated Tuesday holds Monday's close. Readers that treat
``date`` as a session grade live one session off its shadow
(docs/decisions/divergence-session-dating-2026-10.md).

``session_date`` records the session as a fact at write time. This module is
the one place the rule lives, used both by the stamp (``run_paper.py``, from
the bars it actually priced) and by the historical backfill
(``scripts/ops/backfill_snapshot_sessions.py``, from what the row recorded).

It does not define its own clock. "Which session do these bars represent" is
:func:`shared.session_close.newest_bar_session`, and "had that session closed"
is :func:`shared.session_close.unclosed_session` (the real close, early-close
aware, plus ``CLOSE_SETTLE``), both from KAN-104's close guard, over
:class:`shared.market_calendar.MarketCalendar`. A stamp and the guard that
refuses graded runs therefore cannot disagree about what "closed" means; since
that guard refuses any graded run whose bars are unclosed, a NULL stamp now
appears only on a tagged (drill) run, which the guard deliberately lets
through.

Pure apart from the calendar and a log line. Every datetime is compared in
UTC; a naive datetime is taken to be UTC, which is how sqlite hands back a
``DateTime(timezone=True)`` column (``session_close`` itself refuses naive
instants, so they are made aware here first).
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from typing import Any

from shared.logging import get_logger
from shared.market_calendar import ET, MarketCalendar
from shared.session_close import unclosed_session

logger = get_logger("session_dating")

#: How far back to search for a closed session. The longest NYSE closure in
#: the modern calendar is four sessions (2001-09-11..14); two weeks is ample.
_LOOKBACK = timedelta(days=14)


@lru_cache(maxsize=1)
def _default_calendar() -> MarketCalendar:
    return MarketCalendar()


def _cal(calendar: MarketCalendar | None) -> MarketCalendar:
    return calendar if calendar is not None else _default_calendar()


def as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def is_closed(
    session: date, at: datetime, *, calendar: MarketCalendar | None = None
) -> bool:
    """Had ``session`` closed (plus the settle margin) by ``at``?"""
    return unclosed_session(session, as_utc(at), calendar=_cal(calendar)) is None


def last_closed_session(
    at: datetime, *, calendar: MarketCalendar | None = None
) -> date | None:
    """The last NYSE session that had closed by ``at``."""
    cal = _cal(calendar)
    at = as_utc(at)
    day = at.astimezone(ET).date()
    for session in reversed(cal.trading_sessions(day - _LOOKBACK, day)):
        if is_closed(session, at, calendar=cal):
            return session
    return None


def last_session_before(
    day: date, *, calendar: MarketCalendar | None = None
) -> date | None:
    """The last NYSE session strictly before ``day``."""
    sessions = _cal(calendar).trading_sessions(
        day - _LOOKBACK, day - timedelta(days=1)
    )
    return sessions[-1] if sessions else None


def stamp_session(
    bar_session: date | None,
    valuation_at: datetime,
    *,
    calendar: MarketCalendar | None = None,
) -> date | None:
    """The session a snapshot marked from ``bar_session``'s bars valued.

    ``bar_session`` is :func:`shared.session_close.newest_bar_session` of the
    bars priced. Returns it when that session had closed by ``valuation_at``.
    Returns None when it had not: the bar is in progress, the marks are a
    partial session, and the row must be ungradeable rather than guessed.

    The bar date is the truth and ``valuation_at`` only the cross-check. If
    IB's historical farm had not published the last session yet, the newest bar
    is the session before it: the book really was marked a session older, so
    the stamp says so and the mismatch is logged.

    Never raises: a stamp that cannot be made is NULL plus a log line, never a
    failed paper run.
    """
    if bar_session is None:
        return None
    cal = _cal(calendar)
    try:
        at = as_utc(valuation_at)
        if not cal.is_trading_day(bar_session):
            logger.warning(
                "session_stamp_not_a_session",
                bar_session=bar_session.isoformat(),
            )
            return None
        unclosed = unclosed_session(bar_session, at, calendar=cal)
        if unclosed is not None:
            logger.warning(
                "session_stamp_partial_bar",
                bar_session=bar_session.isoformat(),
                valuation_at=at.isoformat(),
                detail=unclosed.describe() + " session_date left NULL.",
            )
            return None
        latest = last_closed_session(at, calendar=cal)
    except Exception as exc:  # calendar bounds, malformed input
        logger.warning(
            "session_stamp_failed",
            bar_session=str(bar_session),
            error=f"{type(exc).__name__}: {exc}",
        )
        return None
    if latest is not None and bar_session < latest:
        logger.warning(
            "session_stamp_stale_bars",
            bar_session=bar_session.isoformat(),
            last_closed_session=latest.isoformat(),
            valuation_at=at.isoformat(),
            detail="marks are older than the last closed session; stamped "
            "with the bar's session",
        )
    return bar_session


def infer_session(
    run_date: date,
    valuation_at: datetime | None,
    *,
    calendar: MarketCalendar | None = None,
) -> date | None:
    """The backfill rule for a row whose bars were never recorded.

    The last session closed by ``valuation_at``; for rows written before
    ``valuation_at`` existed (KAN-44), the last session strictly before the SGT
    run date. The two rules agree on every recorded row that carries both
    (decision doc, "Quantification").
    """
    if valuation_at is not None:
        return last_closed_session(valuation_at, calendar=calendar)
    return last_session_before(run_date, calendar=calendar)


def equity_by_session(rows: Iterable[Mapping[str, Any]]) -> dict[date, float]:
    """Collapse snapshot rows to one live value per US session valued.

    ``rows`` are ``PaperTradingState.get_equity_history`` dicts. Rows with no
    ``session_date`` are skipped: an unstamped row (a partial bar, or history
    the backfill has not reached) is ungradeable, never guessed. Where two run
    dates valued one session — the Tuesday after a US Monday holiday, a weekend
    catch-up — the later run wins; it is the newer valuation of the same
    closes, and in every recorded case the values are identical.
    """
    latest: dict[date, tuple[date, float]] = {}
    for row in rows:
        raw = row.get("session_date")
        if raw is None:
            continue
        session = raw if isinstance(raw, date) else date.fromisoformat(str(raw))
        run = row["date"]
        run = run if isinstance(run, date) else date.fromisoformat(str(run))
        held = latest.get(session)
        if held is None or run >= held[0]:
            latest[session] = (run, float(row["equity"]))
    return {session: value for session, (_, value) in sorted(latest.items())}


def unstamped_run_dates(rows: Iterable[Mapping[str, Any]]) -> list[date]:
    """Run dates of rows :func:`equity_by_session` skips, ascending."""
    return sorted(
        row["date"] if isinstance(row["date"], date)
        else date.fromisoformat(str(row["date"]))
        for row in rows
        if row.get("session_date") is None
    )
