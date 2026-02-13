from __future__ import annotations

from fastapi import APIRouter, Depends

from services.api.auth import APIUser, get_current_user

router = APIRouter(prefix="/api/v1/risk", tags=["risk"])


@router.get("/status")
def get_risk_status(
    user: APIUser = Depends(get_current_user),
) -> dict:
    """Return current risk status."""
    return {
        "drawdown_pct": 0.0,
        "margin_utilization_pct": 0.0,
        "kill_switch_active": False,
        "kill_switch_reason": None,
    }
