from __future__ import annotations

import os

from fastapi import Depends, Header, HTTPException, status
from pydantic import BaseModel

from shared.logging import get_logger

logger = get_logger("api.auth")

# Roles ordered by privilege level (highest first).
ROLES = ("admin", "operator", "viewer")

ROLE_HIERARCHY: dict[str, int] = {role: idx for idx, role in enumerate(ROLES)}


class APIUser(BaseModel):
    api_key: str
    role: str


def _load_api_keys() -> dict[str, str]:
    """Load API key -> role mapping.

    Uses ``API_KEYS`` env var if set (format: ``key1:role1,key2:role2``),
    otherwise falls back to development defaults.
    """
    env_keys = os.environ.get("API_KEYS")
    if env_keys:
        mapping: dict[str, str] = {}
        for entry in env_keys.split(","):
            key, role = entry.strip().split(":")
            mapping[key.strip()] = role.strip()
        return mapping

    return {
        "test-key": "admin",
        "operator-key": "operator",
        "viewer-key": "viewer",
    }


# Singleton mapping, reloaded once at import time.
API_KEYS = _load_api_keys()


def get_current_user(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> APIUser:
    """FastAPI dependency that validates the ``X-API-Key`` header."""
    if x_api_key is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key",
        )
    role = API_KEYS.get(x_api_key)
    if role is None:
        logger.warning("auth_failed", api_key=x_api_key[:4] + "***")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )
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
