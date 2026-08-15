"""Read-side evidence arithmetic — the ONE place the ladder's rules are computed.

Every test runs against a fake calendar so the suite needs neither the network
nor ``exchange_calendars``; one test at the bottom runs the real
:class:`~shared.market_calendar.MarketCalendar` to prove the injected interface
is the real one.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from shared.evidence_store import (
    DEFAULT_TRIGGER_SESSIONS,
    MAX_STREAK_LOOKBACK_SESSIONS,
    blindness,
    breach_streak,
    current_epoch_state,
    epoch_progress,
    scoring_floor_for,
)
from shared.market_calendar import MarketCalendar
from shared.models.base import Base
from shared.models.equity_snapshot import EquitySnapshot
from shared.models.evidence import (
    DivergenceDaily,
    DivergenceStatus,
    DrillOutcome,
    EpochState,
    GateEpoch,
    GateEpochEvent,
)
from shared.models.portfolio import Trade
from shared.universe import DRILL_PORTFOLIO


BASELINE = "backtest_multi_20260812_101500.json"
OTHER_BASELINE = "backtest_multi_20260701_090000.json"
SLEEVE = "momentum"

#: A manifest that ``validate_manifest`` accepts — the read-time helper
#: re-validates on load, so every epoch fixture needs a real one.
MANIFEST = {
    "baseline_id": BASELINE,
    "sleeves": [
        "momentum",
        "sector_rotation",
        "thematic_momentum",
        "quality_value",
        "earnings_drift",
        "tail_risk_hedge",
    ],
    "weights": {
        "momentum": 0.2308,
        "sector_rotation": 0.1538,
        "thematic_momentum": 0.1410,
        "quality_value": 0.1538,
        "earnings_drift": 0.1923,
        "tail_risk_hedge": 0.1283,
    },
    "membership_snapshot": "data/universe/sp500_membership.json",
    "membership_snapshot_sha256": "a" * 64,
    "divergence": {"window_sessions": 30, "threshold": 0.20},
    "cost_model": {
        "slippage_bps": 10.0,
        "commission_per_share": 0.005,
        "commission_minimum": 1.0,
    },
    "money_path": {
        "services/risk_management": "b" * 40,
        "services/execution": "c" * 40,
        "scripts/run_paper.py": "d" * 40,
        "shared/order_ledger.py": "e" * 40,
        "shared/liquidation.py": "f" * 40,
    },
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class FakeCalendar:
    """A calendar over an explicit session list — no exchange data, no network."""

    def __init__(self, sessions: list[date]) -> None:
        self._sessions = sorted(sessions)
        self._lookup = set(self._sessions)

    def is_trading_day(self, d: date) -> bool:
        return d in self._lookup

    def trading_sessions(self, start: date, end: date) -> list[date]:
        return [d for d in self._sessions if start <= d <= end]


def weekday_sessions(
    start: date, end: date, holidays: tuple[date, ...] = ()
) -> list[date]:
    days: list[date] = []
    day = start
    while day <= end:
        if day.weekday() < 5 and day not in holidays:
            days.append(day)
        day += timedelta(days=1)
    return days


@pytest.fixture
def cal() -> FakeCalendar:
    # Deliberately wide: the streak scan walks back up to 120 sessions.
    return FakeCalendar(weekday_sessions(date(2024, 1, 1), date(2027, 12, 31)))


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    yield db
    db.close()
    engine.dispose()


def _now() -> datetime:
    return datetime(2026, 8, 15, 4, 45, tzinfo=timezone.utc)


def add_verdict(
    db: Session,
    *,
    session_date: date,
    status: str,
    sleeve: str = SLEEVE,
    baseline_id: str = BASELINE,
) -> None:
    db.add(
        DivergenceDaily(
            sleeve=sleeve,
            session_date=session_date,
            status=status,
            baseline_id=baseline_id,
            window_sessions=30,
            threshold=0.20,
            metric_value=0.1,
            created_at=_now(),
        )
    )
    db.flush()


def seed_tail(
    db: Session,
    sessions: list[date],
    statuses: list[str | None],
    *,
    sleeve: str = SLEEVE,
    baseline_id: str = BASELINE,
) -> None:
    """Write ``statuses`` onto the LAST len(statuses) sessions, oldest first.

    ``None`` writes no row at all — which is exactly how a blind session is
    represented, since absence IS the signal (D15).
    """
    window = sessions[-len(statuses) :]
    for day, status in zip(window, statuses):
        if status is None:
            continue
        add_verdict(
            db,
            session_date=day,
            status=status,
            sleeve=sleeve,
            baseline_id=baseline_id,
        )


AS_OF = date(2026, 8, 14)  # a Friday


@pytest.fixture
def sessions(cal: FakeCalendar) -> list[date]:
    return cal.trading_sessions(date(2026, 1, 1), AS_OF)


def _streak(db, cal, *, floor_offset: int = 20, **kwargs):
    """Run the streak with a floor ``floor_offset`` sessions back from as_of."""
    all_sessions = cal.trading_sessions(date(2026, 1, 1), AS_OF)
    defaults = dict(
        sleeve=SLEEVE,
        as_of=AS_OF,
        baseline_id=BASELINE,
        scoring_floor=all_sessions[-floor_offset],
        calendar=cal,
    )
    defaults.update(kwargs)
    return breach_streak(db, **defaults)


# ---------------------------------------------------------------------------
# scoring_floor_for
# ---------------------------------------------------------------------------


def test_scoring_floor_is_the_thirtieth_session_on_or_after_the_rung_start(cal):
    # 2026-06-01 is a Monday; the fake calendar has Mon-Fri sessions, so the
    # 30th session is 29 weekdays later.
    floor = scoring_floor_for(date(2026, 6, 1), calendar=cal)

    assert floor == cal.trading_sessions(date(2026, 6, 1), date(2026, 12, 31))[29]
    assert floor == date(2026, 7, 10)


def test_scoring_floor_for_a_recent_rung_start_is_in_the_future(cal):
    started = date(2026, 8, 3)

    floor = scoring_floor_for(started, calendar=cal)

    # Nothing scores yet — the clock runs, scoring waits (D11).
    assert floor > AS_OF


def test_scoring_floor_honours_a_non_default_window(cal):
    floor = scoring_floor_for(date(2026, 6, 1), window_sessions=5, calendar=cal)

    assert floor == date(2026, 6, 5)


# ---------------------------------------------------------------------------
# breach_streak
# ---------------------------------------------------------------------------


def test_ten_consecutive_breaches_fire(session, cal, sessions):
    seed_tail(session, sessions, [DivergenceStatus.BREACH] * 10)

    streak = _streak(session, cal)

    assert streak.length == 10
    assert streak.fires is True
    assert streak.started_on == sessions[-10]
    assert streak.sleeve == SLEEVE


def test_nine_consecutive_breaches_do_not_fire(session, cal, sessions):
    seed_tail(session, sessions, [DivergenceStatus.BREACH] * 9)

    streak = _streak(session, cal)

    assert streak.length == 9
    assert streak.fires is False


def test_an_ok_verdict_between_two_nine_session_runs_breaks_the_streak(
    session, cal, sessions
):
    seed_tail(
        session,
        sessions,
        [DivergenceStatus.BREACH] * 9
        + [DivergenceStatus.OK]
        + [DivergenceStatus.BREACH] * 9,
    )

    streak = _streak(session, cal)

    assert streak.length == 9
    assert streak.fires is False


def test_a_warning_verdict_breaks_a_run_exactly_as_ok_does(session, cal, sessions):
    seed_tail(
        session,
        sessions,
        [DivergenceStatus.BREACH] * 9
        + [DivergenceStatus.WARNING]
        + [DivergenceStatus.BREACH] * 9,
    )

    streak = _streak(session, cal)

    assert streak.length == 9
    assert streak.fires is False


def test_a_blind_session_bridges_a_breach_run_rather_than_breaking_it(
    session, cal, sessions
):
    seed_tail(
        session,
        sessions,
        [DivergenceStatus.BREACH] * 9 + [None] + [DivergenceStatus.BREACH],
    )

    # Floor at the oldest seeded session, so the only pause in scope is the
    # bridged one.
    streak = _streak(session, cal, floor_offset=11)

    assert streak.length == 10
    assert streak.fires is True
    assert streak.paused_sessions == 1


def test_a_recorded_no_data_verdict_bridges_exactly_as_a_missing_row_does(
    session, cal, sessions
):
    seed_tail(
        session,
        sessions,
        [DivergenceStatus.BREACH] * 9
        + [DivergenceStatus.NO_DATA]
        + [DivergenceStatus.BREACH],
    )

    streak = _streak(session, cal, floor_offset=11)

    assert streak.length == 10
    assert streak.fires is True
    assert streak.paused_sessions == 1


def test_the_scan_stops_at_the_scoring_floor(session, cal, sessions):
    seed_tail(session, sessions, [DivergenceStatus.BREACH] * 20)

    streak = _streak(session, cal, floor_offset=6)

    # Only the six sessions at or above the floor score.
    assert streak.length == 6
    assert streak.fires is False
    assert streak.started_on == sessions[-6]


def test_verdicts_under_another_baseline_are_invisible_to_scoring(
    session, cal, sessions
):
    seed_tail(session, sessions, [DivergenceStatus.BREACH] * 10)
    seed_tail(
        session,
        sessions,
        [DivergenceStatus.OK] * 10,
        baseline_id=OTHER_BASELINE,
    )

    under_current = _streak(session, cal)
    under_other = _streak(session, cal, baseline_id=OTHER_BASELINE)

    assert (under_current.length, under_current.fires) == (10, True)
    assert (under_other.length, under_other.fires) == (0, False)


def test_an_unbroken_run_of_pause_states_terminates_at_the_lookback_bound(
    session, cal
):
    all_sessions = cal.trading_sessions(date(2024, 1, 1), AS_OF)

    streak = breach_streak(
        session,
        sleeve=SLEEVE,
        as_of=AS_OF,
        baseline_id=BASELINE,
        scoring_floor=date(2024, 1, 1),
        calendar=cal,
    )

    assert streak.truncated is True
    assert streak.paused_sessions == MAX_STREAK_LOOKBACK_SESSIONS
    assert streak.scanned_to == all_sessions[-MAX_STREAK_LOOKBACK_SESSIONS]


def test_the_scoring_floor_takes_precedence_over_the_lookback_bound(session, cal):
    all_sessions = cal.trading_sessions(date(2024, 1, 1), AS_OF)
    floor = all_sessions[-MAX_STREAK_LOOKBACK_SESSIONS]

    streak = breach_streak(
        session,
        sleeve=SLEEVE,
        as_of=AS_OF,
        baseline_id=BASELINE,
        scoring_floor=floor,
        calendar=cal,
    )

    assert streak.truncated is False
    assert streak.scanned_to < floor


def test_a_truncated_scan_that_reaches_the_trigger_still_fires(session, cal):
    all_sessions = cal.trading_sessions(date(2024, 1, 1), AS_OF)
    seed_tail(
        session,
        all_sessions,
        [DivergenceStatus.BREACH] * MAX_STREAK_LOOKBACK_SESSIONS,
    )

    streak = breach_streak(
        session,
        sleeve=SLEEVE,
        as_of=AS_OF,
        baseline_id=BASELINE,
        scoring_floor=date(2024, 1, 1),
        calendar=cal,
    )

    assert streak.truncated is True
    assert streak.length == MAX_STREAK_LOOKBACK_SESSIONS
    assert streak.fires is True


def test_an_as_of_that_is_not_a_trading_day_scores_the_prior_session(
    session, cal, sessions
):
    seed_tail(session, sessions, [DivergenceStatus.BREACH] * DEFAULT_TRIGGER_SESSIONS)
    sunday = AS_OF + timedelta(days=2)
    assert not cal.is_trading_day(sunday)

    streak = _streak(session, cal, as_of=sunday)

    assert streak.length == DEFAULT_TRIGGER_SESSIONS
    assert streak.scanned_to <= AS_OF


# ---------------------------------------------------------------------------
# blindness
# ---------------------------------------------------------------------------


SLEEVES = [
    "momentum",
    "sector_rotation",
    "thematic_momentum",
    "quality_value",
    "earnings_drift",
    "tail_risk_hedge",
]


@pytest.fixture
def ten_sessions(cal: FakeCalendar) -> list[date]:
    """Ten consecutive sessions ending on AS_OF, spanning two weekends."""
    return cal.trading_sessions(date(2026, 1, 1), AS_OF)[-10:]


def report(db: Session, day: date, sleeves: list[str], status: str) -> None:
    for sleeve in sleeves:
        add_verdict(db, session_date=day, status=status, sleeve=sleeve)


def seed_mixed_ten(db: Session, days: list[date]) -> None:
    """3 blind, 1 all-NO_DATA, 1 partial, 5 fully-reported sessions."""
    # days[0:3] deliberately get no rows at all — absence IS the signal.
    report(db, days[3], SLEEVES, DivergenceStatus.NO_DATA)
    report(db, days[4], SLEEVES[:3], DivergenceStatus.OK)
    for day in days[5:]:
        report(db, day, SLEEVES, DivergenceStatus.OK)


def _blind(db, cal, days, **kwargs):
    defaults = dict(
        start=days[0],
        end=days[-1],
        sleeves=SLEEVES,
        baseline_id=BASELINE,
        calendar=cal,
    )
    defaults.update(kwargs)
    return blindness(db, **defaults)


def test_sessions_with_no_rows_at_all_are_blind(session, cal, ten_sessions):
    seed_mixed_ten(session, ten_sessions)

    result = _blind(session, cal, ten_sessions)

    assert result.blind_sessions == ten_sessions[0:3]


def test_a_session_where_every_sleeve_reported_no_data_is_not_blind_but_paused(
    session, cal, ten_sessions
):
    seed_mixed_ten(session, ten_sessions)

    result = _blind(session, cal, ten_sessions)

    assert result.no_data_sessions == [ten_sessions[3]]
    assert ten_sessions[3] not in result.blind_sessions


def test_a_partially_reported_session_is_its_own_category(
    session, cal, ten_sessions
):
    seed_mixed_ten(session, ten_sessions)

    result = _blind(session, cal, ten_sessions)

    assert result.partial_sessions == [ten_sessions[4]]
    # The monitor demonstrably ran, so the epoch clock is not blind: the run of
    # three blind sessions plus one NO_DATA session stops here.
    assert result.longest_consecutive == 4
    assert result.is_safety_incident is False


def test_weekends_and_holidays_never_appear_in_any_blindness_list(session):
    holiday = date(2026, 7, 3)
    cal = FakeCalendar(
        weekday_sessions(date(2026, 6, 1), date(2026, 7, 31), holidays=(holiday,))
    )

    result = _blind(session, cal, [date(2026, 6, 29), date(2026, 7, 10)])

    reported = (
        result.blind_sessions + result.no_data_sessions + result.partial_sessions
    )
    assert holiday not in reported
    assert all(day.weekday() < 5 for day in reported)
    # Every session in the range is blind — nothing was ever written.
    assert reported == cal.trading_sessions(date(2026, 6, 29), date(2026, 7, 10))


def test_five_consecutive_blind_sessions_are_not_yet_a_safety_incident(
    session, cal, ten_sessions
):
    for day in ten_sessions[5:]:
        report(session, day, SLEEVES, DivergenceStatus.OK)

    result = _blind(session, cal, ten_sessions)

    assert result.longest_consecutive == 5
    assert result.is_safety_incident is False


def test_six_consecutive_blind_sessions_are_a_safety_incident(
    session, cal, ten_sessions
):
    for day in ten_sessions[6:]:
        report(session, day, SLEEVES, DivergenceStatus.OK)

    result = _blind(session, cal, ten_sessions)

    assert result.longest_consecutive == 6
    assert result.is_safety_incident is True


def test_blindness_ignores_rows_written_under_another_baseline(
    session, cal, ten_sessions
):
    for day in ten_sessions:
        for sleeve in SLEEVES:
            add_verdict(
                session,
                session_date=day,
                status=DivergenceStatus.OK,
                sleeve=sleeve,
                baseline_id=OTHER_BASELINE,
            )

    result = _blind(session, cal, ten_sessions)

    assert result.blind_sessions == ten_sessions


def test_blindness_rejects_an_empty_sleeve_list(session, cal, ten_sessions):
    with pytest.raises(ValueError, match="sleeves"):
        _blind(session, cal, ten_sessions, sleeves=[])


# ---------------------------------------------------------------------------
# current_epoch_state — the fold
# ---------------------------------------------------------------------------


EPOCH_START = datetime(2026, 6, 1, 13, 30, tzinfo=timezone.utc)


def make_epoch(
    db: Session,
    *,
    label: str = "v2",
    rung: int = 0,
    started_at: datetime = EPOCH_START,
    manifest: dict | None = None,
) -> GateEpoch:
    epoch = GateEpoch(
        label=label,
        rung=rung,
        manifest=manifest if manifest is not None else dict(MANIFEST),
        started_at=started_at,
    )
    db.add(epoch)
    db.flush()
    return epoch


def add_events(
    db: Session,
    epoch: GateEpoch,
    kinds: list[str],
    *,
    same_timestamp: bool = False,
) -> None:
    for index, kind in enumerate(kinds):
        occurred = EPOCH_START + timedelta(days=0 if same_timestamp else index)
        db.add(
            GateEpochEvent(
                epoch_id=epoch.id,
                event_type=kind,
                occurred_at=occurred,
            )
        )
    db.flush()


@pytest.mark.parametrize(
    "kinds,expected",
    [
        ([], EpochState.RUNNING),
        (["started"], EpochState.RUNNING),
        (["started", "extended"], EpochState.EXTENDED),
        (["started", "restarted"], EpochState.RESTARTED),
        (["started", "breached"], EpochState.BREACHED),
        (["started", "clean"], EpochState.CLEAN),
        (["started", "breached", "disarmed"], EpochState.DISARMED),
        (["started", "rung_change"], EpochState.RUNNING),
    ],
)
def test_epoch_state_folds_its_events(session, kinds, expected):
    epoch = make_epoch(session)
    add_events(session, epoch, kinds)

    state, anomalies = current_epoch_state(session, epoch_id=epoch.id)

    assert state == expected
    assert anomalies == []


def test_a_terminal_event_is_final_and_a_later_event_is_reported_as_an_anomaly(
    session,
):
    epoch = make_epoch(session)
    add_events(session, epoch, ["started", "breached", "extended"])

    state, anomalies = current_epoch_state(session, epoch_id=epoch.id)

    assert state == EpochState.BREACHED
    assert anomalies == [
        "Epoch v2 was written to after it ended: an 'extended' event at "
        "2026-06-03T13:30:00+00:00 follows the terminal 'breached' event at "
        "2026-06-02T13:30:00+00:00."
    ]


def test_events_sharing_a_timestamp_fold_deterministically_by_id(session):
    epoch = make_epoch(session)
    add_events(session, epoch, ["clean", "breached"], same_timestamp=True)

    state, _ = current_epoch_state(session, epoch_id=epoch.id)

    assert state == EpochState.BREACHED


def test_the_id_tie_break_follows_insertion_order_not_the_event_name(session):
    epoch = make_epoch(session)
    add_events(session, epoch, ["breached", "clean"], same_timestamp=True)

    state, _ = current_epoch_state(session, epoch_id=epoch.id)

    assert state == EpochState.CLEAN


def test_epoch_state_ignores_another_epochs_events(session):
    epoch = make_epoch(session)
    other = make_epoch(session, label="v3")
    add_events(session, epoch, ["started"])
    add_events(session, other, ["started", "breached"])

    state, _ = current_epoch_state(session, epoch_id=epoch.id)

    assert state == EpochState.RUNNING


def test_epoch_state_rejects_an_unknown_epoch_id(session):
    with pytest.raises(ValueError, match="no epoch"):
        current_epoch_state(session, epoch_id=404)


# ---------------------------------------------------------------------------
# epoch_progress
# ---------------------------------------------------------------------------


SHORT_START = date(2026, 8, 3)  # a Monday; exactly 10 sessions through AS_OF
SCORED_START = date(2026, 6, 1)  # far enough back that the window has closed


def add_snapshot(
    db: Session,
    *,
    portfolio: str,
    day: date,
    equity: float,
    market_value: float = 0.0,
) -> None:
    db.add(
        EquitySnapshot(
            portfolio=portfolio,
            date=day,
            equity=equity,
            cash=equity - market_value,
            market_value=market_value,
            created_at=_now(),
        )
    )
    db.flush()


def add_trade(db: Session, *, portfolio: str, executed_at: datetime) -> None:
    db.add(
        Trade(
            ticker="AAPL",
            portfolio=portfolio,
            side="sell",
            quantity=10.0,
            price=101.0,
            entry_price=100.0,
            entry_date=executed_at.date() - timedelta(days=5),
            pnl=10.0,
            executed_at=executed_at,
        )
    )
    db.flush()


def add_drill(
    db: Session,
    *,
    epoch_id: int,
    drill_type: str,
    passed: bool,
    occurred_at: datetime = EPOCH_START,
) -> None:
    db.add(
        DrillOutcome(
            epoch_id=epoch_id,
            drill_type=drill_type,
            passed=passed,
            occurred_at=occurred_at,
        )
    )
    db.flush()


def seed_clean_verdicts(
    db: Session, cal: FakeCalendar, start: date, end: date
) -> None:
    for day in cal.trading_sessions(start, end):
        report(db, day, SLEEVES, DivergenceStatus.OK)


def seed_verdicts(
    db: Session,
    days: list[date],
    *,
    breach_days: list[date] | tuple[date, ...] = (),
    sleeve: str = SLEEVE,
) -> None:
    """Every sleeve reports on every session; ``sleeve`` breaches on some."""
    breaching = set(breach_days)
    for day in days:
        for name in SLEEVES:
            status = (
                DivergenceStatus.BREACH
                if name == sleeve and day in breaching
                else DivergenceStatus.OK
            )
            add_verdict(db, session_date=day, status=status, sleeve=name)


def build_epoch(
    db: Session,
    cal: FakeCalendar,
    *,
    start: date = SHORT_START,
    equities: list[float] | None = None,
    exposed_sessions: int = 6,
    trades: int = 15,
    drills: tuple[str, ...] = ("restart_halt", "synthetic_stop"),
    verdicts: bool = True,
    snapshots: bool = True,
) -> tuple[GateEpoch, list[date]]:
    """An otherwise-green epoch, so each test can perturb exactly one thing."""
    epoch = make_epoch(
        db, started_at=datetime.combine(start, time(13, 30), tzinfo=timezone.utc)
    )
    days = cal.trading_sessions(start, AS_OF)

    if verdicts:
        seed_clean_verdicts(db, cal, start, AS_OF)

    if snapshots:
        series = equities if equities is not None else [4000.0] * len(days)
        for index, day in enumerate(days):
            add_snapshot(
                db,
                portfolio="momentum",
                day=day,
                equity=series[index],
                market_value=100.0 if index < exposed_sessions else 0.0,
            )

    for index in range(trades):
        add_trade(
            db,
            portfolio="momentum",
            executed_at=datetime.combine(
                days[index % len(days)], time(20, 0), tzinfo=timezone.utc
            ),
        )

    for drill in drills:
        add_drill(db, epoch_id=epoch.id, drill_type=drill, passed=True)

    return epoch, days


def _progress(db, cal, epoch, **kwargs):
    defaults = dict(epoch_id=epoch.id, as_of=AS_OF, calendar=cal)
    defaults.update(kwargs)
    return epoch_progress(db, **defaults)


def test_a_healthy_epoch_scores_green_on_everything(session, cal):
    epoch, days = build_epoch(session, cal)

    progress = _progress(session, cal, epoch)

    assert progress.epoch_id == epoch.id
    assert progress.label == "v2"
    assert progress.rung == 0
    assert progress.state == EpochState.RUNNING
    assert progress.sessions_elapsed == 10
    assert progress.sessions_paused == 0
    assert set(progress.criteria.values()) == {"green"}


def test_criteria_always_has_exactly_the_five_documented_keys(session, cal):
    epoch, _ = build_epoch(session, cal)

    progress = _progress(session, cal, epoch)

    assert set(progress.criteria) == {
        "divergence",
        "drawdown",
        "safety",
        "drills",
        "evidence_quantum",
    }


def test_round_trips_count_sell_leg_trades_and_exclude_the_drill_sleeve(
    session, cal
):
    epoch, days = build_epoch(session, cal, trades=15)
    add_trade(
        session,
        portfolio=DRILL_PORTFOLIO,
        executed_at=datetime.combine(days[0], time(20, 0), tzinfo=timezone.utc),
    )
    # A trade that exited before the epoch opened is not this epoch's evidence.
    add_trade(
        session,
        portfolio="momentum",
        executed_at=datetime(2026, 7, 20, 20, 0, tzinfo=timezone.utc),
    )

    progress = _progress(session, cal, epoch)

    assert progress.round_trips == 15


def test_exposure_is_the_share_of_elapsed_sessions_holding_market_value(
    session, cal
):
    epoch, _ = build_epoch(session, cal, exposed_sessions=6)

    progress = _progress(session, cal, epoch)

    assert progress.exposure_session_pct == pytest.approx(60.0)


def test_max_drawdown_is_measured_on_summed_equity_across_sleeves(session, cal):
    equities = [3800.0, 4000.0, 3900.0, 3700.0, 3500.0, 3600.0, 3800.0, 3900.0,
                3950.0, 4000.0]
    epoch, days = build_epoch(session, cal, equities=equities)
    # An excluded portfolio must not move the series.
    for day in days:
        add_snapshot(session, portfolio="_aggregate", day=day, equity=9_999.0)

    progress = _progress(session, cal, epoch)

    assert progress.max_drawdown_pct == pytest.approx(12.5)


def test_drawdown_is_red_above_the_bound_and_green_at_exactly_the_bound(
    session, cal
):
    at_bound = [4000.0] + [3520.0] + [4000.0] * 8  # 12.00%
    epoch, _ = build_epoch(session, cal, equities=at_bound)

    progress = _progress(session, cal, epoch)

    assert progress.max_drawdown_pct == pytest.approx(12.0)
    assert progress.criteria["drawdown"] == "green"


def test_a_drawdown_past_the_bound_is_red_and_named_in_blocking(session, cal):
    equities = [4000.0, 3500.0] + [4000.0] * 8
    epoch, _ = build_epoch(session, cal, equities=equities)

    progress = _progress(session, cal, epoch)

    assert progress.criteria["drawdown"] == "red"
    assert (
        "Drawdown is red: the epoch's maximum drawdown measured 12.50%, "
        "above the 12.00% bound." in progress.blocking
    )


def test_sessions_elapsed_excludes_paused_sessions(session, cal):
    epoch, days = build_epoch(session, cal, verdicts=False)
    # Only the last four sessions were observed; the first six are blind.
    for day in days[6:]:
        report(session, day, SLEEVES, DivergenceStatus.OK)

    progress = _progress(session, cal, epoch)

    assert progress.sessions_paused == 6
    assert progress.sessions_elapsed == 4


def test_a_short_round_trip_count_is_amber_and_never_red(session, cal):
    epoch, _ = build_epoch(session, cal, trades=14)

    progress = _progress(session, cal, epoch)

    assert progress.round_trips == 14
    assert progress.criteria["evidence_quantum"] == "amber"
    assert "red" not in progress.criteria.values()
    assert (
        "Evidence quantum is amber: 14 round trips (need 15) and 60.0% of "
        "elapsed sessions held exposure (need 60.0%)." in progress.blocking
    )


def test_thin_exposure_is_amber_and_never_red(session, cal):
    epoch, _ = build_epoch(session, cal, exposed_sessions=5)

    progress = _progress(session, cal, epoch)

    assert progress.exposure_session_pct == pytest.approx(50.0)
    assert progress.criteria["evidence_quantum"] == "amber"


def test_a_missing_drill_type_is_amber_and_named(session, cal):
    epoch, _ = build_epoch(session, cal, drills=("restart_halt",))

    progress = _progress(session, cal, epoch)

    assert progress.criteria["drills"] == "amber"
    assert (
        "Drills are amber: no passing run is recorded for drill type(s) "
        "'synthetic_stop'." in progress.blocking
    )


def test_a_drill_that_failed_and_was_later_re_run_passes(session, cal):
    epoch, _ = build_epoch(session, cal, drills=("restart_halt",))
    add_drill(session, epoch_id=epoch.id, drill_type="synthetic_stop", passed=False)
    add_drill(session, epoch_id=epoch.id, drill_type="synthetic_stop", passed=True)

    progress = _progress(session, cal, epoch)

    assert progress.criteria["drills"] == "green"


def test_scoring_waits_until_the_window_lies_fully_inside_the_rung(session, cal):
    epoch, _ = build_epoch(session, cal)

    progress = _progress(session, cal, epoch)

    assert progress.scoring_floor == date(2026, 9, 11)
    assert progress.criteria["divergence"] == "green"
    assert (
        "Divergence scoring is not yet complete: the window opens on "
        "2026-09-11, and the epoch has reached 2026-08-14." in progress.blocking
    )


def test_a_firing_breach_streak_makes_divergence_red(session, cal):
    epoch, days = build_epoch(session, cal, start=SCORED_START, verdicts=False)
    seed_verdicts(session, days, breach_days=days[-10:])

    progress = _progress(session, cal, epoch)

    assert progress.scoring_floor == date(2026, 7, 10)
    assert progress.criteria["divergence"] == "red"
    assert (
        "Divergence is red: sleeve 'momentum' has held BREACH for 10 "
        "consecutive sessions, at or above the 10-session trigger."
        in progress.blocking
    )


def test_a_breach_run_that_predates_the_scoring_floor_does_not_fire(session, cal):
    epoch, days = build_epoch(session, cal, start=SCORED_START, verdicts=False)
    floor_index = days.index(date(2026, 7, 10))
    seed_verdicts(session, days, breach_days=days[floor_index - 12 : floor_index - 2])

    progress = _progress(session, cal, epoch)

    assert progress.criteria["divergence"] == "green"


def test_blindness_past_the_incident_bound_makes_safety_red(session, cal):
    epoch, days = build_epoch(session, cal, verdicts=False)
    for day in days[6:]:
        report(session, day, SLEEVES, DivergenceStatus.OK)

    progress = _progress(session, cal, epoch)

    assert progress.criteria["safety"] == "red"
    assert (
        "Safety is red: the monitor was blind for 6 consecutive sessions, "
        "above the 5 allowed." in progress.blocking
    )


def test_a_recorded_safety_incident_event_makes_safety_red(session, cal):
    epoch, _ = build_epoch(session, cal)
    session.add(
        GateEpochEvent(
            epoch_id=epoch.id,
            event_type="safety_incident",
            occurred_at=datetime(2026, 8, 5, 13, 30, tzinfo=timezone.utc),
        )
    )
    session.flush()

    progress = _progress(session, cal, epoch)

    assert progress.criteria["safety"] == "red"
    assert (
        "Safety is red: a safety_incident event was recorded at "
        "2026-08-05T13:30:00+00:00." in progress.blocking
    )


def test_the_epoch_state_and_its_anomalies_reach_the_caller(session, cal):
    epoch, _ = build_epoch(session, cal)
    add_events(session, epoch, ["started", "breached", "extended"])

    progress = _progress(session, cal, epoch)

    assert progress.state == EpochState.BREACHED
    assert any("was written to after it ended" in entry for entry in progress.blocking)


# ---------------------------------------------------------------------------
# Manifest-sourced pins
# ---------------------------------------------------------------------------


def test_the_sleeve_set_and_baseline_come_from_the_manifest(session, cal):
    manifest = dict(MANIFEST)
    manifest["baseline_id"] = OTHER_BASELINE
    epoch = make_epoch(
        session,
        manifest=manifest,
        started_at=datetime.combine(SHORT_START, time(13, 30), tzinfo=timezone.utc),
    )
    # Written under the epoch's own baseline: seen.
    seed_clean_verdicts(session, cal, SHORT_START, AS_OF)

    progress = _progress(session, cal, epoch)

    # The clean rows above were written under BASELINE, not OTHER_BASELINE, so
    # under this epoch's pinned baseline every session is blind.
    assert progress.sessions_paused == 10
    assert progress.criteria["safety"] == "red"


def test_the_divergence_window_comes_from_the_manifest(session, cal):
    manifest = dict(MANIFEST)
    manifest["divergence"] = {"window_sessions": 5, "threshold": 0.20}
    epoch = make_epoch(
        session,
        manifest=manifest,
        started_at=datetime.combine(SHORT_START, time(13, 30), tzinfo=timezone.utc),
    )
    seed_clean_verdicts(session, cal, SHORT_START, AS_OF)

    progress = _progress(session, cal, epoch)

    # Five sessions, not thirty: the window closed on 2026-08-07.
    assert progress.scoring_floor == date(2026, 8, 7)


def test_a_hand_edited_manifest_fails_loudly_at_read_time(session, cal):
    epoch = make_epoch(session, manifest={"sleeves": []})

    with pytest.raises(ValueError, match="manifest"):
        _progress(session, cal, epoch)


# ---------------------------------------------------------------------------
# Pinned edge cases
# ---------------------------------------------------------------------------


def test_an_epoch_that_started_today_divides_by_nothing(session, cal):
    epoch = make_epoch(
        session,
        started_at=datetime.combine(AS_OF, time(13, 30), tzinfo=timezone.utc),
    )
    report(session, AS_OF, SLEEVES, DivergenceStatus.OK)

    progress = _progress(session, cal, epoch, as_of=AS_OF)

    assert progress.sessions_elapsed == 1
    assert progress.exposure_session_pct == 0.0
    assert progress.criteria["evidence_quantum"] == "amber"


def test_an_epoch_with_no_elapsed_sessions_reports_rather_than_crashes(
    session, cal
):
    saturday = date(2026, 8, 15)
    epoch = make_epoch(
        session,
        started_at=datetime.combine(saturday, time(13, 30), tzinfo=timezone.utc),
    )

    progress = _progress(session, cal, epoch, as_of=saturday)

    assert progress.sessions_elapsed == 0
    assert progress.exposure_session_pct == 0.0
    assert (
        "The epoch has no elapsed sessions yet, so exposure and drawdown are "
        "not yet measurable." in progress.blocking
    )


def test_no_equity_rows_reports_zero_drawdown_and_says_so(session, cal):
    epoch, _ = build_epoch(session, cal, snapshots=False)

    progress = _progress(session, cal, epoch)

    assert progress.max_drawdown_pct == 0.0
    assert (
        "No equity snapshots exist in the epoch window, so maximum drawdown "
        "is reported as 0.00% rather than measured." in progress.blocking
    )


def test_a_non_positive_equity_date_is_excluded_from_the_series_and_named(
    session, cal
):
    equities = [4000.0, 0.0] + [4000.0] * 8
    epoch, _ = build_epoch(session, cal, equities=equities)

    progress = _progress(session, cal, epoch)

    # A zero NAV is a data fault, not a 100% drawdown.
    assert progress.max_drawdown_pct == 0.0
    assert (
        "Equity snapshots summing to zero or less were excluded from the "
        "drawdown series on 2026-08-04." in progress.blocking
    )


def test_a_future_as_of_is_clamped_to_today_and_said_so(session, cal):
    epoch, _ = build_epoch(session, cal)
    future = date.today() + timedelta(days=365)

    progress = _progress(session, cal, epoch, as_of=future)

    assert (
        f"as_of {future} is in the future; it was clamped to {date.today()}."
        in progress.blocking
    )


def test_an_as_of_before_the_epoch_start_is_a_caller_bug(session, cal):
    epoch, _ = build_epoch(session, cal)

    with pytest.raises(ValueError, match="before the epoch start"):
        _progress(session, cal, epoch, as_of=SHORT_START - timedelta(days=1))


def test_a_weekend_as_of_scores_the_most_recent_session(session, cal):
    epoch, _ = build_epoch(session, cal)

    sunday = _progress(session, cal, epoch, as_of=AS_OF + timedelta(days=2))
    friday = _progress(session, cal, epoch)

    assert sunday.sessions_elapsed == friday.sessions_elapsed
    assert sunday.max_drawdown_pct == friday.max_drawdown_pct


def test_epoch_progress_rejects_an_unknown_epoch_id(session, cal):
    with pytest.raises(ValueError, match="no epoch"):
        epoch_progress(session, epoch_id=404, as_of=AS_OF, calendar=cal)


# ---------------------------------------------------------------------------
# Read-only guarantee, and the real calendar
# ---------------------------------------------------------------------------


class CommitRaises(Session):
    def commit(self):  # pragma: no cover - the assertion is that this never runs
        raise AssertionError("evidence_store must never commit")


def test_the_module_never_writes_and_never_commits(cal):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with CommitRaises(engine) as db:
        epoch, _ = build_epoch(db, cal)
        db.flush()

        progress = epoch_progress(db, epoch_id=epoch.id, as_of=AS_OF, calendar=cal)

        assert progress.sessions_elapsed == 10
        assert not db.new
        assert not db.dirty
        assert not db.deleted
    engine.dispose()


def test_the_injected_calendar_interface_is_the_real_market_calendars(session):
    real = MarketCalendar()

    floor = scoring_floor_for(date(2026, 1, 2), calendar=real)

    assert floor == date(2026, 2, 13)
    assert real.is_trading_day(floor)
    assert len(real.trading_sessions(date(2026, 1, 2), floor)) == 30


def test_a_streak_runs_against_the_real_calendar_without_a_fake(session):
    real = MarketCalendar()
    days = real.trading_sessions(date(2026, 7, 1), date(2026, 8, 14))
    for day in days[-10:]:
        add_verdict(session, session_date=day, status=DivergenceStatus.BREACH)

    streak = breach_streak(
        session,
        sleeve=SLEEVE,
        as_of=date(2026, 8, 14),
        baseline_id=BASELINE,
        scoring_floor=days[-10],
        calendar=real,
    )

    assert streak.length == 10
    assert streak.fires is True


def test_exposure_is_measured_only_over_sessions_that_were_actually_observed(
    session, cal
):
    """A paused session cannot count toward exposure it is absent from.

    The denominator excludes paused sessions, so the numerator must too —
    otherwise a mostly-blind window reports more than 100% exposure.
    """
    epoch, days = build_epoch(session, cal, verdicts=False, exposed_sessions=10)
    for day in days[6:]:
        report(session, day, SLEEVES, DivergenceStatus.OK)

    progress = _progress(session, cal, epoch)

    assert progress.sessions_paused == 6
    assert progress.sessions_elapsed == 4
    assert progress.exposure_session_pct == pytest.approx(100.0)
