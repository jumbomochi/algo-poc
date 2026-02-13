from __future__ import annotations

from fastapi import APIRouter, Depends

from services.api.auth import APIUser, get_current_user

router = APIRouter(prefix="/api/v1/activity", tags=["activity"])


@router.get("/trades")
def get_recent_trades(
    user: APIUser = Depends(get_current_user),
) -> list[dict]:
    """Return recent trades."""
    return []


@router.get("/audit")
def get_audit_log(
    user: APIUser = Depends(get_current_user),
) -> list[dict]:
    """Return recent audit log entries."""
    return []
