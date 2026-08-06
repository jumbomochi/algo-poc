"""Tests for the .env permission checker.

These tests only ever touch files under pytest's tmp_path — never the
repo's real .env or the user's shell environment. The checker script is a
read-only diagnostic by default; --fix is opt-in and is exercised here only
against tmp_path fixtures, never real secrets.
"""

from __future__ import annotations

import os
import stat

import pytest

from scripts.ops.check_env_permissions import (
    PermissionCheckResult,
    check_permissions,
    fix_permissions,
)


def _chmod(path, mode: int) -> None:
    os.chmod(path, mode)


class TestCheckPermissions:
    def test_world_readable_env_file_is_flagged(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("API_KEYS=x:admin\n")
        _chmod(env_file, 0o644)  # group/world readable

        result = check_permissions(str(env_file))

        assert isinstance(result, PermissionCheckResult)
        assert result.secure is False
        assert "group" in result.message.lower() or "other" in result.message.lower()

    def test_owner_only_env_file_passes(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("API_KEYS=x:admin\n")
        _chmod(env_file, 0o600)  # owner read/write only

        result = check_permissions(str(env_file))

        assert result.secure is True

    def test_world_writable_env_file_is_flagged(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("API_KEYS=x:admin\n")
        _chmod(env_file, 0o606)

        result = check_permissions(str(env_file))

        assert result.secure is False

    def test_missing_env_file_reports_not_found(self, tmp_path):
        missing = tmp_path / "does-not-exist.env"

        result = check_permissions(str(missing))

        assert result.secure is False
        assert "not found" in result.message.lower()


class TestFixPermissions:
    def test_fix_permissions_sets_owner_only(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("API_KEYS=x:admin\n")
        _chmod(env_file, 0o644)

        fix_permissions(str(env_file))

        mode = stat.S_IMODE(os.stat(env_file).st_mode)
        assert mode == 0o600

    def test_fix_permissions_on_missing_file_raises(self, tmp_path):
        missing = tmp_path / "does-not-exist.env"

        with pytest.raises(FileNotFoundError):
            fix_permissions(str(missing))
