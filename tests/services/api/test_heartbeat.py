"""KAN-15 (P1-12) — the api's liveness heartbeat.

Before this, ``services/api/app.py`` called ``setup_metrics("api", ...)`` but
never registered a heartbeat collector. ``HeartbeatStale``'s
``job!="data-ingestion"`` matcher therefore *included* the api and could never
fire for it, because ``algo_heartbeat_age_seconds{job="api"}`` did not exist —
a wedged API was invisible to the alert that was supposed to cover it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from prometheus_client import CollectorRegistry, generate_latest

from services.api.app import API_HEARTBEAT_INTERVAL_SECONDS, _heartbeat_loop
from shared.heartbeat import (
    HEARTBEAT_AGE_METRIC_NAME,
    heartbeat_age_seconds,
    register_heartbeat_collector,
)


async def test_the_heartbeat_loop_writes_before_it_first_sleeps(tmp_path: Path) -> None:
    """A heartbeat written only *after* the first interval leaves a freshly
    started container reporting inf for that whole window."""
    path = tmp_path / "heartbeat"
    task = asyncio.create_task(_heartbeat_loop(path, interval=3600))
    try:
        for _ in range(50):
            if path.exists():
                break
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
    assert path.exists(), "no heartbeat was written before the first sleep"
    assert heartbeat_age_seconds(path) < 5


async def test_the_heartbeat_loop_keeps_writing(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat"
    task = asyncio.create_task(_heartbeat_loop(path, interval=0.01))
    try:
        await asyncio.sleep(0.05)
        first = path.read_text()
        await asyncio.sleep(0.05)
        second = path.read_text()
    finally:
        task.cancel()
    assert second != first, "the loop stopped after one iteration"


def test_the_api_publishes_the_heartbeat_gauge(tmp_path: Path) -> None:
    """AC7 — the series HeartbeatStale needs actually exists for job="api"."""
    path = tmp_path / "heartbeat"
    path.write_text("0")
    registry = CollectorRegistry()
    register_heartbeat_collector(path, registry=registry)
    exposition = generate_latest(registry).decode()
    assert HEARTBEAT_AGE_METRIC_NAME in exposition


def test_the_write_cadence_is_well_inside_the_alert_threshold() -> None:
    """config/alert_rules.yml fires HeartbeatStale above 120s with `for: 2m`.
    A cadence anywhere near that would make an ordinary slow iteration look
    like a wedge; one far above it would make a wedge look ordinary."""
    assert API_HEARTBEAT_INTERVAL_SECONDS * 2 < 120
