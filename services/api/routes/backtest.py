from __future__ import annotations

from fastapi import APIRouter, Depends

from services.api.auth import APIUser, get_current_user

router = APIRouter(prefix="/api/v1/backtest", tags=["backtest"])


@router.get("/results")
def get_backtest_results(
    user: APIUser = Depends(get_current_user),
) -> dict:
    """Return latest backtest results."""
    return {
        "last_run": None,
        "sharpe_ratio": None,
        "total_return_pct": None,
        "max_drawdown_pct": None,
        "win_rate_pct": None,
    }
