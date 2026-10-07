"""Tests for re-projecting a burned fill (KAN-108).

The scenario is the real one. ARKW order 272 (thematic_momentum) filled at IB
on 2026-10-07 at 13:30:13Z, 11 @ 165.00, commission 1.090036, execution
0000e0d5.6ac63cba.01.01. The sleeve held $24.83, so the projector refused it
with "fill would make sleeve cash negative" AFTER committing the execution row
-- the burn. On 2026-10-08 the startup restore expired intent 272 with
ABSENT_AT_IB_REASON. The repair plan is to fund the sleeve from its own ARKG
sale (order 276, 37 sh) and then re-project.

Every burn below is produced by the real ``FillProjector.apply``, not by
writing a ``projection_applied=false`` row by hand, so the tests break if the
projector's audit-row behaviour changes under the tool.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import Session

from scripts.ops.reproject_fill import (
    CONFIRMATION,
    DLQ_STREAM,
    ReprojectRefusedError,
    apply_reprojection,
    main,
    plan_reprojection,
    write_exclusive,
)
from scripts.reconcile_paper import reconcile_snapshot
from services.portfolio_accounting.projector import (
    FillProjector,
    InvalidFillError,
)
from shared.broker_state import BrokerPosition
from shared.models import (
    Base,
    EquitySnapshot,
    ExecutionFill,
    OrderIntent,
    OrderStatus,
    PortfolioConfig,
    Position,
)
from shared.order_ledger import ABSENT_AT_IB_REASON, OrderLedger
from shared.schemas.messages import FillMessage

ACCOUNT = "DUN551088"
SLEEVE = "thematic_momentum"
ARKW_CON = 168647416
ARKG_CON = 172522641
EXEC_ID = "0000e0d5.6ac63cba.01.01"
REC_ARKW = "sleeve-2026-10-07-DUN551088-paper-thematic_momentum-ARKW-buy"
REC_ARKG = "sleeve-2026-10-08-DUN551088-paper-thematic_momentum-ARKG-sell"
EXECUTED = datetime(2026, 10, 7, 13, 30, 13, tzinfo=timezone.utc)
CREATED = datetime(2026, 9, 24, 13, 30, 15, tzinfo=timezone.utc)
SLEEVE_CASH = 24.831929000000628
ARKW_COST = 11 * 165.0 + 1.090036          # 1,816.090036
ARKG_PROCEEDS = 37 * 50.17 - 1.0           # 1,855.29


def _intent(recommendation_id, *, action, symbol, con_id, quantity, order_id):
    return OrderIntent(
        recommendation_id=recommendation_id,
        account_id=ACCOUNT,
        mode="paper",
        portfolio=SLEEVE,
        con_id=con_id,
        symbol=symbol,
        exchange="SMART",
        currency="USD",
        action=action,
        requested_quantity=quantity,
        limit_price=165.0 if symbol == "ARKW" else None,
        order_type="LMT" if action == "BUY" else "MKT",
        reserved_notional=quantity * 165.0 if action == "BUY" else 0,
        filled_quantity=0,
        status=OrderStatus.SUBMITTED.value,
        ib_order_id=order_id,
        created_at=CREATED,
        updated_at=CREATED,
        submitted_at=CREATED,
    )


def _arkw_fill() -> FillMessage:
    """The message the live callback published on 2026-10-07."""
    return FillMessage(
        ticker="ARKW",
        timestamp=EXECUTED,
        side="buy",
        quantity=11.0,
        fill_price=165.0,
        commission=1.090036,
        commission_currency="USD",
        commission_trading=1.090036,
        recommendation_id=REC_ARKW,
        order_id="272",
        execution_id=EXEC_ID,
        account_id=ACCOUNT,
        cumulative_quantity=11.0,
        portfolio=SLEEVE,
        con_id=ARKW_CON,
        exchange="BATS",
        currency="USD",
        order_done=True,
    )


def _arkg_sale() -> FillMessage:
    return FillMessage(
        ticker="ARKG",
        timestamp=datetime(2026, 10, 8, 13, 30, 2, tzinfo=timezone.utc),
        side="sell",
        quantity=37.0,
        fill_price=50.17,
        commission=1.0,
        commission_currency="USD",
        commission_trading=1.0,
        recommendation_id=REC_ARKG,
        order_id="276",
        execution_id="0000e0d5.6ac7aaaa.01.01",
        account_id=ACCOUNT,
        cumulative_quantity=37.0,
        portfolio=SLEEVE,
        con_id=ARKG_CON,
        exchange="ARCA",
        currency="USD",
        order_done=True,
    )


def _book(tmp_path, *, expire_reason: str | None = ABSENT_AT_IB_REASON):
    """The paper book as it stood on the morning of 2026-10-08."""
    url = f"sqlite:///{tmp_path / 'book.db'}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(PortfolioConfig(
            portfolio=SLEEVE, capital=14_100.0, cash=SLEEVE_CASH,
            created_at=CREATED, updated_at=CREATED,
        ))
        session.add(Position(
            account_id=ACCOUNT, ticker="ARKG", portfolio=SLEEVE,
            con_id=ARKG_CON, exchange="SMART", currency="USD", quantity=37.0,
            avg_entry_price=50.45, current_price=50.45, peak_price=50.45,
            highest_price_since_entry=50.45, opened_at=CREATED, status="open",
        ))
        session.add(_intent(
            REC_ARKW, action="BUY", symbol="ARKW", con_id=ARKW_CON,
            quantity=11.1691, order_id="272",
        ))
        session.add(_intent(
            REC_ARKG, action="SELL", symbol="ARKG", con_id=ARKG_CON,
            quantity=37.0, order_id="276",
        ))
        # The SGT 2026-10-08 run valued the sleeve WITHOUT the ARKW it owned.
        session.add(EquitySnapshot(
            portfolio=SLEEVE, date=date(2026, 10, 8), equity=1_891.0,
            cash=SLEEVE_CASH, market_value=1_866.0,
            session_date=date(2026, 10, 7),
            created_at=datetime(2026, 10, 7, 20, 15, tzinfo=timezone.utc),
        ))
        session.commit()

        # The burn, by the real projector, exactly as on 2026-10-07.
        with pytest.raises(InvalidFillError, match="sleeve cash negative"):
            FillProjector(session).apply(_arkw_fill())

        # KAN-106's startup restore on 2026-10-08.
        if expire_reason is not None:
            OrderLedger(session).transition(
                REC_ARKW, OrderStatus.EXPIRED, reason=expire_reason
            )
            session.commit()
    return url, engine


def _fund(engine):
    """Tonight's ARKG trailing-stop sale, projected the ordinary way."""
    with Session(engine) as session:
        assert FillProjector(session).apply(_arkg_sale()) is True


def _state(engine) -> dict:
    """Every row of every table: 'writes nothing' means this is unchanged."""
    with engine.connect() as conn:
        return {
            table.name: sorted(
                (tuple(map(repr, row)) for row in conn.execute(table.select())),
            )
            for table in Base.metadata.sorted_tables
        }


def _cash(session) -> float:
    return float(session.scalar(
        select(PortfolioConfig.cash).where(PortfolioConfig.portfolio == SLEEVE)
    ))


def _confirm(monkeypatch, answer=CONFIRMATION, tty=True):
    monkeypatch.setattr("sys.stdin.isatty", lambda: tty, raising=False)
    monkeypatch.setattr("builtins.input", lambda *a: answer)


def _cli(url, tmp_path, *extra):
    return main([
        "--database-url", url,
        "--artifact-dir", str(tmp_path / "reconciliation"),
        *extra,
    ])


# --------------------------------------------------------------------------
# AC1. The dry run reports the exact delta, refuses on cash, writes nothing.
# --------------------------------------------------------------------------


def test_the_dry_run_refuses_an_unfunded_sleeve_and_writes_nothing(
    tmp_path, capsys
):
    url, engine = _book(tmp_path)
    before = _state(engine)

    assert _cli(url, tmp_path, "--execution-id", EXEC_ID) == 1

    out = capsys.readouterr().out
    assert EXEC_ID in out
    assert "ARKW" in out and "272" in out
    # The exact cash delta and both ends of it, to the cent and beyond.
    assert "-1,816.090036" in out
    assert "24.831929" in out
    assert "-1,791.258107" in out
    assert "sleeve cash negative" in out
    assert "EXPIRED" in out and "UN-EXPIRE" in out
    assert _state(engine) == before
    assert not (tmp_path / "reconciliation").exists()


def test_the_dry_run_of_a_funded_sleeve_passes_every_check(tmp_path, capsys):
    url, engine = _book(tmp_path)
    _fund(engine)
    before = _state(engine)

    assert _cli(url, tmp_path, "--execution-id", EXEC_ID) == 0

    out = capsys.readouterr().out
    assert "FAIL" not in out
    assert "Dry-run only" in out
    assert "FILLED" in out
    # The DLQ entry is named, with how to drain it, before anything happens.
    assert DLQ_STREAM in out and "XDEL" in out
    assert _state(engine) == before


def test_planning_issues_only_selects(tmp_path):
    """The dry run's no-write guarantee, below the CLI: on Postgres it also
    runs READ ONLY, but the plan must not depend on that to be harmless."""
    _, engine = _book(tmp_path)
    _fund(engine)
    statements: list[str] = []
    event.listen(
        engine, "before_cursor_execute",
        lambda conn, cursor, statement, *a: statements.append(statement),
    )

    with Session(engine) as session:
        plan = plan_reprojection(session, execution_ids=[EXEC_ID])
        session.rollback()

    assert plan.fills and not plan.problems
    assert statements
    assert all(s.lstrip().upper().startswith("SELECT") for s in statements)


# --------------------------------------------------------------------------
# AC2/AC3. --apply projects it; the absent-expiry is undone.
# --------------------------------------------------------------------------


def test_apply_projects_the_burned_fill(tmp_path, monkeypatch, capsys):
    url, engine = _book(tmp_path)
    _fund(engine)
    _confirm(monkeypatch)

    assert _cli(url, tmp_path, "--execution-id", EXEC_ID, "--apply") == 0

    with Session(engine) as session:
        position = session.scalar(
            select(Position).where(Position.con_id == ARKW_CON)
        )
        assert position is not None
        assert position.quantity == 11.0
        assert position.avg_entry_price == 165.0
        assert position.account_id == ACCOUNT
        assert position.portfolio == SLEEVE
        assert position.exchange == "SMART"   # the intent's, as apply() does
        assert position.opened_at.replace(tzinfo=timezone.utc) == EXECUTED

        assert _cash(session) == pytest.approx(
            SLEEVE_CASH + ARKG_PROCEEDS - ARKW_COST, abs=1e-9
        )

        intent = session.scalar(
            select(OrderIntent).where(OrderIntent.recommendation_id == REC_ARKW)
        )
        # AC3: un-expired, then terminalized by the fill. 11 of 11.1691 is
        # whole-share rounding, which the projector completes as FILLED.
        assert intent.status == OrderStatus.FILLED.value
        assert intent.reason is None
        assert intent.filled_quantity == 11.0

        row = session.scalar(
            select(ExecutionFill).where(ExecutionFill.execution_id == EXEC_ID)
        )
        assert row.projection_applied is True
        # The immutable audit row is never rewritten.
        assert row.price == 165.0 and row.exchange == "BATS"


def test_apply_writes_an_audit_artifact_naming_the_equity_gap(
    tmp_path, monkeypatch
):
    url, engine = _book(tmp_path)
    _fund(engine)
    _confirm(monkeypatch)

    assert _cli(url, tmp_path, "--execution-id", EXEC_ID, "--apply") == 0

    [artifact] = list((tmp_path / "reconciliation").glob("reproject-fill-*.json"))
    payload = json.loads(artifact.read_text())
    [applied] = payload["applied"]
    assert applied["execution_id"] == EXEC_ID
    assert applied["unexpired"] is True
    assert applied["intent_status_before"] == OrderStatus.EXPIRED.value
    assert applied["intent_reason_before"] == ABSENT_AT_IB_REASON
    assert applied["intent_status_after"] == OrderStatus.FILLED.value
    assert applied["cash_delta"] == pytest.approx(-ARKW_COST, abs=1e-9)
    assert payload["failed"] == []
    assert "NOT rewritten" in payload["equity_series_note"]
    [snapshot] = payload["equity_snapshots_not_repainted"]
    assert snapshot["date"] == "2026-10-08"
    assert payload["dlq"]["stream"] == DLQ_STREAM
    assert payload["dlq"]["execution_ids"] == [EXEC_ID]


def test_an_intent_expired_for_another_reason_is_refused(
    tmp_path, monkeypatch, capsys
):
    url, engine = _book(tmp_path, expire_reason="IB reported Inactive")
    _fund(engine)
    before = _state(engine)
    _confirm(monkeypatch)

    assert _cli(url, tmp_path, "--execution-id", EXEC_ID) == 1
    assert "IB reported Inactive" in capsys.readouterr().out

    assert _cli(url, tmp_path, "--execution-id", EXEC_ID, "--apply") == 1
    assert _state(engine) == before


def test_a_cancelled_intent_is_refused(tmp_path, capsys):
    """CANCELLED is a terminal decision this tool does not overturn, even
    though the projector itself would accept a late fill against it."""
    url, engine = _book(tmp_path, expire_reason=None)
    with Session(engine) as session:
        OrderLedger(session).transition(REC_ARKW, OrderStatus.CANCELLED)
        session.commit()
    _fund(engine)

    assert _cli(url, tmp_path, "--execution-id", EXEC_ID) == 1
    assert "CANCELLED" in capsys.readouterr().out


def test_a_submitted_intent_is_filled_without_un_expiring(tmp_path, monkeypatch):
    url, engine = _book(tmp_path, expire_reason=None)
    _fund(engine)
    _confirm(monkeypatch)

    assert _cli(url, tmp_path, "--execution-id", EXEC_ID, "--apply") == 0

    with Session(engine) as session:
        intent = session.scalar(
            select(OrderIntent).where(OrderIntent.recommendation_id == REC_ARKW)
        )
        assert intent.status == OrderStatus.FILLED.value


# --------------------------------------------------------------------------
# AC2. One transaction per fill; a failure rolls back completely.
# --------------------------------------------------------------------------


def test_a_failure_inside_apply_rolls_back_the_un_expiry_too(tmp_path):
    """The plan was made while the sleeve was funded; the cash went away
    before --apply ran. The projector refuses, and nothing moves -- not the
    accounting, not the flag, and not the intent's un-expiry, which happened
    earlier in the same transaction."""
    _, engine = _book(tmp_path)
    _fund(engine)
    with Session(engine) as session:
        plan = plan_reprojection(session, execution_ids=[EXEC_ID])
        assert not plan.problems
        session.execute(text(
            "UPDATE portfolio_config SET cash = :cash WHERE portfolio = :p"
        ), {"cash": SLEEVE_CASH, "p": SLEEVE})
        session.commit()
        before = _state(engine)

        result = apply_reprojection(session, plan, confirm=CONFIRMATION)

    assert result.applied == ()
    [(execution_id, reason)] = result.failed
    assert execution_id == EXEC_ID
    assert "sleeve cash negative" in reason
    assert _state(engine) == before
    with Session(engine) as session:
        intent = session.scalar(
            select(OrderIntent).where(OrderIntent.recommendation_id == REC_ARKW)
        )
        assert intent.status == OrderStatus.EXPIRED.value
        assert intent.reason == ABSENT_AT_IB_REASON


def test_a_failure_is_written_to_the_artifact(tmp_path, monkeypatch, capsys):
    url, engine = _book(tmp_path)
    _fund(engine)
    _confirm(monkeypatch)
    original = FillProjector.project_recorded

    def drained(self, execution, intent, *, order_done):
        # The sleeve is spent between the plan and the write.
        self.session.execute(text(
            "UPDATE portfolio_config SET cash = 0 WHERE portfolio = :p"
        ), {"p": SLEEVE})
        return original(self, execution, intent, order_done=order_done)

    monkeypatch.setattr(FillProjector, "project_recorded", drained)

    assert _cli(url, tmp_path, "--execution-id", EXEC_ID, "--apply") == 1

    [artifact] = list((tmp_path / "reconciliation").glob("reproject-fill-*.json"))
    payload = json.loads(artifact.read_text())
    assert payload["applied"] == []
    assert payload["failed"][0]["execution_id"] == EXEC_ID
    with Session(engine) as session:
        row = session.scalar(
            select(ExecutionFill).where(ExecutionFill.execution_id == EXEC_ID)
        )
        assert row.projection_applied is False
        assert _cash(session) == pytest.approx(SLEEVE_CASH + ARKG_PROCEEDS)


# --------------------------------------------------------------------------
# AC4. Re-running after success is a no-op.
# --------------------------------------------------------------------------


def test_re_running_after_success_changes_nothing(tmp_path, monkeypatch, capsys):
    url, engine = _book(tmp_path)
    _fund(engine)
    _confirm(monkeypatch)
    assert _cli(url, tmp_path, "--execution-id", EXEC_ID, "--apply") == 0
    after_first = _state(engine)
    capsys.readouterr()

    assert _cli(url, tmp_path, "--all-unapplied", "--apply") == 0
    assert "No unprojected fills" in capsys.readouterr().out
    assert _cli(url, tmp_path, "--execution-id", EXEC_ID, "--apply") == 0
    assert "already projected" in capsys.readouterr().out

    assert _state(engine) == after_first
    assert len(list((tmp_path / "reconciliation").glob("*.json"))) == 1


def test_all_unapplied_selects_the_burn(tmp_path):
    _, engine = _book(tmp_path)
    with Session(engine) as session:
        plan = plan_reprojection(session, all_unapplied=True)
        session.rollback()
    assert [fill.execution_id for fill in plan.fills] == [EXEC_ID]


def test_the_account_filter_is_enforced(tmp_path, capsys):
    url, engine = _book(tmp_path)
    _fund(engine)

    assert _cli(url, tmp_path, "--all-unapplied", "--account", "DU0000001") == 0
    assert "No unprojected fills" in capsys.readouterr().out

    assert _cli(
        url, tmp_path, "--execution-id", EXEC_ID, "--account", "DU0000001"
    ) == 1
    assert "no execution_fills row" in capsys.readouterr().out


def test_a_selection_is_required(tmp_path):
    url, _ = _book(tmp_path)
    with pytest.raises(SystemExit):
        _cli(url, tmp_path)


# --------------------------------------------------------------------------
# AC5. No TTY, or the wrong phrase, writes nothing.
# --------------------------------------------------------------------------


def test_apply_without_a_tty_is_refused(tmp_path, monkeypatch, capsys):
    url, engine = _book(tmp_path)
    _fund(engine)
    before = _state(engine)
    _confirm(monkeypatch, tty=False)

    assert _cli(url, tmp_path, "--execution-id", EXEC_ID, "--apply") == 2

    assert "TTY" in capsys.readouterr().err
    assert _state(engine) == before


def test_apply_with_the_wrong_phrase_is_refused(tmp_path, monkeypatch, capsys):
    url, engine = _book(tmp_path)
    _fund(engine)
    before = _state(engine)
    _confirm(monkeypatch, answer="yes")

    assert _cli(url, tmp_path, "--execution-id", EXEC_ID, "--apply") == 2

    assert CONFIRMATION in capsys.readouterr().err
    assert _state(engine) == before
    assert not (tmp_path / "reconciliation").exists()


def test_apply_reprojection_demands_the_phrase(tmp_path):
    _, engine = _book(tmp_path)
    _fund(engine)
    with Session(engine) as session:
        plan = plan_reprojection(session, execution_ids=[EXEC_ID])
        with pytest.raises(ReprojectRefusedError, match=CONFIRMATION):
            apply_reprojection(session, plan, confirm="yes")


def test_apply_reprojection_refuses_a_plan_with_problems(tmp_path):
    _, engine = _book(tmp_path)          # unfunded
    with Session(engine) as session:
        plan = plan_reprojection(session, execution_ids=[EXEC_ID])
        assert plan.problems
        with pytest.raises(ReprojectRefusedError, match="refus"):
            apply_reprojection(session, plan, confirm=CONFIRMATION)


# --------------------------------------------------------------------------
# Pre-checks mirroring restore_missed_entries' knowable rejections.
# --------------------------------------------------------------------------


def test_an_unowned_open_position_on_the_contract_is_named(tmp_path, capsys):
    url, engine = _book(tmp_path)
    _fund(engine)
    with Session(engine) as session:
        session.add(Position(
            account_id=None, ticker="ARKW", portfolio="momentum",
            con_id=ARKW_CON, exchange="SMART", currency="USD", quantity=1.0,
            avg_entry_price=160, current_price=160, peak_price=160,
            highest_price_since_entry=160, opened_at=CREATED, status="open",
        ))
        session.commit()

    assert _cli(url, tmp_path, "--execution-id", EXEC_ID) == 1
    assert "ownership is unresolved" in capsys.readouterr().out


def test_a_contract_identity_conflict_is_named(tmp_path, capsys):
    url, engine = _book(tmp_path)
    _fund(engine)
    with Session(engine) as session:
        session.add(Position(
            account_id=ACCOUNT, ticker="ARKW", portfolio=SLEEVE,
            con_id=ARKW_CON, exchange="ARCA", currency="USD", quantity=1.0,
            avg_entry_price=160, current_price=160, peak_price=160,
            highest_price_since_entry=160, opened_at=CREATED, status="open",
        ))
        session.commit()

    assert _cli(url, tmp_path, "--execution-id", EXEC_ID) == 1
    assert "contract identity conflicts" in capsys.readouterr().out


def test_a_live_account_is_refused(tmp_path, capsys):
    url, engine = _book(tmp_path)
    _fund(engine)
    with Session(engine) as session:
        session.execute(text(
            "UPDATE execution_fills SET account_id = 'U1234567'"
        ))
        session.execute(text(
            "UPDATE order_intents SET account_id = 'U1234567' "
            "WHERE recommendation_id = :r"
        ), {"r": REC_ARKW})
        session.commit()

    assert _cli(url, tmp_path, "--execution-id", EXEC_ID) == 1
    assert "paper account" in capsys.readouterr().out


def test_a_projector_validation_failure_is_named(tmp_path, capsys):
    """The projector's own ``_validate`` runs in the dry run: here the
    intent's order id no longer matches the recorded execution."""
    url, engine = _book(tmp_path)
    _fund(engine)
    with Session(engine) as session:
        session.execute(text(
            "UPDATE order_intents SET ib_order_id = '999' "
            "WHERE recommendation_id = :r"
        ), {"r": REC_ARKW})
        session.commit()

    assert _cli(url, tmp_path, "--execution-id", EXEC_ID) == 1
    assert "conflicts with durable intent on: order" in capsys.readouterr().out


# --------------------------------------------------------------------------
# AC6. The replayed 2026-10-07 scenario reconciles ok afterwards.
# --------------------------------------------------------------------------


def _ib_after_arkg_sale():
    """IB on 2026-10-08 after the ARKG sale: it holds the 11 ARKW the book
    did not know about, and no ARKG."""
    return SimpleNamespace(
        account_id=ACCOUNT,
        mode="paper",
        positions={ARKW_CON: BrokerPosition(
            account_id=ACCOUNT, con_id=ARKW_CON, symbol="ARKW", quantity=11.0,
        )},
        open_orders={},
    )


def test_reconciliation_reads_ok_after_the_repair(tmp_path, monkeypatch):
    url, engine = _book(tmp_path)
    _fund(engine)
    with Session(engine) as session:
        before, _ = reconcile_snapshot(session, _ib_after_arkg_sale())
        session.commit()
    assert before.severity == "major"
    assert {item["type"] for item in before.discrepancies} == {
        "missing_in_db", "unapplied_execution_fill",
    }

    _confirm(monkeypatch)
    assert _cli(url, tmp_path, "--execution-id", EXEC_ID, "--apply") == 0

    with Session(engine) as session:
        after, plan = reconcile_snapshot(session, _ib_after_arkg_sale())
        session.commit()
    assert after.discrepancies == []
    assert after.severity == "ok"
    assert after.entries_allowed is True
    assert plan.unresolved == []


# --------------------------------------------------------------------------
# The artifact never overwrites.
# --------------------------------------------------------------------------


def test_an_existing_artifact_is_never_overwritten(tmp_path):
    first = write_exclusive(tmp_path, "reproject-fill-20261008T000000Z", {"a": 1})
    second = write_exclusive(tmp_path, "reproject-fill-20261008T000000Z", {"a": 2})

    assert first != second
    assert json.loads(first.read_text()) == {"a": 1}
    assert json.loads(second.read_text()) == {"a": 2}
