"""Which US session an equity snapshot valued (KAN-103).

``equity_snapshots.date`` is the SGT wall-clock date of the run that wrote the
row. At 04:15 SGT the last daily bar IB returns is the US session that closed
at 04:00 SGT, so the row dated Tuesday holds Monday's close. Readers that treat
``date`` as a session grade live one session off its shadow
(docs/decisions/divergence-session-dating-2026-10.md).

``session_date`` records the session as a fact at write time. This module is
the one place the rule lives, used both by the stamp (``run_paper.py``, from
the bars it actually priced) and by the historical backfill
(``scripts/ops/backfill_snapshot_sessions.py``, from what the row recorded).

Pure apart from the calendar and a log line. Every datetime is compared in
UTC; a naive datetime is taken to be UTC, which is how sqlite hands back a
``DateTime(timezone=True)`` column.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

from shared.logging import get_logger

ET = ZoneInfo("America/New_York")

logger = get_logger("session_dating")

#: How far back to search for a closed session. The longest NYSE closure in
#: the modern calendar is four sessions (2001-09-11..14); two weeks is ample.
_LOOKBACK = timedelta(days=14)


@lru_cache(maxsize=1)
def _default_calendar() -> Any:
    import exchange_calendars as xcals

    return xcals.get_calendar("XNYS")


def _cal(calendar: Any | None) -> Any:
    return calendar if calendar is not None else _default_calendar()


def as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _ts(day: date) -> Any:
    import pandas as pd

    return pd.Timestamp(day)


def session_close(session: date, *, calendar: Any | None = None) -> datetime:
    """The UTC instant ``session`` closed. Early-close (half-day) aware."""
    close = _cal(calendar).session_close(_ts(session))
    return close.to_pydatetime().astimezone(timezone.utc)


def _sessions(start: date, end: date, calendar: Any | None) -> list[date]:
    return [
        ts.date() for ts in _cal(calendar).sessions_in_range(_ts(start), _ts(end))
    ]


def last_closed_session(
    at: datetime, *, calendar: Any | None = None
) -> date | None:
    """The last NYSE session whose close is at or before ``at``."""
    at = as_utc(at)
    day = at.astimezone(ET).date()
    for session in reversed(_sessions(day - _LOOKBACK, day, calendar)):
        if session_close(session, calendar=calendar) <= at:
            return session
    return None


def last_session_before(day: date, *, calendar: Any | None = None) -> date | None:
    """The last NYSE session strictly before ``day``."""
    sessions = _sessions(day - _LOOKBACK, day - timedelta(days=1), calendar)
    return sessions[-1] if sessions else None


def session_in_progress(
    at: datetime, *, calendar: Any | None = None
) -> date | None:
    """The session that was trading at ``at``, or None outside RTH."""
    at = as_utc(at)
    day = at.astimezone(ET).date()
    cal = _cal(calendar)
    if not cal.is_session(_ts(day)):
        return None
    opened = cal.session_open(_ts(day)).to_pydatetime().astimezone(timezone.utc)
    if opened <= at < session_close(day, calendar=calendar):
        return day
    return None


def stamp_session(
    bar_session: date | None,
    valuation_at: datetime,
    *,
    calendar: Any | None = None,
) -> date | None:
    """The session a snapshot marked from ``bar_session``'s bars valued.

    Returns ``bar_session`` when that session had closed by ``valuation_at``.
    Returns None when it had not: the bar is in progress, the marks are a
    partial session, and the row must be ungradeable rather than guessed
    (from 2026-11-03 the 04:15 SGT run falls before the 05:00 SGT close).

    The bar date is the truth and ``valuation_at`` only the cross-check. If
    IB's historical farm had not published the last session yet, ``bars[-1]``
    is the session before it: the book really was marked a session older, so
    the stamp says so and the mismatch is logged.

    Never raises: a stamp that cannot be made is NULL plus a log line, never a
    failed paper run.
    """
    if bar_session is None:
        return None
    try:
        at = as_utc(valuation_at)
        if not _cal(calendar).is_session(_ts(bar_session)):
            logger.warning(
                "session_stamp_not_a_session",
                bar_session=bar_session.isoformat(),
            )
            return None
        if session_close(bar_session, calendar=calendar) > at:
            logger.warning(
                "session_stamp_partial_bar",
                bar_session=bar_session.isoformat(),
                valuation_at=at.isoformat(),
                detail="the bar's session had not closed at valuation; "
                "session_date left NULL",
            )
            return None
        latest = last_closed_session(at, calendar=calendar)
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
    calendar: Any | None = None,
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
