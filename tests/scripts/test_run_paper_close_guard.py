"""run_paper.py refuses to price a session that has not closed — KAN-104.

The guard has two halves and both are driven here through ``main()`` with the
run's clock (``run_paper._utc_now``) faked:

1. **Before the run touches the broker or the book**: is a session in progress
   right now? This is the half the old 04:15 SGT slot trips every EST winter.
2. **After the fetch**: had the session the newest bar covers closed when the
   fetch STARTED? This catches a run that began pre-open and fetched into the
   session, which the first half cannot see.

A refusal returns exit 4 (the wrapper names it and withholds the dead-man ping)
and raises a best-effort alert on ``stream:alerts``. Collaborators past the
point under test are replaced with tripwires, so "the run stopped here" is
asserted by what was never reached rather than inferred from output.
"""

from __future__ import annotations

import ast
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import scripts.run_paper as run_paper
from shared.market_calendar import ET

SGT = ZoneInfo("Asia/Singapore")
REPO = Path(__file__).resolve().parents[2]
WRAPPER = REPO / "deploy/launchd/run_paper.sh"


class Reached(Exception):
    """A tripwire: the run got further than the guard should have let it."""


class FakeSession:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass


@pytest.fixture
def harness(monkeypatch):
    """main() wired to fakes up to the IB fetch, with a scriptable clock."""
    h = SimpleNamespace(
        clock=[],
        alerts=[],
        session=FakeSession(),
        bars={},
        fetch_calls=0,
        broker_reads=0,
    )

    def fake_now():
        assert h.clock, "the run read its clock more often than the test expected"
        return h.clock.pop(0)

    def fake_alert(redis_url, **kwargs):
        h.alerts.append(kwargs)
        return True

    async def fake_broker_snapshot(**kwargs):
        h.broker_reads += 1
        return SimpleNamespace(account_id="DU0000000")

    def fake_fetch(**kwargs):
        h.fetch_calls += 1
        return h.bars

    def priced(*args, **kwargs):
        raise Reached("pricing")

    capital = SimpleNamespace(
        net_liquidation_base=1.0,
        net_liquidation_trading_equivalent=1.0,
        fx_base_per_trading=1.0,
        settled_cash_trading=1.0,
        deployable_capital=1.0,
    )
    monkeypatch.setattr(run_paper, "_utc_now", fake_now)
    monkeypatch.setattr(run_paper, "emit_alert_best_effort", fake_alert)
    monkeypatch.setattr(run_paper, "make_db_session", lambda url: h.session)
    monkeypatch.setattr(
        run_paper.PaperTradingState, "load", classmethod(lambda cls, s: SimpleNamespace())
    )
    monkeypatch.setattr(run_paper, "read_broker_snapshot", fake_broker_snapshot)
    monkeypatch.setattr(
        run_paper,
        "prepare_daily_run",
        lambda **kw: SimpleNamespace(
            reconciliation=SimpleNamespace(entries_allowed=True, severity="ok"),
            capital=capital,
        ),
    )
    monkeypatch.setattr(run_paper, "get_union_universe", lambda sleeves: ["AAA", "BBB"])
    monkeypatch.setattr(run_paper, "fetch_bars_from_ib", fake_fetch)
    # The first thing the run does with the bars after the guard.
    monkeypatch.setattr(run_paper, "load_data_caches", priced)
    monkeypatch.setattr(
        sys, "argv", ["run_paper.py", "--db-url", "sqlite://", "--redis-url", "redis://x"]
    )
    return h


def _bars(session: date) -> dict[str, list[dict]]:
    prior = session - timedelta(days=3)
    return {
        "AAA": [{"date": prior, "close": 10.0}, {"date": session, "close": 10.5}],
        "BBB": [{"date": prior, "close": 20.0}],
    }


# ---------------------------------------------------------------------------
# Half 1: the clock, before the broker or the book are touched
# ---------------------------------------------------------------------------

def test_the_old_slot_on_the_first_est_morning_is_refused_before_the_broker(harness):
    """Tue 2026-11-03 04:15 SGT = Mon 15:15 EST: the run the ticket is about."""
    harness.clock = [datetime(2026, 11, 3, 4, 15, tzinfo=SGT)]

    code = run_paper.main()

    assert code == run_paper.EXIT_SESSION_NOT_CLOSED == 4
    assert harness.broker_reads == 0, "the broker was read before the guard"
    assert harness.fetch_calls == 0
    assert harness.session.closed
    [alert] = harness.alerts
    assert alert["event_type"] == "paper_run_session_not_closed"
    assert alert["priority"] == "high"
    assert alert["context"]["session"] == "2026-11-02"
    assert "2026-11-02" in alert["message"]


def test_the_old_slot_on_the_last_est_morning_is_refused(harness):
    """Sat 2027-03-13 04:15 SGT = Fri 03-12 15:15 EST."""
    harness.clock = [datetime(2027, 3, 13, 4, 15, tzinfo=SGT)]

    assert run_paper.main() == 4
    assert harness.alerts[0]["context"]["session"] == "2027-03-12"


def test_a_half_day_is_refused_before_its_13_00_close(harness):
    harness.clock = [datetime(2026, 11, 27, 12, 45, tzinfo=ET)]

    assert run_paper.main() == 4
    assert "13:00" in harness.alerts[0]["message"]


def test_the_refusal_names_the_session_and_when_to_re_run(harness, capsys):
    harness.clock = [datetime(2026, 11, 3, 4, 15, tzinfo=SGT)]

    run_paper.main()

    out = capsys.readouterr().out
    assert "REFUSED" in out
    assert "16:00 EST" in out
    assert "earliest re-run is 2026-11-02 16:05 EST (2026-11-03 05:05 SGT)" in out
    assert "next scheduled 05:15 SGT run" in out


def test_the_refusal_tells_a_catch_up_what_it_has_lost(harness, capsys):
    """Review MEDIUM-1. The 05:15 run was missed and the operator starts a
    catch-up at 23:00 SGT — inside the next session. It is refused, correctly,
    and the message must say that the missed session cannot be booked now and
    where the rule is written down, rather than leave a retry loop."""
    harness.clock = [datetime(2026, 11, 3, 23, 0, tzinfo=SGT)]  # Tue 10:00 EST

    assert run_paper.main() == 4

    out = capsys.readouterr().out
    assert "2026-11-03" in out
    assert "missed session can no longer be booked" in out
    assert "Catch-up runs" in out
    assert "no as-of mode" in out


# ---------------------------------------------------------------------------
# Half 2: the bars, against the instant the fetch started
# ---------------------------------------------------------------------------

def test_a_pre_open_run_that_fetched_todays_forming_bar_is_refused(harness):
    """Started 09:25 ET (passes the clock check: nothing is in progress yet),
    the fetch started 09:31 and came back with a bar for today."""
    harness.clock = [
        datetime(2026, 11, 2, 9, 25, tzinfo=ET),
        datetime(2026, 11, 2, 9, 31, tzinfo=ET),
    ]
    harness.bars = _bars(date(2026, 11, 2))

    code = run_paper.main()

    assert code == 4
    assert harness.fetch_calls == 1
    assert harness.alerts[0]["context"]["stage"] == "newest bar unclosed"


@pytest.mark.parametrize(
    "now, bars_session",
    [
        # EST, the new 05:15 slot, bars through the session that just closed.
        (datetime(2026, 11, 3, 5, 15, tzinfo=SGT), date(2026, 11, 2)),
        # EDT, the new 05:15 slot.
        (datetime(2026, 10, 6, 5, 15, tzinfo=SGT), date(2026, 10, 5)),
        # Half day, after its 13:00 close.
        (datetime(2026, 11, 28, 5, 15, tzinfo=SGT), date(2026, 11, 27)),
        # A pre-open catch-up that only saw yesterday's bars.
        (datetime(2026, 11, 3, 9, 0, tzinfo=ET), date(2026, 11, 2)),
    ],
)
def test_a_post_close_run_goes_on_to_price(harness, now, bars_session):
    harness.clock = [now, now + timedelta(seconds=30)]
    harness.bars = _bars(bars_session)

    with pytest.raises(Reached, match="pricing"):
        run_paper.main()

    assert harness.alerts == []
    assert harness.broker_reads == 1


def test_the_fetch_start_not_its_end_is_what_is_checked():
    """A fetch that starts at 15:58 ET and ends at 16:06 holds partial bars for
    its first tickers, so its END proves nothing. The instant checked against
    the newest bar's close must be read BEFORE fetch_bars_from_ib is called.
    Proven structurally, because a faked clock cannot tell the two apart."""
    tree = ast.parse(Path(run_paper.__file__).read_text())
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    src = ast.get_source_segment(Path(run_paper.__file__).read_text(), main)
    assert src.index("fetch_started_at = _utc_now()") < src.index("fetch_bars_from_ib(")
    assert "unclosed_session(bars_session, fetch_started_at)" in src
    # And the same pair is what the shadow carries for the monitor to re-check.
    assert "priced_at=fetch_started_at" in src
    assert "bars_session=bars_session" in src


# ---------------------------------------------------------------------------
# Side effects of a late refusal (review MEDIUM-2)
# ---------------------------------------------------------------------------

#: Captured at import, before any fixture patches the module.
REAL_PREPARE_DAILY_RUN = run_paper.prepare_daily_run
REAL_MAKE_DB_SESSION = run_paper.make_db_session


def _broker_snapshot():
    """A real BrokerAccountSnapshot, captured now (the FX staleness check in
    calculate_capital_budget reads the real clock, not the faked one)."""
    from datetime import timezone

    from shared.broker_state import BrokerAccountSnapshot

    captured_at = datetime.now(timezone.utc)
    return BrokerAccountSnapshot(
        account_id="DUTEST",
        mode="paper",
        base_currency="SGD",
        trading_currency="USD",
        net_liquidation_base=1_350_000,
        fx_base_per_trading=1.35,
        net_liquidation_trading_equivalent=1_000_000,
        settled_cash_trading=50_000,
        fx_source="test",
        fx_captured_at=captured_at,
        captured_at=captured_at,
    )


def test_a_late_refusal_then_a_good_run_leaves_a_clean_book(
    harness, monkeypatch, tmp_path, capsys
):
    """The "newest bar unclosed" refusal fires AFTER read_broker_snapshot and
    prepare_daily_run have committed a capital_snapshots and a
    reconciliation_reports row. Drive that refusal against a real database,
    then the next good run, and check what every reader of those tables sees.

    What "clean" means here, reader by reader:

    * risk_management takes the NEWEST capital_snapshots row (captured_at
      desc, id desc), and its drawdown peak is a max over real broker NAVs;
      reconciliation_status.py and gate_data_source.py take the NEWEST
      reconciliation_reports row. So the good run's rows must be the newest,
      and must not carry a stale blocking severity.
    * Nothing priced from bars may exist from the refused run: no equity
      snapshot, no order intent, no position.
    * Entries are decided from the run's OWN reconciliation, not a stored one,
      so the good run must report entries enabled.

    The refused run's rows are kept on purpose: each is a true broker reading
    taken before the open (the late half of the guard can only fire for a run
    that started pre-open), not a price derived from a partial bar. Deferring
    the commit to drop them would also drop the reconciliation reading when a
    run crashes or times out mid-fetch, which the KAN-86 report depends on.
    """
    from sqlalchemy import create_engine, func, select
    from sqlalchemy.orm import Session

    from scripts.ops.reconciliation_status import collect_facts
    from shared.models import (
        Base,
        CapitalSnapshot,
        OrderIntent,
        Position,
        ReconciliationReport,
    )
    from shared.models.equity_snapshot import EquitySnapshot

    db_url = f"sqlite:///{tmp_path / 'paper.db'}"
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)

    async def real_shaped_snapshot(**kwargs):
        harness.broker_reads += 1
        return _broker_snapshot()

    monkeypatch.setattr(run_paper, "make_db_session", REAL_MAKE_DB_SESSION)
    monkeypatch.setattr(run_paper, "read_broker_snapshot", real_shaped_snapshot)
    monkeypatch.setattr(run_paper, "prepare_daily_run", REAL_PREPARE_DAILY_RUN)
    monkeypatch.setattr(sys, "argv", [
        "run_paper.py", "--db-url", db_url, "--redis-url", "redis://x",
        "--no-entries-disabled",
    ])

    def counts():
        with Session(engine) as s:
            return {
                model.__name__: s.scalar(select(func.count()).select_from(model))
                for model in (
                    CapitalSnapshot, ReconciliationReport,
                    EquitySnapshot, OrderIntent, Position,
                )
            }

    # 1. A catch-up started pre-open (Mon 2026-11-02 09:25 EST = 22:25 SGT),
    #    whose fetch began after the open and returned the forming bar.
    harness.clock = [
        datetime(2026, 11, 2, 9, 25, tzinfo=ET),
        datetime(2026, 11, 2, 9, 31, tzinfo=ET),
    ]
    harness.bars = _bars(date(2026, 11, 2))
    assert run_paper.main() == 4
    assert counts() == {
        "CapitalSnapshot": 1, "ReconciliationReport": 1,
        "EquitySnapshot": 0, "OrderIntent": 0, "Position": 0,
    }
    with Session(engine) as s:
        refused_snapshot_id = s.scalar(select(CapitalSnapshot.id))

    # 2. The scheduled run after the close (Tue 2026-11-03 05:15 SGT).
    harness.clock = [
        datetime(2026, 11, 3, 5, 15, tzinfo=SGT),
        datetime(2026, 11, 3, 5, 15, 30, tzinfo=SGT),
    ]
    harness.bars = _bars(date(2026, 11, 2))
    capsys.readouterr()
    with pytest.raises(Reached, match="pricing"):
        run_paper.main()
    assert "entries: enabled" in capsys.readouterr().out
    assert counts() == {
        "CapitalSnapshot": 2, "ReconciliationReport": 2,
        "EquitySnapshot": 0, "OrderIntent": 0, "Position": 0,
    }

    with Session(engine) as s:
        newest_capital = s.scalars(
            select(CapitalSnapshot).order_by(
                CapitalSnapshot.captured_at.desc(), CapitalSnapshot.id.desc()
            )
        ).first()
        assert newest_capital.id != refused_snapshot_id, (
            "risk would size against the refused run's capital snapshot"
        )
        assert newest_capital.reconciliation_status == "ok"
        newest_report = s.scalars(
            select(ReconciliationReport).order_by(
                ReconciliationReport.created_at.desc(), ReconciliationReport.id.desc()
            )
        ).first()
        assert newest_report.entries_allowed is True
        assert newest_report.status == "ok"
        facts = collect_facts(s, mode="paper")
        assert facts.status == "ok" and facts.entries_allowed


# ---------------------------------------------------------------------------
# The drill exemption
# ---------------------------------------------------------------------------

def test_a_tagged_drill_run_is_exempt_and_says_so(harness, monkeypatch, capsys):
    """docs/operations/drill-runbook.md opens the drill position during RTH so
    the entry fills; the drill sleeve is excluded from every graded reader."""
    monkeypatch.setattr(sys, "argv", [
        "run_paper.py", "--db-url", "sqlite://", "--redis-url", "redis://x",
        "--portfolio-tag", run_paper.DRILL_PORTFOLIO, "--portfolio-tag-capital", "500",
    ])
    monkeypatch.setattr(run_paper, "ensure_tagged_portfolio", lambda *a, **k: False)
    # Mid-session. Read once, to stamp the fetch; the clock half of the guard
    # never asks, and the bars half is skipped for a tag.
    harness.clock = [datetime(2026, 11, 2, 11, 0, tzinfo=ET)]
    harness.bars = _bars(date(2026, 11, 2))

    with pytest.raises(Reached, match="pricing"):
        run_paper.main()

    assert "exempt from the KAN-104 close guard" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Contract with the wrapper
# ---------------------------------------------------------------------------

def test_the_exit_code_is_distinct_from_the_existing_ones():
    assert run_paper.EXIT_SESSION_NOT_CLOSED not in {0, 1, 2, run_paper.EXIT_INSUFFICIENT_BAR_COVERAGE, 124}


def test_the_wrapper_names_exit_4_instead_of_no_signals_committed():
    text = WRAPPER.read_text()
    assert '[ "$EXIT_CODE" = "4" ]' in text
    assert "NYSE session had not closed" in text
    # Review MEDIUM-1: the Telegram line must say what to do, not just what happened.
    assert "next scheduled 05:15 SGT run" in text
    assert "cannot be caught up once the next session has opened" in text
