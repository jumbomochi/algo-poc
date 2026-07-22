from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from migrations.versions import c3a947f26510_track_fill_projection_outcome as migration


def migration_op(*, has_execution_fills: bool) -> MagicMock:
    operation = MagicMock()
    operation.get_bind.return_value.scalar.return_value = has_execution_fills
    return operation


def test_upgrade_adds_projection_marker_when_execution_fill_table_is_empty(
    monkeypatch,
):
    operation = migration_op(has_execution_fills=False)
    monkeypatch.setattr(migration, "op", operation)

    migration.upgrade()

    operation.add_column.assert_called_once()
    operation.alter_column.assert_called_once_with(
        "execution_fills", "projection_applied", server_default=None
    )


def test_upgrade_refuses_to_classify_preexisting_execution_fills(monkeypatch):
    operation = migration_op(has_execution_fills=True)
    monkeypatch.setattr(migration, "op", operation)

    with pytest.raises(RuntimeError, match="execution_fills must be empty"):
        migration.upgrade()

    operation.add_column.assert_not_called()
    operation.alter_column.assert_not_called()


def test_downgrade_removes_projection_marker(monkeypatch):
    operation = MagicMock()
    monkeypatch.setattr(migration, "op", operation)

    migration.downgrade()

    operation.drop_column.assert_called_once_with(
        "execution_fills", "projection_applied"
    )
