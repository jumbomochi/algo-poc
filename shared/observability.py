"""Prometheus metrics helpers for algo-poc services."""
from __future__ import annotations

from prometheus_client import (
    Counter,
    Gauge,
    Histogram,
    start_http_server,
)

from shared.logging import get_logger

logger = get_logger("observability")


def setup_metrics(service_name: str, port: int = 9090) -> None:
    """Start a Prometheus metrics HTTP endpoint.

    Launches a background HTTP server that exposes ``/metrics`` on the
    given *port*.  Call this once at service startup.

    Args:
        service_name: Human-readable name used for log messages.
        port: TCP port to bind the metrics server to.
    """
    start_http_server(port)
    logger.info("metrics_server_started", service=service_name, port=port)


def create_counter(name: str, description: str) -> Counter:
    """Create and return a Prometheus :class:`Counter`.

    Args:
        name: Metric name (e.g. ``messages_processed_total``).
        description: Human-readable help string.
    """
    return Counter(name, description)


def create_histogram(name: str, description: str) -> Histogram:
    """Create and return a Prometheus :class:`Histogram`.

    Args:
        name: Metric name (e.g. ``request_duration_seconds``).
        description: Human-readable help string.
    """
    return Histogram(name, description)


def create_gauge(name: str, description: str) -> Gauge:
    """Create and return a Prometheus :class:`Gauge`.

    Args:
        name: Metric name (e.g. ``active_connections``).
        description: Human-readable help string.
    """
    return Gauge(name, description)
