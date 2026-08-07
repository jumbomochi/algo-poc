"""Prometheus metrics helpers for algo-poc services."""

from __future__ import annotations

import errno
from dataclasses import dataclass

from prometheus_client import (
    REGISTRY,
    CollectorRegistry,
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
    given *port*.  Call this once at service startup — every service `main`
    now does (T6 observability wiring), so Prometheus's static
    ``<service>:9090`` scrape targets in config/prometheus.yml actually have
    something listening.

    Idempotent by design: a duplicate bind on *port* raises ``OSError``
    with ``errno.EADDRINUSE`` ("address already in use") from the stdlib
    socket layer. That specific case means metrics are already being served
    in this process, which is the outcome we want anyway — so it's logged
    and swallowed rather than propagated. Crashing an entire trading service
    because its *metrics* endpoint was already up would be exactly backwards.

    Narrowed deliberately to EADDRINUSE only (review feedback): any other
    ``OSError`` — permission denied binding a privileged port, no free file
    descriptors, a bad address — is a real, different problem and should
    propagate and crash loudly rather than be silently treated the same as
    "already started".

    Args:
        service_name: Human-readable name used for log messages.
        port: TCP port to bind the metrics server to.
    """
    try:
        start_http_server(port)
    except OSError as exc:
        if exc.errno != errno.EADDRINUSE:
            raise
        logger.warning(
            "metrics_server_already_started",
            service=service_name,
            port=port,
            error=str(exc),
        )
        return
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


@dataclass(frozen=True)
class TradingMetrics:
    """Metrics required to audit daily capital and order decisions."""

    deployable_capital: Gauge
    sleeve_budget: Gauge
    reserved_notional: Gauge
    lifecycle_transitions: Counter
    lifecycle_state: Gauge
    reconciliation_entries_allowed: Gauge
    # IMPORTANT fix (T6 review): lifecycle_transitions{status=RISK_REJECTED}
    # only covers a *new order* being rejected at entry. It says nothing
    # about a passive breach on an *already-held* position (stop-loss,
    # soft/hard ceiling, margin) — that path is
    # RiskServiceRunner.run_passive_scan() in
    # services/risk_management/runner.py, which increments this with
    # breach_type=<PassiveBreach.action_type> ("trim" | "notify"). Defined
    # here even though that function isn't invoked from `run()` yet (T2's
    # thread wires that) so the metric — and the PassiveBreachDetected
    # alert rule in config/alert_rules.yml — already exist the moment T2
    # lands it.
    risk_breach_total: Counter


def create_trading_metrics(
    *, registry: CollectorRegistry | None = None
) -> TradingMetrics:
    """Create the daily orchestration metric collectors.

    Construction stays explicit so tests and individual services can own a
    registry lifecycle without module-import duplicate collector failures.
    """
    target_registry = registry or REGISTRY
    return TradingMetrics(
        deployable_capital=Gauge(
            "algo_deployable_capital_usd",
            "Broker NAV-derived capital available for strategy sleeves",
            registry=target_registry,
        ),
        sleeve_budget=Gauge(
            "algo_sleeve_budget_usd",
            "Current NAV-derived budget per strategy sleeve",
            ["portfolio"],
            registry=target_registry,
        ),
        reserved_notional=Gauge(
            "algo_reserved_notional_usd",
            "Unfilled active buy notional reserved per strategy sleeve",
            ["portfolio"],
            registry=target_registry,
        ),
        lifecycle_transitions=Counter(
            "algo_order_lifecycle_transitions_total",
            "Durably persisted order lifecycle transitions",
            ["status"],
            registry=target_registry,
        ),
        lifecycle_state=Gauge(
            "algo_order_lifecycle_state",
            "Current durable order count by account, mode, and lifecycle state",
            ["account_id", "mode", "status"],
            registry=target_registry,
        ),
        reconciliation_entries_allowed=Gauge(
            "algo_reconciliation_entries_allowed",
            "Whether broker/database reconciliation permits new entries",
            registry=target_registry,
        ),
        risk_breach_total=Counter(
            "algo_risk_breach_total",
            "Passive breaches on held positions (stop-loss / ceiling / margin)",
            ["breach_type"],
            registry=target_registry,
        ),
    )


DEFAULT_TRADING_METRICS = create_trading_metrics()
