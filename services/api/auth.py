from __future__ import annotations

import os
import time
from collections import OrderedDict
from collections.abc import Callable

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
# guessing loop against one specific wrong key must be stopped — but this
# must never cost the operator access to the kill switch. See
# get_current_user() for how that's enforced: a valid key always succeeds
# before lockout state is even consulted, and a missing header is never
# counted as a guess.
LOCKOUT_MAX_FAILURES = 5
LOCKOUT_WINDOW_SECONDS = 60.0
LOCKOUT_DURATION_SECONDS = 300.0
# Hard cap on distinct tracked buckets, to bound memory under a
# distributed/many-address attack — see _LockoutTracker.
LOCKOUT_MAX_TRACKED_KEYS = 10_000


class _LockoutTracker:
    """Tracks auth failures per lockout bucket and locks out repeat offenders.

    Callers choose the bucket key (``client_id``) — see
    ``get_current_user()``, which keys on ``(address, presented-key-prefix)``
    rather than address alone, so one client guessing many different wrong
    keys can't lock out every other client behind the same reverse proxy,
    and so a bucket only ever accumulates failures for one specific wrong
    guess.

    In-memory and per-process: this is adequate for the single-process API
    deployment this repo documents (no multi-instance API scaling exists
    today). If the API is ever scaled horizontally, this state needs to move
    to a shared store (e.g. Redis) to stay effective across instances.

    Bounded: entries with no failures left inside the window and no active
    lockout are swept on every ``record_failure`` call, and the tracker
    hard-caps at ``max_tracked_keys`` buckets (evicting the
    least-recently-active one) so an attack from many distinct buckets can't
    grow this structure without bound.
    """

    def __init__(
        self,
        max_failures: int = LOCKOUT_MAX_FAILURES,
        window_seconds: float = LOCKOUT_WINDOW_SECONDS,
        lockout_seconds: float = LOCKOUT_DURATION_SECONDS,
        max_tracked_keys: int = LOCKOUT_MAX_TRACKED_KEYS,
        now_fn: Callable[[], float] = time.monotonic,
    ):
        self._max_failures = max_failures
        self._window_seconds = window_seconds
        self._lockout_seconds = lockout_seconds
        self._max_tracked_keys = max_tracked_keys
        self._now = now_fn
        # Ordered so eviction can drop the least-recently-touched bucket.
        self._failures: OrderedDict[str, list[float]] = OrderedDict()
        self._locked_until: dict[str, float] = {}

    def _forget(self, client_id: str) -> None:
        self._failures.pop(client_id, None)
        self._locked_until.pop(client_id, None)

    def is_locked_out(self, client_id: str) -> bool:
        locked_until = self._locked_until.get(client_id)
        if locked_until is None:
            return False
        if self._now() >= locked_until:
            # Lockout expired: clear it so the client gets a clean slate.
            self._forget(client_id)
            return False
        return True

    def record_failure(self, client_id: str) -> None:
        now = self._now()

        recent = [
            t for t in self._failures.get(client_id, [])
            if now - t < self._window_seconds
        ]
        recent.append(now)
        self._failures[client_id] = recent
        self._failures.move_to_end(client_id)
        if len(recent) >= self._max_failures:
            self._locked_until[client_id] = now + self._lockout_seconds

        self._sweep(now)

    def _sweep(self, now: float) -> None:
        """Drop buckets that are neither actively failing nor locked out,
        then enforce the hard cap by evicting the oldest survivors.
        """
        for client_id in list(self._failures.keys()):
            recent = [
                t for t in self._failures[client_id] if now - t < self._window_seconds
            ]
            if recent:
                self._failures[client_id] = recent
            elif client_id not in self._locked_until:
                del self._failures[client_id]

        while len(self._failures) > self._max_tracked_keys:
            oldest_id, _ = self._failures.popitem(last=False)
            self._locked_until.pop(oldest_id, None)

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
    """FastAPI dependency that validates the ``X-API-Key`` header.

    Validity is checked before any lockout state: a request presenting a
    genuinely valid key always succeeds, full stop — the kill switch this
    guards must stay reachable for the operator no matter how many
    unrelated failures came from the same address. A missing header isn't
    a guess and never touches lockout state either; only a *specific wrong
    key* accumulates failures, bucketed by ``(address, key prefix)`` so one
    attacker trying many different wrong keys can't lock out other clients
    sharing that address (e.g. behind a reverse proxy).
    """
    if x_api_key is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key",
        )

    role = API_KEYS.get(x_api_key)
    if role is not None:
        return APIUser(api_key=x_api_key, role=role)

    address = request.client.host if request.client else "unknown"
    # Bucketed by the wrong key's own prefix (not the full key — this
    # value is logged) so distinct wrong guesses from one address don't
    # share a lockout budget.
    bucket = f"{address}:{x_api_key[:4]}"

    if _lockout.is_locked_out(bucket):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed authentication attempts; try again later",
        )

    _lockout.record_failure(bucket)
    logger.warning("auth_failed", api_key=x_api_key[:4] + "***")
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing API key",
    )


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
