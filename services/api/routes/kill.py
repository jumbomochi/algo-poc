from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from services.api.auth import APIUser, require_role
from shared.halt_state import HaltStateRepository
from shared.logging import get_logger
from shared.schemas.messages import KillMessage

logger = get_logger("api.kill")

KILL_STREAM = "stream:kill"

router = APIRouter(prefix="/api/v1/kill", tags=["kill"])


class KillRequest(BaseModel):
    reason: str = "manual kill via API"


@router.post("")
async def trigger_kill_switch(
    request: Request,
    body: KillRequest | None = None,
    user: APIUser = Depends(require_role("admin")),
) -> dict:
    """Trigger the kill switch (admin only).

    Publishes a :class:`KillMessage` to ``stream:kill``, which the risk and
    execution services consume (cancel open orders, halt new entries,
    liquidate). Returns 503 if the message cannot be published — a kill that
    did not reach the stream must never look successful.
    """
    now = datetime.now(timezone.utc)
    triggered_by = user.api_key[:4] + "***"
    reason = body.reason if body is not None else KillRequest().reason

    redis = getattr(request.app.state, "redis", None)
    if redis is None:
        logger.critical("kill_switch_unavailable_no_redis", triggered_by=triggered_by)
        raise HTTPException(
            status_code=503,
            detail="Kill switch unavailable: API has no Redis connection",
        )

    message = KillMessage(timestamp=now, triggered_by=triggered_by, reason=reason)
    try:
        message_id = await redis.publish(KILL_STREAM, message.to_stream_dict())
        if isinstance(message_id, bytes):
            message_id = message_id.decode()
    except Exception as exc:
        logger.critical(
            "kill_switch_publish_failed",
            triggered_by=triggered_by,
            error=str(exc),
        )
        raise HTTPException(
            status_code=503,
            detail=f"Kill switch publish failed: {exc}",
        ) from exc

    logger.critical(
        "kill_switch_triggered_via_api",
        triggered_by=triggered_by,
        reason=reason,
        message_id=str(message_id),
        timestamp=now.isoformat(),
    )
    return {
        "status": "triggered",
        "triggered_by": triggered_by,
        "reason": reason,
        "message_id": str(message_id),
        "timestamp": now.isoformat(),
    }


@router.delete("")
async def clear_kill_switch(
    request: Request,
    user: APIUser = Depends(require_role("admin")),
) -> dict:
    """Clear a persisted halt so trading can resume (admin only).

    This is the explicit human clear the fail-closed kill switch requires. It
    marks the durable halt cleared; the risk service re-syncs from the DB on its
    periodic cadence (and reloads the cleared state on any restart).
    """
    sessionmaker = getattr(request.app.state, "db_sessionmaker", None)
    if sessionmaker is None:
        logger.critical("kill_clear_unavailable_no_db", triggered_by=user.api_key[:4])
        raise HTTPException(
            status_code=503,
            detail="Kill clear unavailable: API has no database connection",
        )

    mode = getattr(request.app.state, "mode", "paper")
    cleared_by = user.api_key[:4] + "***"
    now = datetime.now(timezone.utc)
    with sessionmaker() as session:
        cleared = HaltStateRepository(session).clear_halt(
            mode=mode, cleared_by=cleared_by, now=now
        )
        session.commit()

    logger.critical(
        "kill_switch_cleared_via_api",
        cleared=cleared,
        cleared_by=cleared_by,
        mode=mode,
    )
    return {
        "status": "cleared" if cleared else "no_active_halt",
        "cleared": cleared,
        "cleared_by": cleared_by,
        "mode": mode,
        "timestamp": now.isoformat(),
    }
