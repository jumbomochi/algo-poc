"""KAN-97: --report says why --apply-plan will refuse, entry by entry."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from scripts.reconcile_paper import report_hints
from services.execution.reconciliation import RepairPlan, UnresolvedRepair


def _plan(*unresolved):
    return RepairPlan(account_id="DUN551088",
                      created_at=datetime(2026, 10, 3, tzinfo=timezone.utc),
                      unresolved=list(unresolved))


def _result(*discrepancies):
    return SimpleNamespace(discrepancies=list(discrepancies))


def test_a_candidate_names_the_order_and_the_statement_restore():
    plan = _plan(UnresolvedRepair(
        reason="sleeve_mapping_required", con_id=322317077,
        candidate_recommendation_id="rec-xlc", candidate_portfolio="sector_rotation",
    ))
    [line] = report_hints(_result({"type": "missing_in_db", "con_id": 322317077}), plan)

    assert "rec-xlc" in line and "restore_missed_entries.py" in line


def test_a_candidate_less_missing_in_db_points_at_the_statement():
    plan = _plan(UnresolvedRepair(reason="sleeve_mapping_required", con_id=42))
    [line] = report_hints(_result({"type": "missing_in_db", "con_id": 42}), plan)

    assert "restore_missed_entries.py" in line


def test_any_other_unresolved_entry_names_its_reason():
    plan = _plan(UnresolvedRepair(
        reason="manual_order_or_fill_resolution_required", ib_order_id="219",
    ))
    [line] = report_hints(_result({"type": "order_missing_at_ib", "ib_order_id": "219"}), plan)

    assert "manual_order_or_fill_resolution_required" in line
    assert "219" in line
    assert "--apply-plan" in line and "refuse" in line


def test_a_plan_with_nothing_unresolved_prints_nothing():
    assert report_hints(_result(), _plan()) == []
