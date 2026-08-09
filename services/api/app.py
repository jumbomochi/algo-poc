from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI

from services.api.auth import APIUser, get_current_user
from services.api.routes import activity, backtest, kill, ml, portfolio, positions, risk
from shared.logging import get_logger
from shared.redis_client import RedisStreamClient

logger = get_logger("api.app")


def create_app(
    redis_client: RedisStreamClient | None = None,
    db_sessionmaker: Any = None,
    mode: str = "paper",
) -> FastAPI:
    """Create and configure the FastAPI application.

    Args:
        redis_client: Injected Redis Streams client (used by tests). When
            omitted, a client is created from config during app lifespan —
            the kill endpoint needs it to publish to ``stream:kill``.
        db_sessionmaker: Injected SQLAlchemy sessionmaker (used by tests). When
            omitted, one is created from config during app lifespan — the kill
            *clear* endpoint needs it to clear the durable halt.
        mode: trading mode the halt table is scoped by (``paper``/``live``).
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        owned_conn = None
        if app.state.redis is None or app.state.db_sessionmaker is None:
            from shared.config import load_config
            from shared.observability import setup_metrics

            config = load_config("config/default.yaml")
            # T6: wire the /metrics endpoint (see shared/observability.py).
            # Skipped whenever a test injects a redis_client double (this
            # branch never runs then), so it only binds a real port for a
            # real container start.
            setup_metrics("api", port=config.observability.prometheus_port)
            if app.state.redis is None:
                import redis.asyncio as aioredis

                owned_conn = aioredis.from_url(config.redis.url)
                app.state.redis = RedisStreamClient(owned_conn)
                logger.info("api_redis_connected", url_scheme="redis")
            if app.state.db_sessionmaker is None:
                from sqlalchemy import create_engine
                from sqlalchemy.orm import sessionmaker

                engine = create_engine(config.database.url)
                app.state.db_sessionmaker = sessionmaker(bind=engine)
                app.state.mode = config.mode
                logger.info("api_db_connected")
        yield
        if owned_conn is not None:
            await owned_conn.aclose()

    app = FastAPI(
        title="algo-poc API",
        version="0.1.0",
        description="Trading bot monitoring and control API",
        lifespan=lifespan,
    )
    # Set immediately (not just in lifespan) so injected clients work even
    # when a TestClient is used without a `with` block (no lifespan events).
    app.state.redis = redis_client
    app.state.db_sessionmaker = db_sessionmaker
    app.state.mode = mode

    # Register route modules.
    app.include_router(portfolio.router)
    app.include_router(positions.router)
    app.include_router(risk.router)
    app.include_router(activity.router)
    app.include_router(kill.router)
    app.include_router(ml.router)
    app.include_router(backtest.router)

    # T6: unauthenticated liveness probe for the container healthcheck (see
    # docker-compose.yml). Deliberately does not touch app.state.redis or any
    # other dependency — this answers "is the process/event loop responsive",
    # not "are our dependencies up"; conflating the two would make a Redis
    # outage look identical to a wedged API process and trigger the wrong
    # response. No auth by design (a healthcheck can't be handed an API key
    # without putting it in the compose file in cleartext) and it returns no
    # information beyond "the server answered".
    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok"}

    # Auth-check smoke endpoint.
    @app.get("/api/v1/auth-check")
    def auth_check(user: APIUser = Depends(get_current_user)) -> dict:
        """Smoke test endpoint to verify authentication."""
        return {"status": "ok", "role": user.role}

    logger.info("api_app_created")
    return app
