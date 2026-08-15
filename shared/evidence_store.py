"""Read-side evidence arithmetic — the ONE place the ladder's rules are computed.

The evidence store (``shared/models/evidence.py``) holds observations and
nothing derived: no streak column, no "is clean" flag. Everything the capital
ladder actually decides on — breach streaks, monitor blindness, epoch progress
— is computed here, at read time, from those rows.

There is exactly one implementation on purpose. The weekly digest and any
future gate evaluator must both call these functions, because two
implementations of "10 consecutive sessions" would eventually disagree and the
disagreement would surface on gate day with real money waiting on it.

The three rules encoded here (direction doc D11/D12/D15):

1. **Only BREACH counts.** WARNING does not. The trigger is one event observed
   for 10 consecutive sessions, not a rolling average.
2. **Blind and NO_DATA pause the clock.** A missing row on an NYSE trading day
   IS the blind signal, and a recorded ``NO_DATA`` verdict is the same
   epistemic situation. Neither breaks a run nor extends it: BREACH x9, one
   blind session, BREACH is a continuing run of 10. Treating a pause as OK
   would let a dead monitor launder a persisting breach into a clean epoch.
3. **Full-window scoring (D11).** A verdict dated ``D`` covers sessions
   ``[D-29, D]``, so it scores only once ``D`` is at least the 30th trading
   session on or after the rung's start. Earlier verdicts are recorded and
   reported but do not score.

This module is strictly read-only: it never writes, never commits, and never
creates a session — callers pass one in.

Intended second consumer: ``scripts/ops/go_live_gate.py``. It has no concrete
``DataSourceProtocol`` implementation today, so there is nothing to wire yet;
whoever implements that data source must import these functions rather than
writing a second streak walk.

Currency note: ``max_drawdown_pct`` is computed on ``EquitySnapshot.equity``,
which ``PaperState.record_equity_snapshot`` documents as denominated in the
trading currency (USD) — the denomination D16's 12% bound is specified in.
The currency-qualified columns (``equity_base``, ``fx_base_per_trading``, ...)
are populated only when the writer is given an FX rate, so they cannot be
relied on as the drawdown series; ``equity`` can.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from shared.models.equity_snapshot import EquitySnapshot
from shared.models.evidence import (
    DivergenceDaily,
    DivergenceStatus,
    DrillOutcome,
    DrillType,
    EpochState,
    GateEpoch,
    GateEpochEvent,
    validate_manifest,
)
from shared.models.portfolio import Trade
from shared.universe import EXCLUDED_PORTFOLIO_PREFIX

__all__ = [
    "CRITERIA_KEYS",
    "DEFAULT_BLINDNESS_INCIDENT_SESSIONS",
    "DEFAULT_TRIGGER_SESSIONS",
    "DEFAULT_WINDOW_SESSIONS",
    "EXCLUDED_PORTFOLIO_PREFIX",
    "MAX_DRAWDOWN_PCT",
    "MAX_STREAK_LOOKBACK_SESSIONS",
    "MIN_EXPOSURE_SESSION_PCT",
    "MIN_ROUND_TRIPS",
    "Blindness",
    "BreachStreak",
    "EpochProgress",
    "blindness",
    "breach_streak",
    "current_epoch_state",
    "epoch_progress",
    "scoring_floor_for",
]

#: D16's drawdown bound, on the trading-currency (USD) NAV series. Inclusive:
#: exactly 12.00% is green, above it is red.
MAX_DRAWDOWN_PCT = 12.0
#: D12's evidence quantum. A shortfall extends an epoch, it never fails one.
MIN_ROUND_TRIPS = 15
MIN_EXPOSURE_SESSION_PCT = 60.0

#: Events that end an epoch. Once one is recorded the epoch is over; a later
#: nonterminal event does not revive it, it is reported as an anomaly.
_TERMINAL_EVENTS: dict[str, str] = {
    "clean": EpochState.CLEAN.value,
    "breached": EpochState.BREACHED.value,
    "disarmed": EpochState.DISARMED.value,
}
_NONTERMINAL_EVENTS: dict[str, str] = {
    "extended": EpochState.EXTENDED.value,
    "restarted": EpochState.RESTARTED.value,
}

DEFAULT_WINDOW_SESSIONS = 30
DEFAULT_TRIGGER_SESSIONS = 10
DEFAULT_BLINDNESS_INCIDENT_SESSIONS = 5
# Hard scan bound. Without it, a sleeve whose every session is a pause state
# would scan back to the beginning of time.
MAX_STREAK_LOOKBACK_SESSIONS = 120

# Generous enough that 30 sessions always fit; short enough to stay a bug
# detector if the calendar itself is broken.
_FLOOR_SEARCH_DAYS = 180

# Pause states: observed nothing, so the streak neither grows nor breaks.
_PAUSE_STATUSES = frozenset({DivergenceStatus.NO_DATA.value})
# States that end a run: the monitor saw the sleeve behaving.
_CLEARING_STATUSES = frozenset(
    {DivergenceStatus.OK.value, DivergenceStatus.WARNING.value}
)


@dataclass(frozen=True)
class BreachStreak:
    """One sleeve's persisting-BREACH run as of a session."""

    sleeve: str
    length: int
    started_on: date | None
    fires: bool
    paused_sessions: int
    #: Oldest session the scan reached — the session it halted at, or the
    #: oldest one examined when the lookback bound stopped it. Reported so a
    #: truncated scan is visible rather than looking like a short streak.
    scanned_to: date
    truncated: bool


@dataclass(frozen=True)
class Blindness:
    """What the monitor could and could not see over a range of sessions."""

    blind_sessions: list[date]
    no_data_sessions: list[date]
    #: Some sleeves reported and some did not. Deliberately NOT blindness: the
    #: monitor demonstrably ran, so the epoch clock is not blind. One sleeve's
    #: gap is a data-quality problem the digest surfaces separately. Counting
    #: it as blindness would pause the whole clock over one sleeve, letting a
    #: partial outage extend an epoch indefinitely.
    partial_sessions: list[date]
    longest_consecutive: int
    is_safety_incident: bool


@dataclass(frozen=True)
class EpochProgress:
    """Everything the ladder grades one epoch on, computed at read time."""

    epoch_id: int
    label: str
    rung: int
    #: Derived by :func:`current_epoch_state` — never read from a column.
    state: str
    sessions_elapsed: int
    sessions_paused: int
    round_trips: int
    exposure_session_pct: float
    max_drawdown_pct: float
    scoring_floor: date
    #: Exactly the five keys in :data:`CRITERIA_KEYS`, each "green"/"amber"/"red".
    criteria: dict[str, str]
    blocking: list[str]


#: Fixed key set so a renderer never has to discover it at runtime.
CRITERIA_KEYS = (
    "divergence",
    "drawdown",
    "safety",
    "drills",
    "evidence_quantum",
)


def _resolve_calendar(calendar: object | None) -> object:
    if calendar is not None:
        return calendar
    from shared.market_calendar import MarketCalendar

    return MarketCalendar()


def _session_on_or_before(calendar: object, day: date) -> date | None:
    """The most recent trading session on or before ``day`` — a Sunday scores Friday."""
    window = calendar.trading_sessions(day - timedelta(days=14), day)
    return window[-1] if window else None


def _as_utc(moment: datetime) -> datetime:
    """Render timestamps identically on sqlite (naive) and Postgres (aware)."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _article(word: str) -> str:
    return "an" if word[:1].lower() in "aeiou" else "a"


def _load_epoch(session: Session, epoch_id: int) -> GateEpoch:
    epoch = session.get(GateEpoch, epoch_id)
    if epoch is None:
        raise ValueError(f"no epoch with id {epoch_id}")
    return epoch


def current_epoch_state(session: Session, *, epoch_id: int) -> tuple[str, list[str]]:
    """Fold an epoch's events into its current state, plus any anomalies.

    The state is derived, never stored: ``gate_epochs`` deliberately has no
    status column, because two sources for "is this epoch clean" would
    eventually disagree.

    Ordering is ``(occurred_at, id)``. The id tie-break matters because the
    epoch writer records a whole precedence chain (D16) inside one transaction,
    so several events routinely share a timestamp.
    """
    epoch = _load_epoch(session, epoch_id)

    events = (
        session.execute(
            select(GateEpochEvent)
            .where(GateEpochEvent.epoch_id == epoch_id)
            .order_by(GateEpochEvent.occurred_at, GateEpochEvent.id)
        )
        .scalars()
        .all()
    )

    anomalies: list[str] = []
    terminal: GateEpochEvent | None = None
    for event in events:
        if event.event_type in _TERMINAL_EVENTS:
            terminal = event
        elif terminal is not None:
            anomalies.append(
                f"Epoch {epoch.label} was written to after it ended: "
                f"{_article(event.event_type)} {event.event_type!r} event at "
                f"{_as_utc(event.occurred_at).isoformat()} follows the terminal "
                f"{terminal.event_type!r} event at "
                f"{_as_utc(terminal.occurred_at).isoformat()}."
            )

    if terminal is not None:
        return _TERMINAL_EVENTS[terminal.event_type], anomalies
    if events:
        return (
            _NONTERMINAL_EVENTS.get(events[-1].event_type, EpochState.RUNNING.value),
            anomalies,
        )
    return EpochState.RUNNING.value, anomalies


def scoring_floor_for(
    rung_started_on: date,
    *,
    window_sessions: int = DEFAULT_WINDOW_SESSIONS,
    calendar: object | None = None,
) -> date:
    """The first session whose rolling window lies fully inside this rung (D11).

    A future date is a valid, un-exceptional answer: a rung that started ten
    days ago yields a floor roughly twenty trading days out, which correctly
    means nothing scores yet.
    """
    calendar = _resolve_calendar(calendar)
    sessions = calendar.trading_sessions(
        rung_started_on, rung_started_on + timedelta(days=_FLOOR_SEARCH_DAYS)
    )
    if len(sessions) < window_sessions:
        raise ValueError(
            f"calendar returned only {len(sessions)} sessions in "
            f"{_FLOOR_SEARCH_DAYS} days from {rung_started_on}; "
            f"cannot locate session {window_sessions}"
        )
    return sessions[window_sessions - 1]


def breach_streak(
    session: Session,
    *,
    sleeve: str,
    as_of: date,
    baseline_id: str,
    scoring_floor: date,
    trigger_sessions: int = DEFAULT_TRIGGER_SESSIONS,
    max_lookback: int = MAX_STREAK_LOOKBACK_SESSIONS,
    calendar: object | None = None,
) -> BreachStreak:
    """Walk back from ``as_of`` counting a persisting BREACH run for one sleeve.

    ``baseline_id`` is required, not optional: ``divergence_daily`` is unique on
    ``(sleeve, session_date, baseline_id)``, so a rebaseline mid-epoch leaves
    two rows per session with potentially opposite verdicts. Rows under any
    other baseline are invisible here — they are history, not evidence for this
    epoch.

    The scan stops at the first of: a clearing verdict, ``scoring_floor``, or
    ``max_lookback`` sessions examined. The floor wins a tie, since it is a
    correctness boundary while the lookback is only a safety bound.
    """
    calendar = _resolve_calendar(calendar)

    effective_as_of = _session_on_or_before(calendar, as_of)
    if effective_as_of is None:
        return BreachStreak(
            sleeve=sleeve,
            length=0,
            started_on=None,
            fires=False,
            paused_sessions=0,
            scanned_to=as_of,
            truncated=False,
        )

    # Fetch enough sessions to cover both stop conditions, plus a margin below
    # the floor so a floor-halt can name the session it stopped at.
    lower = min(
        scoring_floor - timedelta(days=14),
        effective_as_of - timedelta(days=max_lookback * 2 + 30),
    )
    walk = calendar.trading_sessions(lower, effective_as_of)[::-1]
    verdicts = _verdicts_by_date(
        session,
        sleeve=sleeve,
        baseline_id=baseline_id,
        start=lower,
        end=effective_as_of,
    )

    length = 0
    paused = 0
    started_on: date | None = None
    truncated = False
    scanned_to = effective_as_of
    examined = 0

    for day in walk:
        if day < scoring_floor:
            scanned_to = day
            break
        if examined >= max_lookback:
            truncated = True
            break

        examined += 1
        scanned_to = day
        status = verdicts.get(day)

        if status is None or status in _PAUSE_STATUSES:
            paused += 1
            continue
        if status in _CLEARING_STATUSES:
            break
        if status == DivergenceStatus.BREACH.value:
            length += 1
            started_on = day
            continue
        # An unknown status is not evidence of anything; treat it as clearing
        # rather than silently extending a breach run.
        break

    return BreachStreak(
        sleeve=sleeve,
        length=length,
        started_on=started_on,
        fires=length >= trigger_sessions,
        paused_sessions=paused,
        scanned_to=scanned_to,
        truncated=truncated,
    )


def blindness(
    session: Session,
    *,
    start: date,
    end: date,
    sleeves: list[str],
    baseline_id: str,
    incident_sessions: int = DEFAULT_BLINDNESS_INCIDENT_SESSIONS,
    calendar: object | None = None,
) -> Blindness:
    """Classify every trading session in ``[start, end]`` by what was observed.

    Blindness is derived from absence, never self-reported: a missing row on an
    NYSE trading day IS the blind signal, so a dead monitor cannot hide by
    staying silent.
    """
    if not sleeves:
        raise ValueError("blindness requires a non-empty sleeves list")

    calendar = _resolve_calendar(calendar)
    sessions = calendar.trading_sessions(start, end)
    expected = set(sleeves)

    rows = session.execute(
        select(
            DivergenceDaily.session_date,
            DivergenceDaily.sleeve,
            DivergenceDaily.status,
        ).where(
            DivergenceDaily.sleeve.in_(sleeves),
            DivergenceDaily.baseline_id == baseline_id,
            DivergenceDaily.session_date >= start,
            DivergenceDaily.session_date <= end,
        )
    ).all()

    reported: dict[date, dict[str, str]] = {}
    for row in rows:
        reported.setdefault(row.session_date, {})[row.sleeve] = row.status

    blind_sessions: list[date] = []
    no_data_sessions: list[date] = []
    partial_sessions: list[date] = []
    longest = 0
    run = 0

    for day in sessions:
        seen = reported.get(day, {})
        if not seen:
            blind_sessions.append(day)
            run += 1
        elif set(seen) != expected:
            partial_sessions.append(day)
            run = 0
        elif all(
            status == DivergenceStatus.NO_DATA.value for status in seen.values()
        ):
            no_data_sessions.append(day)
            run += 1
        else:
            run = 0
        longest = max(longest, run)

    return Blindness(
        blind_sessions=blind_sessions,
        no_data_sessions=no_data_sessions,
        partial_sessions=partial_sessions,
        longest_consecutive=longest,
        # Strictly greater: the rule is "blindness exceeding 5 consecutive
        # sessions", so 5 is not an incident and 6 is.
        is_safety_incident=longest > incident_sessions,
    )


def _verdicts_by_date(
    session: Session,
    *,
    sleeve: str,
    baseline_id: str,
    start: date,
    end: date,
) -> dict[date, str]:
    rows = session.execute(
        select(DivergenceDaily.session_date, DivergenceDaily.status).where(
            DivergenceDaily.sleeve == sleeve,
            DivergenceDaily.baseline_id == baseline_id,
            DivergenceDaily.session_date >= start,
            DivergenceDaily.session_date <= end,
        )
    ).all()
    return {row.session_date: row.status for row in rows}


def _day_bounds(start: date, end: date) -> tuple[datetime, datetime]:
    """UTC half-open [start 00:00, end+1 00:00) — the repo reads exit dates as
    ``executed_at.date()`` (scripts/paper_state.py:556), so the day boundary is
    UTC midnight."""
    return (
        datetime.combine(start, time.min, tzinfo=timezone.utc),
        datetime.combine(end + timedelta(days=1), time.min, tzinfo=timezone.utc),
    )


def _round_trips(
    session: Session, *, start: date, end: date, excluded_prefix: str
) -> int:
    """Completed round trips in the window.

    A ``trades`` row is written only on the sell leg
    (scripts/paper_state.py:277), so one row is one completed round trip. The
    table has no ``exit_date`` column; ``executed_at`` IS the exit timestamp,
    which is how every other reader in the repo treats it.
    """
    lower, upper = _day_bounds(start, end)
    return int(
        session.execute(
            select(func.count())
            .select_from(Trade)
            .where(
                Trade.executed_at >= lower,
                Trade.executed_at < upper,
                ~Trade.portfolio.startswith(excluded_prefix, autoescape=True),
            )
        ).scalar_one()
    )


def _equity_series(
    session: Session, *, start: date, end: date, excluded_prefix: str
) -> list[tuple[date, float, float]]:
    """Per-date ``(date, summed equity, summed market value)``, ascending.

    Summed across sleeves and excluding synthetic portfolios — the same shape
    ``shared/position_loader.py`` already uses for ``peak_nav``, so the two
    readers cannot disagree about what the account was worth.
    """
    rows = session.execute(
        select(
            EquitySnapshot.date,
            func.sum(EquitySnapshot.equity),
            func.sum(EquitySnapshot.market_value),
        )
        .where(
            EquitySnapshot.date >= start,
            EquitySnapshot.date <= end,
            ~EquitySnapshot.portfolio.startswith(excluded_prefix, autoescape=True),
        )
        .group_by(EquitySnapshot.date)
        .order_by(EquitySnapshot.date)
    ).all()
    return [(row[0], float(row[1] or 0.0), float(row[2] or 0.0)) for row in rows]


def _max_drawdown_pct(series: list[tuple[date, float]]) -> float:
    peak = 0.0
    worst = 0.0
    for _, value in series:
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak * 100.0)
    return worst


def _passing_drill_types(session: Session, *, epoch_id: int) -> set[str]:
    rows = session.execute(
        select(DrillOutcome.drill_type).where(
            DrillOutcome.epoch_id == epoch_id,
            DrillOutcome.passed.is_(True),
        )
    ).all()
    return {row[0] for row in rows}


def _safety_incident_events(session: Session, *, epoch_id: int) -> list[datetime]:
    rows = session.execute(
        select(GateEpochEvent.occurred_at)
        .where(
            GateEpochEvent.epoch_id == epoch_id,
            GateEpochEvent.event_type == "safety_incident",
        )
        .order_by(GateEpochEvent.occurred_at)
    ).all()
    return [row[0] for row in rows]


def epoch_progress(
    session: Session,
    *,
    epoch_id: int,
    as_of: date,
    excluded_prefix: str = EXCLUDED_PORTFOLIO_PREFIX,
    calendar: object | None = None,
) -> EpochProgress:
    """Score one epoch against the ladder's five criteria.

    The sleeve set, the baseline, and the divergence window are read from the
    epoch's own manifest rather than taken as arguments: passing them
    separately would let a caller score an epoch against pins it was never run
    under. The manifest is re-validated on load, so a hand-edited one fails
    loudly here instead of producing a silently wrong score.

    ``divergence``, ``drawdown`` and ``safety`` are never amber; ``drills`` and
    ``evidence_quantum`` are never red. That asymmetry is D12 made structural:
    a shortfall extends an epoch, a breach fails it.
    """
    calendar = _resolve_calendar(calendar)
    epoch = _load_epoch(session, epoch_id)
    validate_manifest(epoch.manifest)

    manifest = epoch.manifest
    sleeves = list(manifest["sleeves"])
    baseline_id = manifest["baseline_id"]
    window_sessions = manifest["divergence"]["window_sessions"]

    start = epoch.started_at.date()
    blocking: list[str] = []

    if as_of < start:
        raise ValueError(
            f"as_of {as_of} is before the epoch start {start}; "
            "an epoch cannot be scored before it began"
        )

    today = date.today()
    if as_of > today:
        blocking.append(f"as_of {as_of} is in the future; it was clamped to {today}.")
        as_of = today

    effective_as_of = _session_on_or_before(calendar, as_of) or as_of
    sessions = (
        calendar.trading_sessions(start, effective_as_of)
        if effective_as_of >= start
        else []
    )

    # ---- blindness / the paused clock ------------------------------------
    if sessions:
        blind = blindness(
            session,
            start=start,
            end=effective_as_of,
            sleeves=sleeves,
            baseline_id=baseline_id,
            calendar=calendar,
        )
    else:
        blind = Blindness([], [], [], 0, False)

    sessions_paused = len(blind.blind_sessions) + len(blind.no_data_sessions)
    sessions_elapsed = len(sessions) - sessions_paused

    # ---- divergence (D11: full-window scoring only) ------------------------
    scoring_floor = scoring_floor_for(
        start, window_sessions=window_sessions, calendar=calendar
    )
    divergence = "green"
    if effective_as_of < scoring_floor:
        blocking.append(
            f"Divergence scoring is not yet complete: the window opens on "
            f"{scoring_floor}, and the epoch has reached {effective_as_of}."
        )
    else:
        for sleeve in sleeves:
            streak = breach_streak(
                session,
                sleeve=sleeve,
                as_of=effective_as_of,
                baseline_id=baseline_id,
                scoring_floor=scoring_floor,
                calendar=calendar,
            )
            if streak.fires:
                divergence = "red"
                blocking.append(
                    f"Divergence is red: sleeve {sleeve!r} has held BREACH for "
                    f"{streak.length} consecutive sessions, at or above the "
                    f"{DEFAULT_TRIGGER_SESSIONS}-session trigger."
                )

    # ---- the three portfolio-scoped aggregates ----------------------------
    round_trips = _round_trips(
        session, start=start, end=effective_as_of, excluded_prefix=excluded_prefix
    )

    equity_rows = (
        _equity_series(
            session,
            start=start,
            end=effective_as_of,
            excluded_prefix=excluded_prefix,
        )
        if sessions
        else []
    )

    # Measured over the sessions that were actually observed. The denominator
    # excludes paused sessions, so the numerator must too — counting exposure
    # on a blind session against a smaller denominator reports more than 100%.
    scored_sessions = (
        set(sessions) - set(blind.blind_sessions) - set(blind.no_data_sessions)
    )
    exposed = sum(
        1
        for day, _, market_value in equity_rows
        if market_value > 0 and day in scored_sessions
    )
    exposure_session_pct = (
        exposed / sessions_elapsed * 100.0 if sessions_elapsed > 0 else 0.0
    )

    # A zero or negative NAV is a data fault, not a 100% drawdown.
    faulted = [day for day, equity, _ in equity_rows if equity <= 0]
    if faulted:
        blocking.append(
            "Equity snapshots summing to zero or less were excluded from the "
            "drawdown series on "
            + ", ".join(str(day) for day in faulted)
            + "."
        )
    max_drawdown = _max_drawdown_pct(
        [(day, equity) for day, equity, _ in equity_rows if equity > 0]
    )
    if sessions and not equity_rows:
        blocking.append(
            "No equity snapshots exist in the epoch window, so maximum "
            "drawdown is reported as 0.00% rather than measured."
        )
    if not sessions or sessions_elapsed <= 0:
        blocking.append(
            "The epoch has no elapsed sessions yet, so exposure and drawdown "
            "are not yet measurable."
        )

    drawdown = "red" if max_drawdown > MAX_DRAWDOWN_PCT else "green"
    if drawdown == "red":
        blocking.append(
            f"Drawdown is red: the epoch's maximum drawdown measured "
            f"{max_drawdown:.2f}%, above the {MAX_DRAWDOWN_PCT:.2f}% bound."
        )

    # ---- safety -----------------------------------------------------------
    safety = "green"
    for occurred_at in _safety_incident_events(session, epoch_id=epoch_id):
        safety = "red"
        blocking.append(
            "Safety is red: a safety_incident event was recorded at "
            f"{_as_utc(occurred_at).isoformat()}."
        )
    if blind.is_safety_incident:
        safety = "red"
        blocking.append(
            f"Safety is red: the monitor was blind for "
            f"{blind.longest_consecutive} consecutive sessions, above the "
            f"{DEFAULT_BLINDNESS_INCIDENT_SESSIONS} allowed."
        )

    # ---- drills -----------------------------------------------------------
    passed = _passing_drill_types(session, epoch_id=epoch_id)
    missing = sorted(drill.value for drill in DrillType if drill.value not in passed)
    drills = "green" if not missing else "amber"
    if missing:
        blocking.append(
            "Drills are amber: no passing run is recorded for drill type(s) "
            + ", ".join(repr(name) for name in missing)
            + "."
        )

    # ---- evidence quantum (D12: amber, never red) -------------------------
    quantum = (
        "green"
        if round_trips >= MIN_ROUND_TRIPS
        and exposure_session_pct >= MIN_EXPOSURE_SESSION_PCT
        else "amber"
    )
    if quantum == "amber":
        blocking.append(
            f"Evidence quantum is amber: {round_trips} round trips "
            f"(need {MIN_ROUND_TRIPS}) and {exposure_session_pct:.1f}% of "
            f"elapsed sessions held exposure "
            f"(need {MIN_EXPOSURE_SESSION_PCT:.1f}%)."
        )

    state, anomalies = current_epoch_state(session, epoch_id=epoch_id)
    blocking.extend(anomalies)

    return EpochProgress(
        epoch_id=epoch.id,
        label=epoch.label,
        rung=epoch.rung,
        state=state,
        sessions_elapsed=max(sessions_elapsed, 0),
        sessions_paused=sessions_paused,
        round_trips=round_trips,
        exposure_session_pct=exposure_session_pct,
        max_drawdown_pct=max_drawdown,
        scoring_floor=scoring_floor,
        criteria={
            "divergence": divergence,
            "drawdown": drawdown,
            "safety": safety,
            "drills": drills,
            "evidence_quantum": quantum,
        },
        blocking=blocking,
    )
