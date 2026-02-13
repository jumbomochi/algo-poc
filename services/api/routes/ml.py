from __future__ import annotations

from fastapi import APIRouter, Depends

from services.api.auth import APIUser, get_current_user

router = APIRouter(prefix="/api/v1/ml", tags=["ml"])


@router.get("/model")
def get_active_model(
    user: APIUser = Depends(get_current_user),
) -> dict:
    """Return active model version and metrics."""
    return {
        "model_version": None,
        "trained_at": None,
        "metrics": {},
    }
