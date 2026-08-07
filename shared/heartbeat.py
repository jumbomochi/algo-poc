"""Heartbeat-file liveness signal for long-running service loops.

Each service's main loop calls :func:`write_heartbeat` once per iteration.
A container-level Docker healthcheck (see docker-compose.yml) then checks the
file's freshness from *outside* the Python process — this is what lets a
wedged-but-not-crashed event loop (the process is alive, but stuck on a
blocking call and never reaching the top of its loop again) be detected and
marked unhealthy, which ``restart: unless-stopped`` alone misses (it only
restarts on process exit, not on a healthcheck failure).

No network/dependency involved by design: this measures "is this process's
own loop still iterating", not "is Redis/Postgres/IB reachable" — mixing the
two would make a downstream outage look identical to a local deadlock.
"""

from __future__ import annotations

import time
from pathlib import Path

DEFAULT_HEARTBEAT_PATH = "/var/algo/heartbeat"


def write_heartbeat(path: str | Path = DEFAULT_HEARTBEAT_PATH) -> None:
    """Touch *path* with the current time, creating parent directories.

    Safe to call every loop iteration — this is a single small file write,
    not an fsync-heavy operation. Never raises for the container's normal
    case (writable path); callers are long-running service loops that should
    not crash over a heartbeat write failing, so this deliberately does not
    swallow exceptions itself — callers can wrap it if they want that.
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
