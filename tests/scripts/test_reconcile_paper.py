from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from scripts.reconcile_paper import (
    RepairAction,
    RepairPlan,
    RepairRefusedError,
    apply_repair_plan,
    persist_reconciliation_report,
    reconcile_snapshot,
    write_repair_plan,
)
from services.execution.reconciliation import PositionReconciler
from shared.broker_state import BrokerPosition
from shared.models import OrderIntent, OrderStatus, Position, ReconciliationReport
from shared.models.base import Base


NOW = datetime(2026, 7, 22, tzinfo=timezone.utc)
REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as value:
        yield value


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
