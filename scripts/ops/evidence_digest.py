#!/usr/bin/env python3
"""Weekly evidence digest — KAN-29.

One Telegram message every Monday morning that answers "did the week behave?"
without archaeology. The direction doc's gate day is meant to be a five-minute
read; this is that read, assembled from the evidence store rather than from
log text.

THE SHAPE, AND WHY
------------------
Three layers with a hard boundary between them:

``DigestSnapshot``   plain data, no behaviour
``render_digest``    a pure function of the snapshot — no DB, no Redis, no
                     filesystem, no clock. This is what the golden tests pin.
``collect_snapshot`` all the I/O, one guarded call per source.

The split exists so the message shape is testable without infrastructure, and
so a source that dies cannot take the message with it: every collector is
wrapped, and a failure becomes a named entry in the MISSING SOURCES banner
instead of a traceback.

FAILURE SEMANTICS
-----------------
This job NEVER skips. A quiet week sends. A week where Redis is down sends,
with a banner naming Redis. The only thing that stops a message is the send
itself failing — and that is precisely the case the dead-man switch covers,
because a digest that cannot be delivered must look, from outside, exactly
like a digest that was never generated.

DELIVERY
--------
The digest imports :class:`TelegramChannel` directly rather than publishing to
``stream:alerts``. This is deliberate and load-bearing: a wedged notifications
service is one of the conditions this digest exists to reveal, and a watcher
that dies with the thing it watches is not a watcher. It reuses the verified
channel class rather than adding a fifth copy of the bash ``telegram()``
helper.

Usage:
    python scripts/ops/evidence_digest.py [--as-of YYYY-MM-DD] [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Callable

# Executed by path from the launchd wrapper, which puts `scripts/ops/` — not
# the repo root — on sys.path. Pin the root so imports resolve to THIS
# checkout rather than to whatever tree an editable install points at.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sqlalchemy import func, select  # noqa: E402

from scripts.ops.record_epoch import _open_epoch  # noqa: E402
from services.notifications.channels import (  # noqa: E402
    TELEGRAM_MAX_BODY,
    TelegramChannel,
)
from shared.evidence_store import (  # noqa: E402
    EXCLUDED_PORTFOLIO_PREFIX,
    _passing_drill_types,
    _resolve_calendar,
    blindness,
    breach_streak,
    epoch_progress,
)
from shared.models.equity_snapshot import EquitySnapshot  # noqa: E402
from shared.models.evidence import (  # noqa: E402
    DivergenceDaily,
    DivergenceStatus,
    DrillType,
)
from shared.redis_client import DEAD_LETTER_SUFFIX  # noqa: E402

ALERTS_STREAM = "stream:alerts"
#: Exported by the launchd wrapper from the keychain (KAN-15).
DEADMAN_URL_VAR = "ALGO_DEADMAN_DIGEST_URL"

#: An epoch is 6 weeks (~30 trading sessions) at a rung — direction doc, "Epoch
#: = 6 weeks". Displayed as the denominator so "week 8 of 6" reads as what it
#: is: an epoch that has been extended.
EPOCH_WEEKS = 6
SESSIONS_PER_WEEK = 5

#: Loudest first. Unknown priorities sort after these, alphabetically, so a new
#: priority name shows up in the message instead of vanishing from the count.
PRIORITY_ORDER = ("critical", "high", "medium", "low", "info")

_TRUNCATION_MARKER = "… {n} lines omitted"

#: Calendar days behind ``as_of`` that a breach-streak scan may reach when no
#: epoch pins a scoring floor. Comfortably past MAX_STREAK_LOOKBACK_SESSIONS
#: (120 sessions ~= 168 calendar days), so the lookback bound in
#: ``breach_streak`` — not this constant — is what actually stops the walk.
_STREAK_FLOOR_DAYS = 400


# ---------------------------------------------------------------------------
# Snapshot — plain data, no behaviour
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EpochLine:
    label: str
    week: int
    weeks_total: int
    state: str
    #: The five CRITERIA_KEYS, each "green" / "amber" / "red".
    criteria: dict[str, str]


@dataclass(frozen=True)
class BlindReport:
    """Sessions on which the monitor produced no verdict at all."""

    blind_sessions: list[date]
    total_sessions: int


@dataclass(frozen=True)
class SleeveLine:
    sleeve: str
    status: str
    metric_value: float | None
    threshold: float
    #: Consecutive BREACH sessions as of ``as_of``; 0 when not breaching.
    breach_sessions: int


@dataclass(frozen=True)
class EquityLine:
    latest: float
    currency: str
    change_pct: float


@dataclass(frozen=True)
class AlertVolume:
    total: int
    by_priority: dict[str, int]
    #: True when the stream's oldest entry is newer than the window start, so
    #: the count is a floor rather than an exact figure.
    at_least: bool


@dataclass(frozen=True)
class DigestSnapshot:
    as_of: date
    window_start: date
    epoch: EpochLine | None
    blind: BlindReport | None
    sleeves: list[SleeveLine]
    equity: EquityLine | None
    #: ``None`` means the source failed; ``{}`` means every DLQ is empty.
    dlq: dict[str, int] | None
    alerts: AlertVolume | None
    drills_due: list[str]
    #: One entry per source that could not be read, already formatted.
    missing: list[str] = field(default_factory=list)
    #: Bare names of the sources that failed. Carried separately from
    #: ``missing`` because rendering has to tell "absent" from "broken": a
    #: missing epoch is the normal state before Rung 0, while a failed epoch
    #: query is not, and they must never produce the same line.
    failed: frozenset[str] = field(default_factory=frozenset)


# ---------------------------------------------------------------------------
# Rendering — pure
# ---------------------------------------------------------------------------


def _epoch_lines(epoch: EpochLine | None, failed: frozenset[str] = frozenset()) -> list[str]:
    if epoch is None:
        return ["Epoch unavailable" if "epoch" in failed else "No epoch running"]
    criteria = " · ".join(
        f"{'quantum' if key == 'evidence_quantum' else key} {value}"
        for key, value in epoch.criteria.items()
    )
    return [
        f"Epoch {epoch.label} — week {epoch.week} of {epoch.weeks_total} "
        f"· {epoch.state}",
        f"  {criteria}",
    ]


def _blind_line(blind: BlindReport | None) -> list[str]:
    if blind is None or not blind.blind_sessions:
        return []
    days = ", ".join(day.isoformat() for day in blind.blind_sessions)
    return [
        f"🚨 BLIND — the monitor saw nothing on {len(blind.blind_sessions)} "
        f"of {blind.total_sessions} sessions ({days})"
    ]


def _missing_line(missing: list[str]) -> list[str]:
    if not missing:
        return []
    return ["⚠️ MISSING SOURCES: " + " · ".join(missing)]


def _equity_lines(equity: EquityLine | None, sleeves: list[SleeveLine]) -> list[str]:
    if equity is None:
        lines = ["Equity unavailable"]
    else:
        if equity.change_pct == 0:
            change = "no change"
        else:
            change = f"{equity.change_pct:+.1f}% wk"
        lines = [f"Equity {equity.latest:,.2f} {equity.currency} ({change})"]

    if not sleeves:
        lines.append("  no divergence verdicts this week")
        return lines

    rendered = []
    for sleeve in sleeves:
        metric = (
            f"{sleeve.metric_value:.2f}" if sleeve.metric_value is not None else "—"
        )
        streak = f" ×{sleeve.breach_sessions}" if sleeve.breach_sessions else ""
        rendered.append(
            f"{sleeve.sleeve} {sleeve.status}{streak} {metric}/{sleeve.threshold:.2f}"
        )
    lines.append("  " + " · ".join(rendered))
    return lines


def _dlq_part(dlq: dict[str, int] | None) -> str:
    if dlq is None:
        return "DLQ unavailable"
    depths = {name: depth for name, depth in dlq.items() if depth}
    if not depths:
        return "DLQ clear"
    detail = ", ".join(f"{name} {depth}" for name, depth in sorted(depths.items()))
    return f"DLQ {sum(depths.values())} ({detail})"


def _alerts_part(alerts: AlertVolume | None) -> str:
    if alerts is None:
        return "alerts unavailable"
    if alerts.total == 0:
        return "alerts none"

    def _rank(name: str) -> tuple[int, str]:
        return (
            PRIORITY_ORDER.index(name) if name in PRIORITY_ORDER else len(
                PRIORITY_ORDER
            ),
            name,
        )

    detail = ", ".join(
        f"{alerts.by_priority[name]} {name}"
        for name in sorted(alerts.by_priority, key=_rank)
    )
    count = f"≥{alerts.total}" if alerts.at_least else str(alerts.total)
    return f"alerts {count} ({detail})" if detail else f"alerts {count}"


def _tail_line(snapshot: DigestSnapshot) -> str:
    drills = ", ".join(snapshot.drills_due) if snapshot.drills_due else "none"
    return (
        f"{_dlq_part(snapshot.dlq)} · {_alerts_part(snapshot.alerts)} "
        f"· drills due: {drills}"
    )


def _truncate(lines: list[str], limit: int) -> list[str]:
    """Drop lines from the tail until the body fits, marking what was cut.

    Lines are emitted loudest-first, so the tail is always the least important
    part of the message. The marker is what makes this different from
    TelegramChannel's silent ``body[:3900]`` slice.
    """
    if len("\n".join(lines)) <= limit:
        return lines

    kept = list(lines)
    while kept:
        marker = _TRUNCATION_MARKER.format(n=len(lines) - len(kept) + 1)
        if len("\n".join([*kept[:-1], marker])) <= limit:
            return [*kept[:-1], marker]
        kept.pop()
    return [_TRUNCATION_MARKER.format(n=len(lines))]


def render_digest(snapshot: DigestSnapshot) -> str:
    """Render the whole digest. Pure: no I/O, no clock, no globals.

    Line order IS the design. Blindness first, then the caveat about the
    message's own completeness, then the epoch clock, then the detail. A reader
    who takes in only the first line must get the most alarming true fact.
    """
    lines: list[str] = [
        *_blind_line(snapshot.blind),
        *_missing_line(snapshot.missing),
        *_epoch_lines(snapshot.epoch, snapshot.failed),
        *_equity_lines(snapshot.equity, snapshot.sleeves),
        _tail_line(snapshot),
    ]
    return "\n".join(_truncate(lines, TELEGRAM_MAX_BODY))


# ---------------------------------------------------------------------------
# Collection — every source guarded
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Sources:
    """The seven readers, injectable so collection is testable without infra.

    Each is a zero-argument callable already bound to its session/connection.
    Keeping them as plain callables (rather than passing a session and a Redis
    handle down) is what lets a test inject a source that raises without
    standing up the thing it would have read.
    """

    epoch: Callable[[], EpochLine | None]
    blind: Callable[[], BlindReport | None]
    sleeves: Callable[[], list[SleeveLine]]
    equity: Callable[[], EquityLine | None]
    dlq: Callable[[], dict[str, int]]
    alerts: Callable[[], AlertVolume | None]
    drills: Callable[[], list[str]]


def collect_snapshot(
    sources: Sources, *, as_of: date, window_start: date
) -> DigestSnapshot:
    """Read every source, degrading to a banner entry rather than raising.

    There is no failure here that should cost the operator the whole message.
    A source that raises yields its "unavailable" value, its name lands in
    ``failed``, and the reason lands in ``missing`` — the digest still goes
    out, honestly labelled.
    """
    missing: list[str] = []
    failed: set[str] = set()

    def _read(name: str, source: Callable[[], object], fallback: object) -> object:
        try:
            return source()
        except Exception as exc:  # noqa: BLE001 — the whole point is total capture
            missing.append(f"{name} ({type(exc).__name__}: {exc})")
            failed.add(name)
            return fallback

    epoch = _read("epoch", sources.epoch, None)
    blind = _read("blind", sources.blind, None)
    sleeves = _read("sleeves", sources.sleeves, [])
    equity = _read("equity", sources.equity, None)
    dlq = _read("dlq", sources.dlq, None)
    alerts = _read("alerts", sources.alerts, None)
    drills = _read("drills", sources.drills, [])

    return DigestSnapshot(
        as_of=as_of,
        window_start=window_start,
        epoch=epoch,
        blind=blind,
        sleeves=sleeves,
        equity=equity,
        dlq=dlq,
        alerts=alerts,
        drills_due=drills,
        missing=sorted(missing),
        failed=frozenset(failed),
    )


# ---------------------------------------------------------------------------
# The real sources
# ---------------------------------------------------------------------------
#
# Each returns a zero-argument callable so Sources can hold it unevaluated —
# collect_snapshot is the only place that decides what a failure means.
#
# Nothing here re-implements ladder arithmetic. Breach streaks, blindness and
# epoch scoring all come from shared/evidence_store.py, whose docstring names
# this digest as the caller that must reuse it: two implementations of "10
# consecutive sessions" would eventually disagree, and the disagreement would
# surface on gate day with real money waiting on it.


def epoch_source(session, *, as_of: date, calendar=None) -> Callable[[], EpochLine | None]:
    def _read() -> EpochLine | None:
        epoch = _open_epoch(session)
        if epoch is None:
            return None
        progress = epoch_progress(
            session, epoch_id=epoch.id, as_of=as_of, calendar=calendar
        )
        return EpochLine(
            label=progress.label,
            week=progress.sessions_elapsed // SESSIONS_PER_WEEK + 1,
            weeks_total=EPOCH_WEEKS,
            state=progress.state,
            criteria=dict(progress.criteria),
        )

    return _read


def blind_source(
    session, *, window_start: date, as_of: date, sleeves: list[str], baseline_id: str,
    calendar=None,
) -> Callable[[], BlindReport | None]:
    def _read() -> BlindReport | None:
        report = blindness(
            session,
            start=window_start,
            end=as_of,
            sleeves=sleeves,
            baseline_id=baseline_id,
            calendar=calendar,
        )
        blind_days = sorted({*report.blind_sessions, *report.no_data_sessions})
        if not blind_days:
            return None
        resolved = _resolve_calendar(calendar)
        total = len(resolved.trading_sessions(window_start, as_of))
        return BlindReport(blind_sessions=blind_days, total_sessions=total)

    return _read


def sleeve_source(
    session,
    *,
    window_start: date,
    as_of: date,
    calendar=None,
    streak_floor: date | None = None,
) -> Callable[[], list[SleeveLine]]:
    """One line per sleeve that produced a verdict this week.

    Driven by the verdicts actually present rather than by the epoch's sleeve
    list, so the section still reports something when no epoch is running —
    which is the state the account is in before Rung 0.

    ``streak_floor`` bounds the breach-streak walk and is emphatically NOT
    ``window_start``: ``breach_streak`` halts at the floor it is given, so
    scoring a streak against this week's start would cap every streak at one
    week. A sleeve two sessions from the 10-session ladder trigger would then
    be reported as "×5". It defaults to far enough back that
    ``MAX_STREAK_LOOKBACK_SESSIONS`` is the only thing bounding the scan.

    Deliberately NOT defaulted to the running epoch's D11 scoring floor. That
    would make the streak agree with the criteria line at the cost of hiding
    real ones: a young epoch's floor is still in the future, so every streak
    would render as zero. The two numbers answer different questions — the
    criteria line says what the ladder currently scores, this says what the
    monitor has actually observed — and the operator needs to see a streak
    building before it starts to count.
    """
    floor = streak_floor or as_of - timedelta(days=_STREAK_FLOOR_DAYS)

    def _read() -> list[SleeveLine]:
        rows = session.execute(
            select(DivergenceDaily)
            .where(
                DivergenceDaily.session_date >= window_start,
                DivergenceDaily.session_date <= as_of,
            )
            .order_by(DivergenceDaily.sleeve, DivergenceDaily.session_date)
        ).scalars().all()

        newest: dict[str, DivergenceDaily] = {}
        for row in rows:
            newest[row.sleeve] = row

        lines: list[SleeveLine] = []
        for sleeve, row in sorted(newest.items()):
            streak = 0
            if row.status == DivergenceStatus.BREACH.value:
                streak = breach_streak(
                    session,
                    sleeve=sleeve,
                    as_of=as_of,
                    baseline_id=row.baseline_id,
                    scoring_floor=floor,
                    calendar=calendar,
                ).length
            lines.append(
                SleeveLine(
                    sleeve=sleeve,
                    status=row.status,
                    metric_value=row.metric_value,
                    threshold=row.threshold,
                    breach_sessions=streak,
                )
            )
        return lines

    return _read


def equity_source(
    session, *, window_start: date, as_of: date,
    excluded_prefix: str = EXCLUDED_PORTFOLIO_PREFIX,
) -> Callable[[], EquityLine | None]:
    """Account equity at the end of the week, and the change across it.

    A display fact, not a scored one — the drawdown that the ladder grades on
    is computed by ``epoch_progress``, and this line never second-guesses it.
    Summed across sleeves and excluding synthetic portfolios, the same shape
    every other equity reader in the repo uses.
    """

    def _read() -> EquityLine | None:
        rows = session.execute(
            select(
                EquitySnapshot.date,
                func.sum(EquitySnapshot.equity),
                func.max(EquitySnapshot.trading_currency),
            )
            .where(
                EquitySnapshot.date >= window_start,
                EquitySnapshot.date <= as_of,
                ~EquitySnapshot.portfolio.startswith(excluded_prefix, autoescape=True),
            )
            .group_by(EquitySnapshot.date)
            .order_by(EquitySnapshot.date)
        ).all()
        if not rows:
            return None

        first = float(rows[0][1] or 0.0)
        last = float(rows[-1][1] or 0.0)
        change = (last - first) / first * 100.0 if first else 0.0
        return EquityLine(
            latest=last, currency=rows[-1][2] or "USD", change_pct=change
        )

    return _read


def drills_source(session, *, epoch_id: int | None) -> Callable[[], list[str]]:
    """Drill types with no passing run recorded for the current epoch.

    With no epoch running every drill is outstanding: a drill passed under a
    previous epoch's pins says nothing about this one.
    """

    def _read() -> list[str]:
        every = sorted(drill.value for drill in DrillType)
        if epoch_id is None:
            return every
        passed = _passing_drill_types(session, epoch_id=epoch_id)
        return [name for name in every if name not in passed]

    return _read


def dlq_source(redis) -> Callable[[], object]:
    """Depth of every dead-letter queue. Async — awaited by the caller."""

    async def _read() -> dict[str, int]:
        if redis is None:
            raise ConnectionError("no Redis connection")
        names = [
            name.decode() if isinstance(name, bytes) else name
            for name in await redis.keys(f"*{DEAD_LETTER_SUFFIX}")
        ]
        return {name: int(await redis.xlen(name)) for name in sorted(names)}

    return _read


def alerts_source(redis, *, window_start: date) -> Callable[[], object]:
    """Alerts published this week, by priority.

    Redis stream IDs are millisecond timestamps, so the window is a range
    query. ``at_least`` is set when the oldest surviving entry is newer than
    the window start: stream:alerts is capped by DEFAULT_STREAM_MAXLEN, and on
    a loud week the earliest alerts are trimmed away. Reporting a trimmed
    count as exact would understate the week that most needed reporting.
    """

    def _decode(value) -> str:
        return value.decode() if isinstance(value, bytes) else value

    async def _read() -> AlertVolume:
        if redis is None:
            raise ConnectionError("no Redis connection")
        start_ms = int(
            datetime.combine(window_start, time.min, tzinfo=timezone.utc).timestamp()
            * 1000
        )
        # Bounded by DEFAULT_STREAM_MAXLEN (25k), so reading the range whole is
        # safe for a weekly job.
        entries = await redis.xrange(ALERTS_STREAM, min=f"{start_ms}-0", max="+")
        by_priority: dict[str, int] = {}
        for _, data in entries:
            fields = {_decode(key): _decode(value) for key, value in data.items()}
            priority = fields.get("priority", "unknown")
            by_priority[priority] = by_priority.get(priority, 0) + 1

        oldest = await redis.xrange(ALERTS_STREAM, min="-", max="+", count=1)
        at_least = bool(oldest) and int(_decode(oldest[0][0]).split("-")[0]) > start_ms

        return AlertVolume(
            total=len(entries), by_priority=by_priority, at_least=at_least
        )

    return _read


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------

SUBJECT = "Weekly evidence digest — {as_of}"


async def deliver(snapshot: DigestSnapshot, *, channel, ping: Callable[[], None]) -> int:
    """Render, send, and ping the dead-man. Returns the process exit code.

    The ping happens only after a send the channel did not raise on, because
    the external check's entire job is to page when a healthy signal stops
    arriving. Pinging on a failed send would report the outage as health.

    The ping itself is fire-and-forget: a failure to ping must not turn a
    delivered digest into a failed job.
    """
    body = render_digest(snapshot)
    try:
        await channel.send(SUBJECT.format(as_of=snapshot.as_of.isoformat()), body)
    except Exception as exc:  # noqa: BLE001 — exit code is the only signal left
        print(f"digest send FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    try:
        ping()
    except Exception as exc:  # noqa: BLE001
        print(f"dead-man ping failed: {type(exc).__name__}: {exc}", file=sys.stderr)
    return 0


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def _blind_pins(session, *, window_start: date, as_of: date) -> tuple[list[str], str]:
    """The sleeve set and baseline to judge blindness against.

    Preference order: the running epoch's manifest, then whatever the week's
    verdicts were written under, then nothing. The fallbacks matter because
    before Rung 0 there is no epoch, and a blindness check keyed only off a
    manifest would find nothing to be blind about on precisely the weeks the
    monitor never ran.
    """
    epoch = _open_epoch(session)
    if epoch is not None:
        return list(epoch.manifest["sleeves"]), epoch.manifest["baseline_id"]

    rows = session.execute(
        select(DivergenceDaily.sleeve, DivergenceDaily.baseline_id)
        .where(
            DivergenceDaily.session_date >= window_start,
            DivergenceDaily.session_date <= as_of,
        )
        .order_by(DivergenceDaily.session_date.desc())
    ).all()
    if rows:
        baseline = rows[0][1]
        return sorted({row[0] for row in rows if row[1] == baseline}), baseline
    return [], ""


def _sessions_with_no_verdict(
    session, *, window_start: date, as_of: date, calendar
) -> list[date]:
    observed = set(
        session.scalars(
            select(DivergenceDaily.session_date).where(
                DivergenceDaily.session_date >= window_start,
                DivergenceDaily.session_date <= as_of,
            )
        ).all()
    )
    return [
        day
        for day in calendar.trading_sessions(window_start, as_of)
        if day not in observed
    ]


def build_sources(
    session, *, redis, as_of: date, window_start: date, calendar=None
) -> Sources:
    """Bind every reader to its connection.

    Performs NO I/O — every query lives inside a closure that
    ``collect_snapshot`` calls under its guard. This is load-bearing, not
    stylistic: a lookup done eagerly here (resolving the epoch to pin
    blindness against, say) would raise past every guard on a week when
    Postgres was down, and the operator would get no message at all. Silence
    is the one output this job may never produce.
    """
    resolved = _resolve_calendar(calendar)

    def _epoch_id() -> int | None:
        epoch = _open_epoch(session)
        return epoch.id if epoch else None

    def _blind() -> BlindReport | None:
        sleeves, baseline_id = _blind_pins(
            session, window_start=window_start, as_of=as_of
        )
        if sleeves and baseline_id:
            return blind_source(
                session,
                window_start=window_start,
                as_of=as_of,
                sleeves=sleeves,
                baseline_id=baseline_id,
                calendar=resolved,
            )()
        # No pins at all: fall back to "did the monitor write anything?".
        missing_days = _sessions_with_no_verdict(
            session, window_start=window_start, as_of=as_of, calendar=resolved
        )
        if not missing_days:
            return None
        return BlindReport(
            blind_sessions=missing_days,
            total_sessions=len(resolved.trading_sessions(window_start, as_of)),
        )

    return Sources(
        epoch=epoch_source(session, as_of=as_of, calendar=resolved),
        blind=_blind,
        sleeves=sleeve_source(
            session, window_start=window_start, as_of=as_of, calendar=resolved
        ),
        equity=equity_source(session, window_start=window_start, as_of=as_of),
        dlq=_awaited(dlq_source(redis)),
        alerts=_awaited(alerts_source(redis, window_start=window_start)),
        # Resolved at call time, inside the guard, for the same reason.
        drills=lambda: drills_source(session, epoch_id=_epoch_id())(),
    )


def _awaited(source: Callable[[], object]) -> Callable[[], object]:
    """Run an async source to completion so Sources stays uniformly sync.

    collect_snapshot's guarantee is that one source's failure cannot reach the
    others; keeping the async boundary inside each source rather than around
    the whole collection is what preserves it.
    """

    def _run():
        return asyncio.run(source())

    return _run


def _open_session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from shared.config import load_config

    config = load_config("config/default.yaml")  # applies ALGO_DATABASE_URL
    engine = create_engine(config.database.url)
    return Session(engine), engine


def _open_redis():
    import redis.asyncio as aioredis

    from shared.config import load_config

    return aioredis.from_url(load_config("config/default.yaml").redis.url)


def _ping_deadman() -> None:
    """Ping the external check, if one is configured for this job.

    The URL is exported by the launchd wrapper, which resolves it through
    KAN-15's keychain loader — the lookup stays in one place rather than being
    reimplemented here.
    """
    import os
    import urllib.request

    url = os.environ.get(DEADMAN_URL_VAR, "")
    if not url.startswith(("http://", "https://")):
        print(
            f"dead-man: {DEADMAN_URL_VAR} is not set to an http(s) URL, so nothing "
            "outside this host can tell the digest was sent",
            file=sys.stderr,
        )
        return
    urllib.request.urlopen(url, timeout=10).close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Weekly evidence digest (KAN-29).")
    parser.add_argument(
        "--as-of",
        type=date.fromisoformat,
        default=None,
        help="Last day of the reported week (default: today).",
    )
    parser.add_argument(
        "--window-days",
        type=int,
        default=7,
        help="Length of the reported window in calendar days.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the digest instead of sending it.",
    )
    args = parser.parse_args(argv)

    as_of = args.as_of or datetime.now(timezone.utc).date()
    window_start = as_of - timedelta(days=args.window_days)

    session, engine = _open_session()
    redis = None
    try:
        if not args.dry_run:
            redis = _open_redis()
        else:
            # A dry run still reports what it could not read, rather than
            # pretending Redis was fine.
            redis = _open_redis_quietly()

        snapshot = collect_snapshot(
            build_sources(
                session, redis=redis, as_of=as_of, window_start=window_start
            ),
            as_of=as_of,
            window_start=window_start,
        )

        if args.dry_run:
            print(render_digest(snapshot))
            return 0

        return asyncio.run(
            deliver(snapshot, channel=TelegramChannel(), ping=_ping_deadman)
        )
    finally:
        session.close()
        if engine is not None:
            engine.dispose()


def _open_redis_quietly():
    """Redis for a dry run: a failure here must not abort the preview."""
    try:
        return _open_redis()
    except Exception:  # noqa: BLE001 — collect_snapshot reports it downstream
        return None


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
