from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends

from services.api.auth import APIUser, require_role
from shared.logging import get_logger

logger = get_logger("api.kill")

router = APIRouter(prefix="/api/v1/kill", tags=["kill"])


@router.post("")
def trigger_kill_switch(
    user: APIUser = Depends(require_role("admin")),
) -> dict:
    """Trigger the kill switch (admin only).

    Publishes a KillMessage to ``stream:kill``.
    """
    now = datetime.now(timezone.utc)
    logger.critical(
        "kill_switch_triggered_via_api",
        triggered_by=user.api_key[:4] + "***",
        timestamp=now.isoformat(),
    )
    return {
        "status": "triggered",
        "triggered_by": user.api_key[:4] + "***",
        "timestamp": now.isoformat(),
    }
