# shared/logging.py
from __future__ import annotations

import structlog


def configure_logging() -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            # Render exc_info=True / .exception() into an actual traceback
            # string — without this the JSON carries only '"exc_info": true'
            # and the stack trace is lost (2026-07-11 execution failure).
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(service: str) -> structlog.BoundLogger:
    configure_logging()
    return structlog.get_logger(service=service)
