"""Heartbeat-file liveness signal for long-running service loops.

Each service's main loop calls :func:`write_heartbeat` once per iteration.
Two independent things read that file's freshness:

1. A container-level Docker healthcheck (see docker-compose.yml) checks it
   from *outside* the Python process.
2. :class:`HeartbeatAgeCollector`, registered via
   :func:`register_heartbeat_collector`, exposes it as a Prometheus gauge
   (``algo_heartbeat_age_seconds``) computed fresh on every ``/metrics``
   scrape.

Both exist because a wedged-but-not-crashed event loop (the process is
alive, but stuck on a blocking call and never reaching the top of its loop
again — the known IB stuck-modal class) is exactly the failure
``restart: unless-stopped`` alone misses (it only restarts on process exit),
and — less obviously — is also a failure ``up{job=...}`` metrics-endpoint
scraping alone misses: ``setup_metrics()``'s HTTP server runs on its own
background *thread*, independent of the wedged asyncio loop, so it keeps
answering scrapes (blocking I/O releases the GIL) with ``up==1`` even while
the loop that's supposed to be doing work is stuck. The Prometheus gauge
closes that gap because it re-reads the file's mtime at scrape time, in the
surviving thread, rather than relying on the wedged loop to have reported
anything recently.

No network/dependency involved by design: this measures "is this process's
own loop still iterating", not "is Redis/Postgres/IB reachable" — mixing the
two would make a downstream outage look identical to a local deadlock.

Fail-loud trade-off: :func:`write_heartbeat` deliberately does NOT swallow
exceptions (a full disk, a permissions problem, a read-only filesystem).
A service loop that can no longer write a few bytes to its own container
filesystem has a real, worth-surfacing problem — silently continuing as if
nothing happened would turn this into exactly the kind of quiet failure this
module exists to catch. Callers that want different behavior can wrap the
call themselves; the default is to let it raise.
"""

from __future__ import annotations

import time
from pathlib import Path

from prometheus_client import REGISTRY, generate_latest
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import CollectorRegistry

from shared.logging import get_logger

logger = get_logger("heartbeat")

DEFAULT_HEARTBEAT_PATH = "/var/algo/heartbeat"

HEARTBEAT_AGE_METRIC_NAME = "algo_heartbeat_age_seconds"


def write_heartbeat(path: str | Path = DEFAULT_HEARTBEAT_PATH) -> None:
    """Touch *path* with the current time, creating parent directories.

    Safe to call every loop iteration — this is a single small file write,
    not an fsync-heavy operation. See the module docstring's "fail-loud
    trade-off" note: this intentionally does not swallow exceptions.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(str(time.time()))


def heartbeat_age_seconds(path: str | Path = DEFAULT_HEARTBEAT_PATH) -> float:
    """Return seconds since the heartbeat file was last written.

    Returns ``float("inf")`` if the file doesn't exist yet (e.g. during the
    brief startup window before the first loop iteration completes) so a
    freshness check treats "never written" the same as "stale", not as
    healthy.
    """
    target = Path(path)
    try:
        mtime = target.stat().st_mtime
    except FileNotFoundError:
        return float("inf")
    return time.time() - mtime


class HeartbeatAgeCollector:
    """Prometheus collector reporting heartbeat-file staleness at scrape time.

    Deliberately a plain custom collector (not a ``Gauge().set()`` updated
    from inside the main loop): a ``Gauge`` only reflects whatever value was
    last *pushed* to it, which is exactly the value a wedged main loop would
    stop pushing. A collector's ``collect()`` runs synchronously inside the
    metrics HTTP server's request handler (its own background thread) every
    time something scrapes ``/metrics`` — so it recomputes the age from the
    file's mtime fresh on every scrape, independent of whether the main loop
    is still iterating.
    """

    def __init__(self, path: str | Path = DEFAULT_HEARTBEAT_PATH):
        self._path = path

    def collect(self):
        family = GaugeMetricFamily(
            HEARTBEAT_AGE_METRIC_NAME,
            "Seconds since this service's main loop last wrote its heartbeat "
            "file, recomputed fresh on every scrape",
        )
        family.add_metric([], heartbeat_age_seconds(self._path))
        yield family


def register_heartbeat_collector(
    path: str | Path = DEFAULT_HEARTBEAT_PATH,
    *,
    registry: CollectorRegistry = REGISTRY,
) -> HeartbeatAgeCollector | None:
    """Register a :class:`HeartbeatAgeCollector` for *path* on *registry*.

    Call once per process, alongside ``setup_metrics()``. Idempotent: a
    second call against the same registry is a no-op rather than emitting a
    duplicate ``algo_heartbeat_age_seconds`` series — two identically-named,
    unlabeled series in one scrape response is invalid Prometheus exposition
    format and would make Prometheus reject the whole scrape, which is a far
    worse outcome than just skipping the redundant registration.
    """
    if HEARTBEAT_AGE_METRIC_NAME in generate_latest(registry).decode():
        logger.warning("heartbeat_collector_already_registered", path=str(path))
        return None
    collector = HeartbeatAgeCollector(path)
    registry.register(collector)
    return collector
