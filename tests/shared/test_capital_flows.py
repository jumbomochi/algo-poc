"""KAN-113: a recorded capital flow is never read as a return.

The synthetic series is one sleeve over six US sessions. It loses 1% a day
for two days, the operator credits $5,000 between the third and fourth
snapshots, it is flat for a session, gains 1% and is flat again:

    session      raw equity   flow-free return
    09-01        10,000.00
    09-02         9,900.00     -1.00%
    09-03         9,800.00     -1.01%
    -- credit +5,000 (committed 09-04 06:00 UTC, between the two writes) --
    09-04        14,800.00      0.00%    <- raw reads this as +51.0%
    09-08        14,948.00     +1.00%
    09-09        14,948.00      0.00%

Every reader must see -1.02% over the window and a 2% drawdown — not +49.5%
and no drawdown.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from backtest.divergence import build_report, window_return
from scripts.divergence_monitor import load_live_equity_series
from scripts.ops.evidence_digest import _equity_lines, equity_source
from scripts.paper_state import PaperTradingState
from scripts.run_paper import capital_flow_steps_by_sleeve, live_equity_by_sleeve
from shared.capital_flows import (
    CapitalFlow,
    flow_adjust,
    flow_adjusted_by_session,
    describe_flow_steps,
    flow_steps,
    included_flow,
    recorded_flows,
    rows_of_record,
)
from shared.evidence_store import equity_series, max_drawdown_pct
from shared.models import (
    Base,
    CapitalAdjustment,
    EquitySnapshot,
    PortfolioConfig,
)
from shared.position_loader import load_portfolio_state

SESSIONS = [
    date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3),
    date(2026, 9, 4), date(2026, 9, 8), date(2026, 9, 9),
]
RAW = [10_000.0, 9_900.0, 9_800.0, 14_800.0, 14_948.0, 14_948.0]
CREDIT = 5_000.0
FLOW_AT = datetime(2026, 9, 4, 6, 0, tzinfo=timezone.utc)
FLOW_FREE_RETURN = 0.98 * 1.01 - 1.0  # -1.02%


def _written(session_date: date) -> datetime:
    """04:15 SGT the next day: 20:15 UTC on the session date."""
    return datetime.combine(
        session_date, datetime.min.time(), tzinfo=timezone.utc
    ) + timedelta(hours=20, minutes=15)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(PortfolioConfig(
            portfolio="momentum", capital=15_000.0, cash=14_948.0,
            created_at=FLOW_AT, updated_at=FLOW_AT,
        ))
        for session_date, equity in zip(SESSIONS, RAW):
            session.add(EquitySnapshot(
                portfolio="momentum",
                date=session_date + timedelta(days=1),
                session_date=session_date,
                equity=equity, cash=equity, market_value=0.0,
                created_at=_written(session_date),
            ))
        session.add(CapitalAdjustment(
            account_id="DUN551088", portfolio="momentum", amount=CREDIT,
            reason="KAN-113 credit flow abc123: deposit", operator="ops",
            created_at=FLOW_AT,
        ))
        session.commit()
        yield session


# ----------------------------------------------------------- the arithmetic


def test_no_flows_leaves_a_series_unchanged_bit_for_bit():
    values = dict(zip(SESSIONS, RAW))
    assert flow_adjust(values, {}) == values
    assert flow_adjust(values, {d: 0.0 for d in SESSIONS}) == values


def test_a_mid_window_credit_is_removed_and_the_end_is_real():
    values = dict(zip(SESSIONS, RAW))
    included = {d: (CREDIT if d >= SESSIONS[3] else 0.0) for d in SESSIONS}
    adjusted = flow_adjust(values, included)

    series = [adjusted[d] for d in SESSIONS]
    assert series[-1] == RAW[-1]
    assert series[3:] == RAW[3:]  # after the flow: untouched
    assert series[3] / series[2] == pytest.approx(1.0)
    assert series[1] / series[0] == pytest.approx(0.99)
    assert window_return(series) == pytest.approx(FLOW_FREE_RETURN)
    assert window_return(RAW) == pytest.approx(0.4948)


def test_a_transfer_out_is_not_a_loss():
    values = {SESSIONS[0]: 10_000.0, SESSIONS[1]: 6_000.0}
    adjusted = flow_adjust(values, {SESSIONS[0]: 0.0, SESSIONS[1]: -4_000.0})
    assert adjusted[SESSIONS[1]] / adjusted[SESSIONS[0]] == pytest.approx(1.0)


def test_a_flow_is_in_a_row_written_after_it_commits():
    flows = [CapitalFlow("momentum", CREDIT, FLOW_AT)]
    assert included_flow(flows, _written(SESSIONS[2])) == 0.0
    assert included_flow(flows, _written(SESSIONS[3])) == CREDIT
    # sqlite hands back naive datetimes: they are UTC.
    naive = _written(SESSIONS[3]).replace(tzinfo=None)
    assert included_flow(flows, naive) == CREDIT


def test_a_catch_up_run_that_revalues_an_old_session_after_a_flow():
    """The row of record for 09-02 was rewritten after the credit, so the
    flow is in it; the steps into and out of it carry the flow both ways."""
    rows = [
        {"date": "2026-09-02", "session_date": "2026-09-01",
         "equity": 10_000.0, "created_at": _written(SESSIONS[0])},
        {"date": "2026-09-03", "session_date": "2026-09-02",
         "equity": 9_900.0, "created_at": _written(SESSIONS[1])},
        # Catch-up: 09-02 re-valued on 09-05, after the credit.
        {"date": "2026-09-05", "session_date": "2026-09-02",
         "equity": 14_900.0, "created_at": FLOW_AT + timedelta(hours=1)},
        {"date": "2026-09-04", "session_date": "2026-09-03",
         "equity": 9_800.0, "created_at": _written(SESSIONS[2])},
    ]
    record = rows_of_record(rows)
    assert record[SESSIONS[1]][0] == 14_900.0
    adjusted = flow_adjusted_by_session(
        rows, [CapitalFlow("momentum", CREDIT, FLOW_AT)]
    )
    days = sorted(adjusted)
    assert adjusted[days[1]] / adjusted[days[0]] == pytest.approx(0.99)
    # The step back out carries the flow the other way, so it reads as the
    # $100 loss it was, not as a -34% collapse. (Measured on the base that
    # held the idle credit, the same end-of-step convention as above.)
    assert adjusted[days[2]] / adjusted[days[1]] == pytest.approx(
        (9_800 + CREDIT) / 14_900
    )


def test_flow_steps_name_the_session_the_flow_enters():
    rows = [
        {"date": d + timedelta(days=1), "session_date": d, "equity": e,
         "created_at": _written(d)}
        for d, e in zip(SESSIONS, RAW)
    ]
    flows = [CapitalFlow("momentum", CREDIT, FLOW_AT)]
    assert flow_steps(rows, flows) == [(SESSIONS[3], CREDIT)]
    assert flow_steps(rows, []) == []
    assert flow_steps([], flows) == []
    # A flow not yet in any snapshot has no step yet.
    later = [CapitalFlow(
        "momentum", CREDIT, _written(SESSIONS[-1]) + timedelta(hours=1)
    )]
    assert flow_steps(rows, later) == []


def test_the_flow_note_names_only_steps_inside_the_window():
    steps = [("momentum", SESSIONS[3], CREDIT)]
    note = describe_flow_steps(steps, start=SESSIONS[0], end=SESSIONS[-1])
    assert "KAN-113" in note
    assert "momentum +5,000.00 USD valued from 2026-09-04" in note
    assert "graded in full" in note
    # On the window's first session the flow is already in the base.
    assert describe_flow_steps(
        steps, start=SESSIONS[3], end=SESSIONS[-1]
    ) is None
    assert describe_flow_steps(
        steps, start=SESSIONS[0], end=SESSIONS[2]
    ) is None
    assert describe_flow_steps(steps, start=None, end=None) is None


# --------------------------------------------------------------- the readers


def test_the_divergence_monitor_grades_the_flow_free_return(db, capsys):
    state = PaperTradingState(db)
    flows = state.capital_flows("momentum")
    assert [f.amount for f in flows] == [CREDIT]

    live = load_live_equity_series(state, "momentum", flows=flows)
    assert "flow-adjusted" in capsys.readouterr().out
    assert live[SESSIONS[-1]] == RAW[-1]

    # The shadow replays the strategy without the credit: the same flow-free
    # path from the same start.
    shadow = {}
    level = 10_000.0
    for index, session_date in enumerate(SESSIONS):
        if index:
            level *= RAW[index] / RAW[index - 1] if index != 3 else 1.0
        shadow[session_date] = level

    report = build_report("momentum", live, shadow, [], window_days=6)
    assert report.live_return == pytest.approx(FLOW_FREE_RETURN)
    assert report.status == "OK"
    assert report.daily_correlation == pytest.approx(1.0)

    raw = build_report(
        "momentum", load_live_equity_series(state, "momentum"), shadow, [],
        window_days=6,
    )
    assert raw.status == "BREACH"  # what the credit would have looked like


def test_the_gate_and_epoch_drawdown_see_through_the_credit(db):
    rows = equity_series(db, start=SESSIONS[0], end=SESSIONS[-1])
    values = [value for _, value, _ in rows]
    assert values[-1] == RAW[-1]
    assert window_return(values) == pytest.approx(FLOW_FREE_RETURN)
    assert max_drawdown_pct(rows) == pytest.approx(2.0)

    raw = equity_series(
        db, start=SESSIONS[0], end=SESSIONS[-1], flow_adjusted=False
    )
    assert [value for _, value, _ in raw] == RAW


def test_a_credit_cannot_close_a_drawdown(db):
    """The deposit lands in the trough. Raw, the series makes a new high
    and the 2% drawdown vanishes; adjusted, it is still there."""
    raw = equity_series(
        db, start=SESSIONS[0], end=SESSIONS[3], flow_adjusted=False
    )
    adjusted = equity_series(db, start=SESSIONS[0], end=SESSIONS[3])
    assert max_drawdown_pct(raw) == pytest.approx(2.0)  # 10,000 -> 9,800
    assert raw[-1][1] > raw[0][1]  # ...and then a "new high" of 14,800
    assert adjusted[-1][1] < adjusted[0][1]
    assert max_drawdown_pct(adjusted) == pytest.approx(2.0)


def test_the_digest_names_the_flow_instead_of_a_gain(db):
    line = equity_source(db, window_start=SESSIONS[0], as_of=SESSIONS[-1])()
    assert line.latest == RAW[-1]
    assert line.change_pct == pytest.approx(FLOW_FREE_RETURN * 100)
    assert line.flows == pytest.approx(CREDIT)
    [text, *_] = _equity_lines(line, [])
    assert "-1.0% wk" in text
    assert "excl. +5,000.00 capital flows" in text


def test_the_digest_line_is_unchanged_without_flows(db):
    db.execute(CapitalAdjustment.__table__.delete())
    line = equity_source(db, window_start=SESSIONS[0], as_of=SESSIONS[-1])()
    assert line.flows == 0.0
    assert line.change_pct == pytest.approx(49.48)
    assert "capital flows" not in _equity_lines(line, [])[0]


def test_the_risk_services_peak_nav_keeps_the_drawdown(db):
    """Run dates carry 10,000 then 9,800; the credit takes NAV to 14,800.
    Raw, the peak becomes 14,800 and the drawdown reads 0%."""
    db.execute(EquitySnapshot.__table__.delete().where(
        EquitySnapshot.session_date > SESSIONS[2]
    ))
    db.execute(EquitySnapshot.__table__.delete().where(
        EquitySnapshot.session_date == SESSIONS[1]
    ))
    config = db.scalar(select(PortfolioConfig))
    config.cash = 14_800.0
    db.flush()

    state = load_portfolio_state(db)
    assert state["nav"] == 14_800.0
    drawdown = (state["peak_nav"] - state["nav"]) / state["peak_nav"]
    assert drawdown == pytest.approx(0.02)

    db.execute(CapitalAdjustment.__table__.delete())
    assert load_portfolio_state(db)["peak_nav"] == 14_800.0


def test_the_shadow_seeds_raw_and_replays_the_flow(db):
    """The full window survives the flow. The seed curve is RAW (what live
    held) and the flow is handed to the shadow on the session it enters."""
    state = PaperTradingState(db)
    curve = live_equity_by_sleeve(state)["momentum"]
    assert [curve[d] for d in SESSIONS] == RAW
    assert capital_flow_steps_by_sleeve(state) == {
        "momentum": {SESSIONS[3]: CREDIT}
    }

    db.execute(CapitalAdjustment.__table__.delete())
    assert capital_flow_steps_by_sleeve(PaperTradingState(db)) == {}


def test_flows_on_a_synthetic_portfolio_never_reach_the_graded_readers(db):
    db.add(CapitalAdjustment(
        account_id="DUN551088", portfolio="__drill__", amount=1e6,
        reason="drill", created_at=FLOW_AT,
    ))
    db.flush()
    rows = equity_series(db, start=SESSIONS[0], end=SESSIONS[-1])
    assert window_return([v for _, v, _ in rows]) == pytest.approx(
        FLOW_FREE_RETURN
    )
    assert {f.portfolio for f in recorded_flows(db)} == {
        "momentum", "__drill__",
    }


def test_the_digest_names_flows_inside_the_divergence_window(db):
    from scripts.ops.evidence_digest import (
        Sources,
        _capital_flow_line,
        capital_flows_source,
        collect_snapshot,
        render_digest,
    )
    from shared.market_calendar import MarketCalendar

    calendar = MarketCalendar()
    db.add(CapitalAdjustment(
        account_id="DUN551088", portfolio="__drill__", amount=500.0,
        reason="drill", created_at=FLOW_AT,
    ))
    db.flush()
    flows = capital_flows_source(db, as_of=SESSIONS[-1], calendar=calendar)()
    assert [(f.portfolio, f.amount) for f in flows] == [("momentum", CREDIT)]
    [line] = _capital_flow_line(flows)
    assert line.startswith("📥 CAPITAL FLOW (KAN-113) in the divergence window")
    assert "momentum +5,000.00 on 2026-09-04" in line
    assert "BREACH streaks unchanged" in line

    # Thirty sessions later the flow has left every divergence window.
    assert capital_flows_source(
        db, as_of=date(2026, 11, 30), calendar=calendar
    )() == []

    # And it reaches the rendered digest through collect_snapshot.
    sources = Sources(
        epoch=lambda: None, blind=lambda: None, sleeves=lambda: [],
        equity=lambda: None, dlq=lambda: {}, alerts=lambda: None,
        drills=lambda: [],
        capital_flows=capital_flows_source(
            db, as_of=SESSIONS[-1], calendar=calendar
        ),
    )
    snapshot = collect_snapshot(
        sources, as_of=SESSIONS[-1], window_start=SESSIONS[0]
    )
    assert "📥 CAPITAL FLOW (KAN-113)" in render_digest(snapshot)


def test_a_transfer_hands_the_shadow_both_legs(db):
    db.add(PortfolioConfig(
        portfolio="sector_rotation", capital=10_000.0, cash=10_000.0,
        created_at=FLOW_AT, updated_at=FLOW_AT,
    ))
    for session_date, equity in zip(SESSIONS, [10_000.0] * 3 + [9_000.0] * 3):
        db.add(EquitySnapshot(
            portfolio="sector_rotation", date=session_date + timedelta(days=1),
            session_date=session_date, equity=equity, cash=equity,
            market_value=0.0, created_at=_written(session_date),
        ))
    db.add(CapitalAdjustment(
        account_id="DUN551088", portfolio="sector_rotation", amount=-1_000.0,
        reason="KAN-113 transfer flow def456: t", created_at=FLOW_AT,
    ))
    db.flush()
    assert capital_flow_steps_by_sleeve(PaperTradingState(db)) == {
        "momentum": {SESSIONS[3]: CREDIT},
        "sector_rotation": {SESSIONS[3]: -1_000.0},
    }
