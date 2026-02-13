from __future__ import annotations

from fastapi import APIRouter, Depends

from services.api.auth import APIUser, get_current_user

router = APIRouter(prefix="/api/v1/portfolio", tags=["portfolio"])


@router.get("")
def get_portfolio(
    user: APIUser = Depends(get_current_user),
) -> dict:
    """Return current portfolio summary."""
    return {
        "positions": [],
        "nav": 0.0,
        "exposure_pct": 0.0,
        "margin_utilization_pct": 0.0,
        "pnl": 0.0,
    }
