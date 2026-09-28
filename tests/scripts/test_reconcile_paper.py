from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from scripts.reconcile_paper import (
    MissingExitPriceError,
    RepairAction,
    RepairPlan,
    RepairRefusedError,
    _read_broker_snapshot,
    apply_repair_plan,
    persist_reconciliation_report,
    reconcile_snapshot,
    write_repair_plan,
)
from services.execution.reconciliation import PositionReconciler
from shared.broker_state import BrokerPosition
from shared.models import (
    ExecutionFill,
    OrderIntent,
    OrderStatus,
    Position,
    ReconciliationReport,
)
from shared.models.portfolio import Trade
from shared.models.portfolio_config import PortfolioConfig
from shared.models.base import Base

NOW = datetime(2026, 7, 22, tzinfo=timezone.utc)
REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as value:
        yield value


@pytest.mark.asyncio
async def test_reconciliation_reader_passes_explicit_currency_configuration(
    monkeypatch,
):
    ib = MagicMock()
    ib.connectAsync = AsyncMock()
    ib.isConnected.return_value = True
    monkeypatch.setitem(sys.modules, "ib_insync", SimpleNamespace(IB=lambda: ib))
    snapshot = object()
    reader = MagicMock()
    reader.snapshot = AsyncMock(return_value=snapshot)
    args = SimpleNamespace(ib_host="127.0.0.1", ib_port=7497, ib_client_id=57)

    with patch(
        "scripts.reconcile_paper.IBAccountReader", return_value=reader
    ) as reader_class:
        result = await _read_broker_snapshot(
            args,
            "paper",
            expected_base_currency="SGD",
            trading_currency="USD",
        )

    assert result is snapshot
    reader_class.assert_called_once_with(
        ib,
        expected_mode="paper",
        expected_base_currency="SGD",
        trading_currency="USD",
    )
    reader.snapshot.assert_awaited_once_with()
    ib.disconnect.assert_called_once_with()


def _plan(*, unresolved=(), account_id="DUN551088") -> RepairPlan:
    return RepairPlan(
        account_id=account_id,
        created_at=NOW,
        actions=[
            RepairAction(
                action="set_position_quantity",
                account_id=account_id,
                portfolio="momentum",
                con_id=265598,
                quantity=10,
            )
        ],
        unresolved=list(unresolved),
    )


def test_report_is_persisted_as_json_without_position_mutation(session):
    session.add(Position(
        account_id="DUN551088", ticker="AAPL", portfolio="momentum", con_id=265598,
        exchange="SMART", currency="USD", quantity=9,
        avg_entry_price=100, current_price=100, peak_price=100,
        highest_price_since_entry=100, opened_at=NOW, status="open",
    ))
    session.commit()
    result = PositionReconciler(account_id="DUN551088").reconcile(
        broker_positions={265598: 10}, db_positions={265598: 9},
        broker_orders={}, db_orders={},
    )

    report = persist_reconciliation_report(
        session, account_id="DUN551088", mode="paper", result=result
    )
    session.commit()

    assert session.scalar(select(Position)).quantity == 9
    stored = session.get(ReconciliationReport, report.id)
    assert stored.entries_allowed is False
    assert stored.result["discrepancies"][0]["con_id"] == 265598


def test_report_ignores_active_intents_from_other_accounts(session):
    session.add(OrderIntent(
        recommendation_id="other-rec", account_id="DUOTHER", mode="paper",
        portfolio="momentum", con_id=265598, symbol="AAPL",
        exchange="SMART", currency="USD", action="BUY",
        requested_quantity=1, limit_price=100, order_type="LMT",
        reserved_notional=100, filled_quantity=0,
        status=OrderStatus.SUBMITTED.value, ib_order_id="99",
        created_at=NOW, updated_at=NOW,
    ))
    session.commit()
    snapshot = SimpleNamespace(
        account_id="DUN551088", mode="paper", positions={}, open_orders={}
    )

    result, _ = reconcile_snapshot(session, snapshot)

    assert result.entries_allowed is True


def _owned_position(account_id):
    return Position(
        account_id=account_id, ticker="AAPL", portfolio="momentum",
        con_id=265598, exchange="SMART", currency="USD", quantity=10,
        avg_entry_price=100, current_price=100, peak_price=100,
        highest_price_since_entry=100, opened_at=NOW, status="open",
    )


def test_report_reconciles_only_connected_account_positions(session):
    session.add_all([_owned_position("DUN551088"), _owned_position("DUOTHER")])
    session.commit()
    snapshot = SimpleNamespace(
        account_id="DUN551088", mode="paper",
        positions={265598: BrokerPosition(
            account_id="DUN551088", con_id=265598, symbol="AAPL", quantity=10,
        )},
        open_orders={},
    )

    result, _ = reconcile_snapshot(session, snapshot)

    assert result.entries_allowed is True


def test_report_fails_closed_on_unowned_legacy_open_position(session):
    session.add(_owned_position(None))
    session.commit()
    snapshot = SimpleNamespace(
        account_id="DUN551088", mode="paper", positions={}, open_orders={}
    )

    result, _ = reconcile_snapshot(session, snapshot)

    assert result.entries_allowed is False
    assert any(
        item["type"] == "db_position_missing_account_id"
        for item in result.discrepancies
    )


def test_write_plan_uses_json_round_trip(tmp_path):
    path = write_repair_plan(_plan(), output_dir=tmp_path)
    payload = json.loads(path.read_text())

    assert path.parent == tmp_path
    assert payload["account_id"] == "DUN551088"
    assert payload["actions"][0]["action"] == "set_position_quantity"


def test_cli_is_importable_when_invoked_as_a_script():
    result = subprocess.run(
        [sys.executable, "scripts/reconcile_paper.py", "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "--apply-plan" in result.stdout


def test_apply_refuses_non_tty(monkeypatch, tmp_path, session):
    plan_path = write_repair_plan(_plan(), output_dir=tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    with pytest.raises(RepairRefusedError, match="TTY"):
        apply_repair_plan(session, plan_path=plan_path)


def test_apply_refuses_unresolved_mappings(monkeypatch, tmp_path, session):
    plan = _plan(unresolved=[
        {"reason": "sleeve_mapping_required", "con_id": 265598}
    ])
    plan_path = write_repair_plan(plan, output_dir=tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    with pytest.raises(RepairRefusedError, match="unresolved"):
        apply_repair_plan(session, plan_path=plan_path)


def test_apply_refuses_live_account_before_backup(monkeypatch, tmp_path, session):
    plan_path = write_repair_plan(_plan(account_id="U17723819"), output_dir=tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    with pytest.raises(RepairRefusedError, match="paper account"):
        apply_repair_plan(session, plan_path=plan_path)
    assert not list(tmp_path.glob("paper_state_pre_repair_*.json"))


def test_apply_requires_exact_confirmation_and_does_not_mutate(monkeypatch, tmp_path, session):
    session.add(Position(
        ticker="AAPL", portfolio="momentum", con_id=265598,
        exchange="SMART", currency="USD", quantity=9,
        avg_entry_price=100, current_price=100, peak_price=100,
        highest_price_since_entry=100, opened_at=NOW, status="open",
    ))
    session.commit()
    plan_path = write_repair_plan(_plan(), output_dir=tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "no")

    with pytest.raises(RepairRefusedError, match="confirmation"):
        apply_repair_plan(session, plan_path=plan_path)

    assert session.scalar(select(Position)).quantity == 9
    assert len(list(tmp_path.glob("paper_state_pre_repair_*.json"))) == 1


def test_apply_executes_only_serialized_action_and_commits_once(monkeypatch, tmp_path, session):
    session.add(Position(
        account_id="DUN551088", ticker="AAPL", portfolio="momentum", con_id=265598,
        exchange="SMART", currency="USD", quantity=9,
        avg_entry_price=100, current_price=100, peak_price=100,
        highest_price_since_entry=100, opened_at=NOW, status="open",
    ))
    session.commit()
    plan_path = write_repair_plan(_plan(), output_dir=tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "APPLY PAPER REPAIR")
    commits = 0
    original_commit = session.commit

    def counting_commit():
        nonlocal commits
        commits += 1
        original_commit()

    monkeypatch.setattr(session, "commit", counting_commit)
    apply_repair_plan(session, plan_path=plan_path)

    assert commits == 1
    assert session.scalar(select(Position)).quantity == 10
    assert len(list(tmp_path.glob("paper_state_pre_repair_*.json"))) == 1


def test_apply_refuses_target_ambiguous_with_unowned_legacy_row(
    monkeypatch, tmp_path, session
):
    session.add_all([_owned_position("DUN551088"), _owned_position(None)])
    session.commit()
    plan_path = write_repair_plan(_plan(), output_dir=tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "APPLY PAPER REPAIR")

    with pytest.raises(RepairRefusedError, match="unowned"):
        apply_repair_plan(session, plan_path=plan_path)

    assert sorted(position.quantity for position in session.scalars(select(Position))) == [10, 10]


@pytest.mark.parametrize(
    ("legacy_portfolio", "legacy_ticker"),
    [
        ("other_sleeve", "AAPL"),
        ("other_sleeve", "AAPL.A"),
    ],
)
def test_apply_refuses_unowned_contract_across_sleeves_and_ticker_aliases(
    legacy_portfolio, legacy_ticker, monkeypatch, tmp_path, session
):
    owned = _owned_position("DUN551088")
    legacy = _owned_position(None)
    legacy.portfolio = legacy_portfolio
    legacy.ticker = legacy_ticker
    session.add_all([owned, legacy])
    session.commit()
    plan_path = write_repair_plan(_plan(), output_dir=tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "APPLY PAPER REPAIR")

    with pytest.raises(RepairRefusedError, match="unowned"):
        apply_repair_plan(session, plan_path=plan_path)

    assert owned.quantity == 10
    assert legacy.quantity == 10


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload["actions"][0].update(quantity=True),
        lambda payload: payload["actions"][0].update(quantity=float("nan")),
        lambda payload: payload["actions"][0].update(quantity=float("inf")),
        lambda payload: payload["actions"][0].update(quantity=-1),
        lambda payload: payload["actions"][0].update(con_id=True),
        lambda payload: payload["actions"][0].update(con_id=0),
        lambda payload: payload["actions"][0].update(con_id=1.5),
        lambda payload: payload["actions"][0].update(portfolio=""),
        lambda payload: payload["actions"][0].update(portfolio="bad space"),
        lambda payload: payload["actions"][0].update(action="delete_position"),
        lambda payload: payload["actions"][0].update(account_id="DUOTHER"),
        lambda payload: payload["actions"][0].update(extra="not-reviewed"),
        lambda payload: payload["actions"][0].pop("quantity"),
        lambda payload: payload.update(extra="not-reviewed"),
    ],
)
def test_malformed_plan_is_refused_before_backup_or_database_access(
    mutate, monkeypatch, tmp_path, session
):
    plan_path = write_repair_plan(_plan(), output_dir=tmp_path)
    payload = json.loads(plan_path.read_text())
    mutate(payload)
    plan_path.write_text(json.dumps(payload))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(session, "execute", lambda *_args, **_kwargs: pytest.fail("DB accessed"))

    with pytest.raises(RepairRefusedError, match="invalid repair plan"):
        apply_repair_plan(session, plan_path=plan_path)

    assert not list(tmp_path.glob("paper_state_pre_repair_*.json"))


@pytest.mark.parametrize("path_kind", ["missing", "directory"])
def test_unreadable_plan_path_is_repair_refusal(path_kind, monkeypatch, tmp_path, session):
    plan_path = tmp_path / "missing.json"
    if path_kind == "directory":
        plan_path.mkdir()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    with pytest.raises(RepairRefusedError, match="repair plan"):
        apply_repair_plan(session, plan_path=plan_path)


# --- KAN-85: a missed exit is not a phantom -------------------------------

LLY_CON_ID = 9160
PAPER = "DUN551088"


def _lly_position(quantity=2.0):
    return Position(
        account_id=PAPER, ticker="LLY", portfolio="sector_rotation",
        con_id=LLY_CON_ID, exchange="SMART", currency="USD",
        quantity=quantity, avg_entry_price=1170.64, current_price=1115.70,
        peak_price=1170.64, highest_price_since_entry=1170.64,
        opened_at=datetime(2026, 7, 30, 13, 31, tzinfo=timezone.utc),
        status="open",
    )


def _fill(side, quantity, execution_id, price=1170.64):
    return ExecutionFill(
        account_id=PAPER, execution_id=execution_id, ib_order_id="19",
        portfolio="sector_rotation", con_id=LLY_CON_ID, symbol="LLY",
        exchange="SMART", currency="USD", side=side, quantity=quantity,
        price=price, commission=0.0,
        executed_at=datetime(2026, 7, 30, 13, 31, tzinfo=timezone.utc),
        projection_applied=True,
    )


def _sleeve(cash=1000.0):
    return PortfolioConfig(
        portfolio="sector_rotation", capital=5000.0, cash=cash,
        created_at=NOW, updated_at=NOW,
    )


def _empty_broker():
    return SimpleNamespace(
        account_id=PAPER, mode="paper", positions={}, open_orders={}
    )


def test_filled_buy_without_offsetting_sell_is_classified_missed_exit(session):
    session.add_all([
        _lly_position(),
        _fill("BUY", 2.0, "0000dc8f.6b2d21b5.01.01"),
    ])
    session.commit()

    _, plan = reconcile_snapshot(session, _empty_broker())

    assert plan.unresolved == []
    [action] = plan.actions
    assert action.action == "close_position_with_fill"
    assert action.classification == "missed_exit"
    assert action.con_id == LLY_CON_ID
    assert action.portfolio == "sector_rotation"
    assert action.quantity == 2.0
    # The exit price is not recoverable from the snapshot; it is left for
    # the operator, never defaulted.
    assert action.price is None
    assert action.executed_at is None
    assert action.evidence["buy_execution_ids"] == ["0000dc8f.6b2d21b5.01.01"]
    assert action.evidence["buy_quantity"] == 2.0
    assert action.evidence["sell_quantity"] == 0.0
    assert action.evidence["price_source"] == "operator_required"
    assert action.execution_id is None
    assert action.commission is None


def test_no_fill_history_is_classified_phantom_and_unchanged(session, tmp_path):
    session.add(_lly_position())
    session.commit()

    _, plan = reconcile_snapshot(session, _empty_broker())

    assert plan.actions == [RepairAction(
        action="set_position_quantity", account_id=PAPER,
        portfolio="sector_rotation", con_id=LLY_CON_ID, quantity=0.0,
    )]
    payload = json.loads(write_repair_plan(plan, output_dir=tmp_path).read_text())
    assert set(payload["actions"][0]) == {
        "action", "account_id", "portfolio", "con_id", "quantity"
    }


def test_unprojected_fill_on_file_is_not_guessed(session):
    unprojected = _fill("SELL", 2.0, "sell-1", price=1115.70)
    unprojected.projection_applied = False
    session.add_all([_lly_position(), _fill("BUY", 2.0, "buy-1"), unprojected])
    session.commit()

    _, plan = reconcile_snapshot(session, _empty_broker())

    assert plan.actions == []
    assert plan.unresolved[0].reason == "recorded_fill_not_projected"


def test_fill_history_that_nets_flat_is_a_phantom(session):
    session.add_all([
        _lly_position(),
        _fill("BUY", 2.0, "buy-1"),
        _fill("SELL", 2.0, "sell-1", price=1115.70),
    ])
    session.commit()

    _, plan = reconcile_snapshot(session, _empty_broker())

    assert [a.action for a in plan.actions] == ["set_position_quantity"]


_REST = {"execution_id": "statement-lly-20260828", "commission": 1.05}
_COMPLETE = {
    "price": 1100.0, "executed_at": "2026-08-28T13:30:00+00:00", **_REST,
}


def _missed_exit_plan(tmp_path, session, **overrides):
    session.add_all([
        _lly_position(), _sleeve(), _fill("BUY", 2.0, "buy-1"),
    ])
    session.commit()
    _, plan = reconcile_snapshot(session, _empty_broker())
    path = write_repair_plan(plan, output_dir=tmp_path)
    payload = json.loads(path.read_text())
    payload["actions"][0].update(overrides)
    path.write_text(json.dumps(payload))
    return path


def test_applying_missed_exit_writes_one_sell_trade_and_moves_cash(
    monkeypatch, tmp_path, session
):
    path = _missed_exit_plan(
        tmp_path, session, price=1100.0,
        executed_at="2026-08-28T13:30:00+00:00", commission=1.05,
        execution_id="statement-lly-20260828",
    )
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "APPLY PAPER REPAIR")

    apply_repair_plan(session, plan_path=path)

    [trade] = session.scalars(select(Trade)).all()
    assert trade.side == "sell"
    assert trade.ticker == "LLY"
    assert trade.portfolio == "sector_rotation"
    assert trade.quantity == 2.0
    assert trade.price == 1100.0
    assert trade.pnl == pytest.approx((1100.0 - 1170.64) * 2.0)
    assert trade.exit_reason.startswith("reconciliation repair")
    assert "statement-lly-20260828" in trade.exit_reason
    assert trade.executed_at.replace(tzinfo=timezone.utc) == datetime(
        2026, 8, 28, 13, 30, tzinfo=timezone.utc
    )
    sleeve = session.scalar(select(PortfolioConfig))
    assert sleeve.cash == pytest.approx(1000.0 + 1100.0 * 2.0 - 1.05)
    assert session.scalars(
        select(Position).where(Position.status == "open")
    ).all() == []
    assert len(list(tmp_path.glob("paper_state_pre_repair_*.json"))) == 1


def test_missed_exit_without_price_is_refused_not_defaulted(
    monkeypatch, tmp_path, session
):
    path = _missed_exit_plan(
        tmp_path, session, executed_at="2026-08-28T13:30:00+00:00"
    )
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(
        "builtins.input", lambda _: pytest.fail("confirmation reached")
    )

    with pytest.raises(MissingExitPriceError, match="price"):
        apply_repair_plan(session, plan_path=path)

    assert session.scalars(select(Trade)).all() == []
    assert session.scalar(select(Position)).quantity == 2.0
    assert session.scalar(select(PortfolioConfig)).cash == 1000.0
    assert not list(tmp_path.glob("paper_state_pre_repair_*.json"))


def test_missed_exit_without_executed_at_is_refused(
    monkeypatch, tmp_path, session
):
    path = _missed_exit_plan(tmp_path, session, price=1100.0)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    with pytest.raises(RepairRefusedError, match="executed_at"):
        apply_repair_plan(session, plan_path=path)

    assert session.scalars(select(Trade)).all() == []


def test_missed_exit_leaving_a_residue_open_is_refused(
    monkeypatch, tmp_path, session
):
    path = _missed_exit_plan(
        tmp_path, session, price=1100.0, quantity=1.5,
        executed_at="2026-08-28T13:30:00+00:00", **_REST,
    )
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "APPLY PAPER REPAIR")

    with pytest.raises(RepairRefusedError, match="residue"):
        apply_repair_plan(session, plan_path=path)

    assert session.scalars(select(Trade)).all() == []
    assert session.scalar(select(Position)).quantity == 2.0


@pytest.mark.parametrize(
    "overrides",
    [
        {"price": 0},
        {"price": -1.0},
        {"price": float("nan")},
        {"price": True},
        {"executed_at": "2026-08-28T13:30:00"},
        {"executed_at": "yesterday"},
        {"commission": -1.0},
        {"execution_id": ""},
        {"classification": "phantom"},
        {"evidence": "trust me"},
    ],
)
def test_malformed_missed_exit_action_is_refused(
    overrides, monkeypatch, tmp_path, session
):
    path = _missed_exit_plan(tmp_path, session, **overrides)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    with pytest.raises(RepairRefusedError, match="invalid repair plan"):
        apply_repair_plan(session, plan_path=path)

    assert not list(tmp_path.glob("paper_state_pre_repair_*.json"))


def test_zero_quantity_set_is_refused_for_a_position_with_fill_history(
    monkeypatch, tmp_path, session
):
    # A plan written before KAN-85 (or hand-edited back to the old action)
    # must not write off a position that was really bought.
    session.add_all([_lly_position(), _sleeve(), _fill("BUY", 2.0, "buy-1")])
    session.commit()
    plan = RepairPlan(
        account_id=PAPER, created_at=NOW,
        actions=[RepairAction(
            action="set_position_quantity", account_id=PAPER,
            portfolio="sector_rotation", con_id=LLY_CON_ID, quantity=0.0,
        )],
    )
    path = write_repair_plan(plan, output_dir=tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "APPLY PAPER REPAIR")

    with pytest.raises(RepairRefusedError, match="fill history"):
        apply_repair_plan(session, plan_path=path)

    assert session.scalar(select(Position)).quantity == 2.0


@pytest.mark.parametrize(
    ("guard", "match"),
    [
        ("tty", "TTY"),
        ("unresolved", "unresolved"),
        ("live", "paper account"),
        ("confirmation", "confirmation"),
    ],
)
def test_hard_guards_still_refuse_a_missed_exit_plan(
    guard, match, monkeypatch, tmp_path, session
):
    path = _missed_exit_plan(
        tmp_path, session, price=1100.0,
        executed_at="2026-08-28T13:30:00+00:00", **_REST,
    )
    payload = json.loads(path.read_text())
    if guard == "unresolved":
        payload["unresolved"] = [{"reason": "sleeve_mapping_required"}]
    if guard == "live":
        payload["account_id"] = "U17723819"
        payload["actions"][0]["account_id"] = "U17723819"
    path.write_text(json.dumps(payload))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: guard != "tty")
    monkeypatch.setattr("builtins.input", lambda _: "no")

    with pytest.raises(RepairRefusedError, match=match):
        apply_repair_plan(session, plan_path=path)

    assert session.scalars(select(Trade)).all() == []
    assert session.scalar(select(Position)).quantity == 2.0


@pytest.mark.parametrize("missing", ["execution_id", "commission"])
def test_missed_exit_without_statement_field_is_refused(
    missing, monkeypatch, tmp_path, session
):
    fields = {**_COMPLETE}
    del fields[missing]
    path = _missed_exit_plan(tmp_path, session, **fields)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    with pytest.raises(RepairRefusedError, match=missing):
        apply_repair_plan(session, plan_path=path)

    assert session.scalars(select(Trade)).all() == []
    assert not list(tmp_path.glob("paper_state_pre_repair_*.json"))


def test_missed_exit_already_recorded_execution_is_refused(
    monkeypatch, tmp_path, session
):
    session.add(_fill("SELL", 1.0, "statement-lly-20260828", price=1100.0))
    session.commit()
    path = _missed_exit_plan(tmp_path, session, **_COMPLETE)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "APPLY PAPER REPAIR")

    with pytest.raises(RepairRefusedError, match="already recorded"):
        apply_repair_plan(session, plan_path=path)

    assert session.scalars(select(Trade)).all() == []


def test_repaired_exit_leaves_fill_history_flat_for_later_phantoms(
    monkeypatch, tmp_path, session
):
    path = _missed_exit_plan(tmp_path, session, **_COMPLETE)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "APPLY PAPER REPAIR")
    apply_repair_plan(session, plan_path=path)

    [sell] = session.scalars(
        select(ExecutionFill).where(ExecutionFill.side == "SELL")
    ).all()
    assert sell.execution_id == "statement-lly-20260828"
    assert sell.quantity == 2.0
    assert sell.projection_applied is True

    # The book reconciles clean, and a later phantom on the same contract is
    # still a phantom rather than another "missed exit".
    result, _ = reconcile_snapshot(session, _empty_broker())
    assert result.entries_allowed is True
    session.add(_lly_position(quantity=1.0))
    session.commit()
    _, plan = reconcile_snapshot(session, _empty_broker())
    assert [a.action for a in plan.actions] == ["set_position_quantity"]
