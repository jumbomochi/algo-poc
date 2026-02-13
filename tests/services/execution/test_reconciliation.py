from __future__ import annotations

import pytest

from services.execution.reconciliation import PositionReconciler, ReconciliationResult


class TestPositionReconciliation:
    def test_matching_positions_ok(self):
        """Matching positions should produce severity=ok."""
        reconciler = PositionReconciler()
        ib_positions = {"AAPL": 100, "MSFT": 50}
        db_positions = {"AAPL": 100, "MSFT": 50}

        result = reconciler.reconcile(ib_positions, db_positions)

        assert result.severity == "ok"
        assert len(result.matched) == 2
        assert len(result.discrepancies) == 0

    def test_small_quantity_difference_minor(self):
        """Quantity off by < 5% should be minor and auto-correct."""
        reconciler = PositionReconciler()
        ib_positions = {"AAPL": 100}
        db_positions = {"AAPL": 102}  # 2% difference

        result = reconciler.reconcile(ib_positions, db_positions)

        assert result.severity == "minor"
        assert len(result.discrepancies) == 1
        disc = result.discrepancies[0]
        assert disc["ticker"] == "AAPL"
        assert disc["type"] == "quantity_mismatch"
        assert disc["auto_correct"] is True

    def test_missing_position_in_ib_major(self):
        """Position in DB but not in IB should be major."""
        reconciler = PositionReconciler()
        ib_positions = {"AAPL": 100}
        db_positions = {"AAPL": 100, "MSFT": 50}

        result = reconciler.reconcile(ib_positions, db_positions)

        assert result.severity == "major"
        assert len(result.discrepancies) == 1
        disc = result.discrepancies[0]
        assert disc["ticker"] == "MSFT"
        assert disc["type"] == "missing_in_ib"

    def test_missing_position_in_db_major(self):
        """Position in IB but not in DB should be major."""
        reconciler = PositionReconciler()
        ib_positions = {"AAPL": 100, "MSFT": 50}
        db_positions = {"AAPL": 100}

        result = reconciler.reconcile(ib_positions, db_positions)

        assert result.severity == "major"
        assert len(result.discrepancies) == 1
        disc = result.discrepancies[0]
        assert disc["ticker"] == "MSFT"
        assert disc["type"] == "missing_in_db"

    def test_large_quantity_difference_major(self):
        """Quantity off by >= 5% should be major."""
        reconciler = PositionReconciler()
        ib_positions = {"AAPL": 100}
        db_positions = {"AAPL": 110}  # 10% difference

        result = reconciler.reconcile(ib_positions, db_positions)

        assert result.severity == "major"
        assert len(result.discrepancies) == 1
        disc = result.discrepancies[0]
        assert disc["ticker"] == "AAPL"
        assert disc["type"] == "quantity_mismatch"
        assert disc["auto_correct"] is False

    def test_empty_positions_ok(self):
        """Both empty positions should produce severity=ok."""
        reconciler = PositionReconciler()
        result = reconciler.reconcile({}, {})

        assert result.severity == "ok"
        assert len(result.matched) == 0
        assert len(result.discrepancies) == 0

    def test_reconciliation_result_dataclass(self):
        """ReconciliationResult should be a proper dataclass."""
        result = ReconciliationResult(
            matched=[{"ticker": "AAPL", "quantity": 100}],
            discrepancies=[],
            severity="ok",
        )
        assert result.severity == "ok"
        assert len(result.matched) == 1

    def test_mixed_minor_and_major_is_major(self):
        """If any discrepancy is major, overall severity should be major."""
        reconciler = PositionReconciler()
        ib_positions = {"AAPL": 100, "GOOG": 200}
        db_positions = {"AAPL": 102, "GOOG": 300}  # AAPL 2% minor, GOOG 33% major

        result = reconciler.reconcile(ib_positions, db_positions)

        assert result.severity == "major"
