from __future__ import annotations

from fastapi import Depends, FastAPI

from services.api.auth import APIUser, get_current_user
from services.api.routes import activity, backtest, kill, ml, portfolio, positions, risk
from shared.logging import get_logger

logger = get_logger("api.app")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="algo-poc API",
        version="0.1.0",
        description="Trading bot monitoring and control API",
    )

    # Register route modules.
    app.include_router(portfolio.router)
    app.include_router(positions.router)
    app.include_router(risk.router)
    app.include_router(activity.router)
    app.include_router(kill.router)
    app.include_router(ml.router)
    app.include_router(backtest.router)

    # Auth-check smoke endpoint.
    @app.get("/api/v1/auth-check")
    def auth_check(user: APIUser = Depends(get_current_user)) -> dict:
        """Smoke test endpoint to verify authentication."""
        return {"status": "ok", "api_key": user.api_key, "role": user.role}

    logger.info("api_app_created")
    return app
