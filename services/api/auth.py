from __future__ import annotations

import os
import time
from collections import defaultdict

from fastapi import Depends, Header, HTTPException, Request, status
from pydantic import BaseModel

from shared.config import load_config
from shared.logging import get_logger

logger = get_logger("api.auth")

# Roles ordered by privilege level (highest first).
ROLES = ("admin", "operator", "viewer")

ROLE_HIERARCHY: dict[str, int] = {role: idx for idx, role in enumerate(ROLES)}


class APIUser(BaseModel):
    api_key: str
    role: str


# Path to the app config, matching the convention used by every service
# runner (`load_config("config/default.yaml")`) — see shared/config.py.
CONFIG_PATH = "config/default.yaml"


def resolve_mode() -> str:
    """Resolve the running mode from the validated ``AppConfig``.

    Deliberately goes through ``AppConfig.mode`` (a ``Literal["paper",
    "live", "backtest"]``) rather than reading ``ALGO_MODE`` directly:
    - A typo'd ``ALGO_MODE`` fails config validation at startup instead of
      silently resolving to "not live" here.
    - ``mode: live`` set only in the YAML config file (no env var override)
      is honored too — the old raw-env-var check missed this entirely.
    """
    return load_config(CONFIG_PATH).mode


def _load_api_keys(mode: str | None = None) -> dict[str, str]:
    """Load API key -> role mapping.

    Uses ``API_KEYS`` env var if set (format: ``key1:role1,key2:role2``),
    otherwise falls back to development defaults. The fallback is refused in
    live mode: an internet-reachable kill switch must never accept "test-key".

    Args:
        mode: The resolved app mode. Defaults to ``AppConfig.mode`` (via
            ``resolve_mode()``); tests may pass this explicitly.
    """
    env_keys = os.environ.get("API_KEYS")
    if env_keys:
        mapping: dict[str, str] = {}
        for entry in env_keys.split(","):
            entry = entry.strip()
            if not entry:
                continue
            key, sep, role = entry.partition(":")
            if not sep or not key.strip() or role.strip() not in ROLES:
                raise ValueError(
                    f"Malformed API_KEYS entry {entry!r}: expected 'key:role' "
                    f"with role in {ROLES}"
                )
            mapping[key.strip()] = role.strip()
        if not mapping:
            raise ValueError("API_KEYS is set but contains no valid entries")
        return mapping

    if (mode if mode is not None else resolve_mode()) == "live":
        raise RuntimeError(
            "API_KEYS must be set in live mode; refusing to start with "
            "development default keys"
        )
    logger.warning(
        "api_keys_using_dev_defaults",
        hint="set API_KEYS=key:role[,key:role] for real deployments",
    )
    return {
        "test-key": "admin",
        "operator-key": "operator",
        "viewer-key": "viewer",
    }


# Singleton mapping, reloaded once at import time.
API_KEYS = _load_api_keys()


# Lockout thresholds for repeated X-API-Key failures. The kill endpoint is
# internet-reachable and admin-gated purely by this key, so an unbounded
# guessing loop must be stopped, not just logged.
LOCKOUT_MAX_FAILURES = 5
LOCKOUT_WINDOW_SECONDS = 60.0
LOCKOUT_DURATION_SECONDS = 300.0


class _LockoutTracker:
    """Tracks auth failures per client address and locks out repeat offenders.

    In-memory and per-process: this is adequate for the single-process API
    deployment this repo documents (no multi-instance API scaling exists
    today). If the API is ever scaled horizontally, this state needs to move
    to a shared store (e.g. Redis) to stay effective across instances.
    """

    def __init__(
        self,
        max_failures: int = LOCKOUT_MAX_FAILURES,
        window_seconds: float = LOCKOUT_WINDOW_SECONDS,
        lockout_seconds: float = LOCKOUT_DURATION_SECONDS,
    ):
        self._max_failures = max_failures
        self._window_seconds = window_seconds
        self._lockout_seconds = lockout_seconds
        self._failures: dict[str, list[float]] = defaultdict(list)
        self._locked_until: dict[str, float] = {}

    def is_locked_out(self, client_id: str) -> bool:
        locked_until = self._locked_until.get(client_id)
        if locked_until is None:
            return False
        if time.monotonic() >= locked_until:
            # Lockout expired: clear it so the client gets a clean slate.
            self._locked_until.pop(client_id, None)
            self._failures.pop(client_id, None)
            return False
        return True

    def record_failure(self, client_id: str) -> None:
        now = time.monotonic()
        recent = [
            t for t in self._failures[client_id] if now - t < self._window_seconds
        ]
        recent.append(now)
        self._failures[client_id] = recent
        if len(recent) >= self._max_failures:
            self._locked_until[client_id] = now + self._lockout_seconds

    def record_success(self, client_id: str) -> None:
        self._failures.pop(client_id, None)
        self._locked_until.pop(client_id, None)

    def reset(self) -> None:
        self._failures.clear()
        self._locked_until.clear()


_lockout = _LockoutTracker()


def reset_lockout() -> None:
    """Clear all lockout state. Exposed for tests, which reuse the same
    process-wide tracker across many TestClient instances that all appear to
    come from the same client address.
    """
    _lockout.reset()


def get_current_user(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> APIUser:
    """FastAPI dependency that validates the ``X-API-Key`` header."""
    client_id = request.client.host if request.client else "unknown"

    if _lockout.is_locked_out(client_id):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed authentication attempts; try again later",
        )

    if x_api_key is None:
        _lockout.record_failure(client_id)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key",
        )
    role = API_KEYS.get(x_api_key)
    if role is None:
        _lockout.record_failure(client_id)
        logger.warning("auth_failed", api_key=x_api_key[:4] + "***")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )
    _lockout.record_success(client_id)
    return APIUser(api_key=x_api_key, role=role)


def require_role(role: str):
    """Return a FastAPI dependency that enforces a minimum role level.

    Role hierarchy: admin > operator > viewer.
    """
    required_level = ROLE_HIERARCHY.get(role)
    if required_level is None:
        raise ValueError(f"Unknown role: {role}")

    def _checker(
        user: APIUser = Depends(get_current_user),
    ) -> APIUser:
        user_level = ROLE_HIERARCHY.get(user.role, len(ROLES))
        # Lower index means higher privilege.
        if user_level > required_level:
            logger.warning(
                "access_denied",
                user_role=user.role,
                required_role=role,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{role}' or higher required",
            )
        return user

    return _checker
