from __future__ import annotations

import pytest

from services.api.auth import reset_lockout


@pytest.fixture(autouse=True)
def _reset_auth_lockout():
    """The auth lockout tracker is a process-wide singleton keyed by client
    address. TestClient always presents the same fake client address, so
    without a reset, failed-auth assertions in one test would accumulate and
    spuriously lock out unrelated tests later in the same session.
    """
    reset_lockout()
    yield
    reset_lockout()
