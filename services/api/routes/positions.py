from __future__ import annotations

from fastapi import APIRouter, Depends

from services.api.auth import APIUser, get_current_user

router = APIRouter(prefix="/api/v1/positions", tags=["positions"])


@router.get("")
def list_positions(
    user: APIUser = Depends(get_current_user),
) -> list[dict]:
    """Return all positions with details."""
    return []


@router.get("/{ticker}")
def get_position(
    ticker: str,
    user: APIUser = Depends(get_current_user),
) -> dict:
    """Return detail for a specific position."""
    return {
        "ticker": ticker,
        "quantity": 0,
        "avg_cost": 0.0,
        "current_price": 0.0,
        "unrealized_pnl": 0.0,
        "market_value": 0.0,
    }
