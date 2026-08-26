"""KAN-29 — the weekly evidence digest.

The rendering half of this module is a pure function over a
:class:`DigestSnapshot`, so every golden-output test here runs with no
database, no Redis and no network. The collection half is exercised against
the sqlite fixture and a fake Redis; the delivery half against a fake channel.

The five golden states are pinned as whole strings rather than as per-line
assertions. A digest is read by a human at 08:00 on a Monday, and the thing
that breaks it is a line moving, not a line's contents changing — whole-string
equality is the only assertion that catches a reorder.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import scripts.ops.evidence_digest as evidence_digest
from scripts.ops.evidence_digest import (
    EPOCH_WEEKS,
    AlertVolume,
    BlindReport,
    DigestSnapshot,
    EpochLine,
    EquityLine,
    SleeveLine,
    Sources,
    alerts_source,
    blind_source,
    build_sources,
    collect_snapshot,
    deliver,
    dlq_source,
    drills_source,
    epoch_source,
    equity_source,
    render_digest,
    sleeve_source,
)
from services.notifications.channels import TELEGRAM_MAX_BODY
from shared.evidence_store import CRITERIA_KEYS, EXCLUDED_PORTFOLIO_PREFIX
from shared.models.base import Base
from shared.models.equity_snapshot import EquitySnapshot
from shared.models.evidence import (
    DivergenceDaily,
    DrillOutcome,
    DrillType,
    GateEpoch,
    GateEpochEvent,
)


#: A manifest ``validate_manifest`` accepts — the read-time helper re-validates
#: on load, so an epoch fixture needs a real one.
MANIFEST = {
    "baseline_id": "backtest_multi_20260812_101500.json",
    "sleeves": ["momentum", "value_rotation"],
    "weights": {"momentum": 0.5, "value_rotation": 0.5},
    "membership_snapshot": "data/universe/sp500_membership.json",
    "membership_snapshot_sha256": "a" * 64,
    "divergence": {"window_sessions": 30, "threshold": 0.20},
    "cost_model": {
        "slippage_bps": 10.0,
        "commission_per_share": 0.005,
        "commission_minimum": 1.0,
    },
    "money_path": {
        "services/risk_management": "b" * 40,
        "services/execution": "c" * 40,
        "scripts/run_paper.py": "d" * 40,
        "shared/order_ledger.py": "e" * 40,
        "shared/liquidation.py": "f" * 40,
    },
}


AS_OF = date(2026, 8, 17)
WINDOW_START = date(2026, 8, 10)


def _snapshot(**overrides) -> DigestSnapshot:
    """An all-green week. Every other fixture is this one, minus something."""
    defaults = dict(
        as_of=AS_OF,
        window_start=WINDOW_START,
        epoch=EpochLine(
            label="v2",
            week=3,
            weeks_total=EPOCH_WEEKS,
            state="RUNNING",
            criteria={
                "divergence": "green",
                "drawdown": "green",
                "safety": "green",
                "drills": "green",
                "evidence_quantum": "green",
            },
        ),
        blind=None,
        sleeves=[
            SleeveLine("momentum", "OK", 0.8, 5.0, 0),
            SleeveLine("sector_rotation", "OK", 1.1, 5.0, 0),
        ],
        equity=EquityLine(latest=5012.34, currency="USD", change_pct=1.2),
        dlq={},
        alerts=AlertVolume(total=3, by_priority={"high": 2, "info": 1}, at_least=False),
        drills_due=[],
        missing=[],
    )
    return DigestSnapshot(**{**defaults, **overrides})


# ---------------------------------------------------------------------------
# The five golden states
# ---------------------------------------------------------------------------


ALL_GREEN = """\
Epoch v2 — week 3 of 6 · RUNNING
  divergence green · drawdown green · safety green · drills green · quantum green
Equity 5,012.34 USD (+1.2% wk)
  momentum OK 0.80/5.00 · sector_rotation OK 1.10/5.00
DLQ clear · alerts 3 (2 high, 1 info) · drills due: none"""


def test_all_green_week_renders_the_golden_string():
    assert render_digest(_snapshot()) == ALL_GREEN


QUIET_WEEK = """\
Epoch v2 — week 1 of 6 · RUNNING
  divergence green · drawdown green · safety green · drills amber · quantum amber
Equity 5,000.00 USD (no change)
  no divergence verdicts this week
DLQ clear · alerts none · drills due: restart_halt, synthetic_stop"""


def test_quiet_week_still_renders_a_full_message():
    """A week where nothing happened is not a week with nothing to say.

    This is the fixture that proves the digest never degrades to silence: no
    fills, no verdicts, no alerts, and it still reports the epoch clock.
    """
    snapshot = _snapshot(
        epoch=replace(
            _snapshot().epoch,
            week=1,
            criteria={
                "divergence": "green",
                "drawdown": "green",
                "safety": "green",
                "drills": "amber",
                "evidence_quantum": "amber",
            },
        ),
        sleeves=[],
        equity=EquityLine(latest=5000.0, currency="USD", change_pct=0.0),
        alerts=AlertVolume(total=0, by_priority={}, at_least=False),
        drills_due=["restart_halt", "synthetic_stop"],
    )
    assert render_digest(snapshot) == QUIET_WEEK


MISSING_SOURCES = """\
⚠️ MISSING SOURCES: alerts (ConnectionError: redis unreachable) · dlq (ConnectionError: redis unreachable)
Epoch v2 — week 3 of 6 · RUNNING
  divergence green · drawdown green · safety green · drills green · quantum green
Equity 5,012.34 USD (+1.2% wk)
  momentum OK 0.80/5.00 · sector_rotation OK 1.10/5.00
DLQ unavailable · alerts unavailable · drills due: none"""


def test_missing_sources_banner_names_every_unavailable_source():
    snapshot = _snapshot(
        dlq=None,
        alerts=None,
        missing=[
            "alerts (ConnectionError: redis unreachable)",
            "dlq (ConnectionError: redis unreachable)",
        ],
    )
    assert render_digest(snapshot) == MISSING_SOURCES


BLIND_FIRST = """\
🚨 BLIND — the monitor saw nothing on 3 of 5 sessions (2026-08-11, 2026-08-12, 2026-08-13)
Epoch v2 — week 3 of 6 · RUNNING
  divergence green · drawdown green · safety green · drills green · quantum green
Equity 5,012.34 USD (+1.2% wk)
  momentum OK 0.80/5.00 · sector_rotation OK 1.10/5.00
DLQ clear · alerts 3 (2 high, 1 info) · drills due: none"""


def test_blind_week_renders_the_blind_line_first():
    snapshot = _snapshot(
        blind=BlindReport(
            blind_sessions=[date(2026, 8, 11), date(2026, 8, 12), date(2026, 8, 13)],
            total_sessions=5,
        )
    )
    assert render_digest(snapshot) == BLIND_FIRST


BREACH_STREAK = """\
Epoch v2 — week 5 of 6 · RUNNING
  divergence red · drawdown green · safety green · drills green · quantum green
Equity 4,700.00 USD (-3.1% wk)
  value_rotation BREACH ×4 12.30/5.00 · momentum OK 0.40/5.00
DLQ 2 (stream:fills:dlq 2) · alerts 12 (12 high) · drills due: none"""


def test_breach_streak_week_reports_the_streak_length():
    snapshot = _snapshot(
        epoch=replace(
            _snapshot().epoch,
            week=5,
            criteria={
                "divergence": "red",
                "drawdown": "green",
                "safety": "green",
                "drills": "green",
                "evidence_quantum": "green",
            },
        ),
        sleeves=[
            SleeveLine("value_rotation", "BREACH", 12.3, 5.0, 4),
            SleeveLine("momentum", "OK", 0.4, 5.0, 0),
        ],
        equity=EquityLine(latest=4700.0, currency="USD", change_pct=-3.1),
        dlq={"stream:fills:dlq": 2},
        alerts=AlertVolume(total=12, by_priority={"high": 12}, at_least=False),
    )
    assert render_digest(snapshot) == BREACH_STREAK


# ---------------------------------------------------------------------------
# Ordering — AC2
# ---------------------------------------------------------------------------


def test_blind_outranks_the_missing_sources_banner():
    """Both banners at once: BLIND is still the first thing read.

    A partial outage is a caveat about the message; blindness is a fact about
    the account. If a reader only takes in one line, it must be the second.
    """
    snapshot = _snapshot(
        blind=BlindReport(blind_sessions=[date(2026, 8, 11)], total_sessions=5),
        alerts=None,
        missing=["alerts (ConnectionError: redis unreachable)"],
    )
    lines = render_digest(snapshot).splitlines()
    assert lines[0].startswith("🚨 BLIND")
    assert lines[1].startswith("⚠️ MISSING SOURCES")


def test_all_green_first_line_is_the_epoch_progress():
    assert render_digest(_snapshot()).splitlines()[0].startswith("Epoch v2")


def test_no_open_epoch_is_reported_rather_than_omitted():
    """No epoch is a real state, not a missing source.

    Before Rung 0 there is no epoch at all. Rendering nothing there would make
    the commonest early state indistinguishable from a broken query.
    """
    rendered = render_digest(_snapshot(epoch=None))
    assert rendered.splitlines()[0] == "No epoch running"
    assert "MISSING SOURCES" not in rendered


# ---------------------------------------------------------------------------
# Length — AC5
# ---------------------------------------------------------------------------


def _worst_case() -> DigestSnapshot:
    """Six sleeves, all breaching, with long names and a full DLQ."""
    return _snapshot(
        sleeves=[
            SleeveLine(f"{name}_extended_sleeve_name", "BREACH", 123.456, 5.0, 40)
            for name in (
                "momentum",
                "sector_rotation",
                "thematic_momentum",
                "quality_value",
                "earnings_drift",
                "tail_risk_hedge",
            )
        ],
        blind=BlindReport(
            blind_sessions=[date(2026, 8, d) for d in range(1, 15)],
            total_sessions=20,
        ),
        dlq={f"stream:{n}:dlq": 999 for n in ("fills", "alerts", "orders", "signals")},
        alerts=AlertVolume(
            total=9999,
            by_priority={"high": 5000, "medium": 3000, "info": 1999},
            at_least=True,
        ),
        drills_due=["restart_halt", "synthetic_stop"],
        missing=[f"source_{i} (RuntimeError: something went wrong)" for i in range(4)],
    )


def test_worst_case_week_fits_the_telegram_body_limit():
    assert len(render_digest(_worst_case())) <= TELEGRAM_MAX_BODY


def test_overflow_is_truncated_with_a_visible_marker():
    """An over-long digest loses its tail loudly, never silently.

    TelegramChannel already slices the body at 3900 characters. Letting that
    slice do the work would drop lines with nothing to show for it — the same
    class of silent failure this job exists to end.
    """
    snapshot = _worst_case()
    huge = replace(
        snapshot,
        sleeves=[
            SleeveLine(f"sleeve_{i:03d}_with_a_long_name", "BREACH", 99.9, 5.0, 30)
            for i in range(400)
        ],
    )
    rendered = render_digest(huge)
    assert len(rendered) <= TELEGRAM_MAX_BODY
    assert "lines omitted" in rendered.splitlines()[-1]


def test_truncation_keeps_the_loudest_line():
    snapshot = replace(
        _worst_case(),
        sleeves=[
            SleeveLine(f"sleeve_{i:03d}_with_a_long_name", "BREACH", 99.9, 5.0, 30)
            for i in range(400)
        ],
    )
    assert render_digest(snapshot).splitlines()[0].startswith("🚨 BLIND")


# ---------------------------------------------------------------------------
# Purity — AC6
# ---------------------------------------------------------------------------


def test_render_digest_performs_no_io(tmp_path, monkeypatch):
    """Rendering must not touch the filesystem, a socket, or a database.

    Enforced by making all three fail: any open(), any socket construction, and
    an empty cwd. A renderer that quietly grew a log read would raise here.
    """
    import socket

    monkeypatch.chdir(tmp_path)

    def _no_sockets(*args, **kwargs):
        raise AssertionError("render_digest opened a socket")

    def _no_files(*args, **kwargs):
        raise AssertionError("render_digest opened a file")

    monkeypatch.setattr(socket, "socket", _no_sockets)
    monkeypatch.setattr("builtins.open", _no_files)

    assert render_digest(_snapshot()) == ALL_GREEN


def test_the_digest_reads_no_logs_and_imports_nothing_from_deploy():
    """AC7, asserted against the source rather than trusted.

    The digest's whole premise is that it reports the evidence store, not
    scrollback. A log tail creeping in would make it agree with the daily
    report's failure mode instead of correcting it.
    """
    source = (
        Path(evidence_digest.__file__).read_text()
        # This docstring names the thing it forbids; strip it before grepping.
        .split('"""', 2)[-1]
    )
    assert "deploy" not in source
    assert ".log" not in source
    assert "readlines" not in source


# ---------------------------------------------------------------------------
# Collection — AC3
# ---------------------------------------------------------------------------


def _sources(**overrides) -> Sources:
    snap = _snapshot()
    defaults = dict(
        epoch=lambda: snap.epoch,
        blind=lambda: None,
        sleeves=lambda: snap.sleeves,
        equity=lambda: snap.equity,
        dlq=lambda: {},
        alerts=lambda: snap.alerts,
        drills=lambda: [],
    )
    return Sources(**{**defaults, **overrides})


def _boom(message: str = "redis unreachable"):
    def _raise():
        raise ConnectionError(message)

    return _raise


def test_a_raising_source_becomes_a_banner_entry_not_a_traceback():
    snapshot = collect_snapshot(
        _sources(alerts=_boom()), as_of=AS_OF, window_start=WINDOW_START
    )
    assert snapshot.alerts is None
    assert snapshot.missing == ["alerts (ConnectionError: redis unreachable)"]


def test_one_dead_source_does_not_stop_the_others():
    """The digest is worth more partial than absent.

    If a single failure could empty the message, the operator would learn
    nothing about the week on exactly the weeks something was wrong.
    """
    snapshot = collect_snapshot(
        _sources(dlq=_boom(), alerts=_boom()), as_of=AS_OF, window_start=WINDOW_START
    )
    assert snapshot.epoch is not None
    assert snapshot.sleeves
    assert len(snapshot.missing) == 2


def test_a_failed_epoch_source_is_not_reported_as_a_quiet_week():
    """"No epoch running" and "the query broke" must never render alike.

    Before Rung 0 the honest answer is "no epoch running", so that string is
    load-bearing. A broken query borrowing it would read as a legitimate state
    forever.
    """
    snapshot = collect_snapshot(
        _sources(epoch=_boom("connection refused")),
        as_of=AS_OF,
        window_start=WINDOW_START,
    )
    rendered = render_digest(snapshot)
    assert "Epoch unavailable" in rendered
    assert "No epoch running" not in rendered


# ---------------------------------------------------------------------------
# Delivery and the dead-man switch — AC4
# ---------------------------------------------------------------------------


class FakeChannel:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[tuple[str, str]] = []

    async def send(self, subject: str, body: str) -> None:
        if self.fail:
            raise RuntimeError("telegram 502")
        self.sent.append((subject, body))


def test_the_dead_man_is_pinged_after_a_successful_send():
    channel = FakeChannel()
    pings: list[str] = []

    exit_code = asyncio.run(
        deliver(_snapshot(), channel=channel, ping=lambda: pings.append("ping"))
    )

    assert exit_code == 0
    assert len(channel.sent) == 1
    assert pings == ["ping"]


def test_a_failed_send_does_not_ping_the_dead_man():
    """The one case where silence is the correct output.

    Pinging here would tell the external checker the digest went out when it
    did not — a monitor reporting a failure as health is worse than no monitor
    (deploy/launchd/deadman.sh says the same about the paper run).
    """
    channel = FakeChannel(fail=True)
    pings: list[str] = []

    exit_code = asyncio.run(
        deliver(_snapshot(), channel=channel, ping=lambda: pings.append("ping"))
    )

    assert exit_code == 1
    assert pings == []


def test_the_subject_carries_the_week_being_reported():
    channel = FakeChannel()
    asyncio.run(deliver(_snapshot(), channel=channel, ping=lambda: None))
    subject, _ = channel.sent[0]
    assert "2026-08-17" in subject


def test_a_failing_dead_man_ping_does_not_fail_the_digest():
    """Fire and forget, exactly as deadman.sh promises.

    A flaky network on the ping must not turn a delivered digest into a failed
    job — that would be monitoring causing the outage it exists to detect.
    """

    def _explode():
        raise ConnectionError("no route to host")

    exit_code = asyncio.run(deliver(_snapshot(), channel=FakeChannel(), ping=_explode))

    assert exit_code == 0


# ---------------------------------------------------------------------------
# The real collectors, against sqlite and a fake Redis
# ---------------------------------------------------------------------------


BASELINE = "backtest_multi_20260812_101500.json"


class FakeCalendar:
    def __init__(self, sessions: list[date]) -> None:
        self._sessions = sorted(sessions)
        self._lookup = set(self._sessions)

    def is_trading_day(self, d: date) -> bool:
        return d in self._lookup

    def trading_sessions(self, start: date, end: date) -> list[date]:
        return [d for d in self._sessions if start <= d <= end]


def _weekdays(start: date, end: date) -> list[date]:
    days, day = [], start
    while day <= end:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


@pytest.fixture
def cal() -> FakeCalendar:
    return FakeCalendar(_weekdays(date(2025, 1, 1), date(2027, 12, 31)))


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


def _verdict(db, sleeve: str, day: date, status: str, metric: float = 1.0) -> None:
    db.add(
        DivergenceDaily(
            sleeve=sleeve,
            session_date=day,
            status=status,
            baseline_id=BASELINE,
            window_sessions=30,
            threshold=5.0,
            metric_value=metric,
            created_at=datetime(2026, 8, 17, tzinfo=timezone.utc),
        )
    )
    db.commit()


def _equity(db, day: date, portfolio: str, equity: float) -> None:
    db.add(
        EquitySnapshot(
            portfolio=portfolio,
            date=day,
            equity=equity,
            cash=0.0,
            market_value=equity,
            trading_currency="USD",
            created_at=datetime(2026, 8, 17, tzinfo=timezone.utc),
        )
    )
    db.commit()


def test_sleeve_source_reports_the_newest_verdict_per_sleeve(db, cal):
    """Two verdicts for one sleeve collapse to the later one.

    The digest reports where the week ENDED, not every step it took; the
    intermediate verdicts are already in the daily alerts.
    """
    _verdict(db, "momentum", date(2026, 8, 11), "BREACH", metric=9.0)
    _verdict(db, "momentum", date(2026, 8, 14), "OK", metric=0.5)

    lines = sleeve_source(db, window_start=WINDOW_START, as_of=AS_OF, calendar=cal)()

    assert [(line.sleeve, line.status, line.metric_value) for line in lines] == [
        ("momentum", "OK", 0.5)
    ]


def test_sleeve_source_reports_the_breach_streak_length(db, cal):
    for day in _weekdays(date(2026, 8, 10), date(2026, 8, 14)):
        _verdict(db, "value_rotation", day, "BREACH", metric=9.0)

    (line,) = sleeve_source(db, window_start=WINDOW_START, as_of=AS_OF, calendar=cal)()

    assert line.status == "BREACH"
    assert line.breach_sessions == 5


def test_sleeve_source_is_empty_when_the_week_produced_no_verdicts(db, cal):
    assert sleeve_source(db, window_start=WINDOW_START, as_of=AS_OF, calendar=cal)() == []


def test_equity_source_sums_sleeves_and_excludes_synthetic_portfolios(db):
    """Two real sleeves add up; a drill portfolio does not.

    The synthetic portfolios exist to exercise the machinery, and counting
    their play money as account equity would show a drill as a windfall.
    """
    _equity(db, date(2026, 8, 10), "momentum", 1000.0)
    _equity(db, date(2026, 8, 10), "value_rotation", 1000.0)
    _equity(db, date(2026, 8, 14), "momentum", 1100.0)
    _equity(db, date(2026, 8, 14), "value_rotation", 1000.0)
    _equity(db, date(2026, 8, 14), f"{EXCLUDED_PORTFOLIO_PREFIX}drill", 500_000.0)

    line = equity_source(db, window_start=WINDOW_START, as_of=AS_OF)()

    assert line.latest == 2100.0
    assert line.change_pct == pytest.approx(5.0)
    assert line.currency == "USD"


def test_equity_source_is_none_when_the_window_has_no_snapshots(db):
    assert equity_source(db, window_start=WINDOW_START, as_of=AS_OF)() is None


def test_drills_source_lists_only_the_drill_types_with_no_passing_run(db):
    db.add(
        DrillOutcome(
            epoch_id=1,
            drill_type=DrillType.RESTART_HALT.value,
            passed=True,
            occurred_at=datetime(2026, 8, 12, tzinfo=timezone.utc),
        )
    )
    db.commit()

    assert drills_source(db, epoch_id=1)() == [DrillType.SYNTHETIC_STOP.value]


def test_drills_source_lists_everything_when_no_epoch_is_running(db):
    assert drills_source(db, epoch_id=None)() == sorted(d.value for d in DrillType)


class FakeRedis:
    """Just enough Redis for the two stream reads the digest makes."""

    def __init__(self, streams: dict[str, list[tuple[str, dict[str, str]]]]) -> None:
        self._streams = streams

    async def keys(self, pattern: str) -> list[str]:
        assert pattern == "*:dlq"
        return [name for name in self._streams if name.endswith(":dlq")]

    async def xlen(self, name: str) -> int:
        return len(self._streams.get(name, []))

    async def xrange(self, name: str, min="-", max="+", count=None):
        entries = self._streams.get(name, [])
        lo = 0 if min == "-" else int(str(min).split("-")[0])
        hi = None if max == "+" else int(str(max).split("-")[0])
        out = [
            (eid, data)
            for eid, data in entries
            if int(eid.split("-")[0]) >= lo
            and (hi is None or int(eid.split("-")[0]) <= hi)
        ]
        return out[:count] if count else out


def _ms(day: date) -> int:
    return int(datetime.combine(day, time.min, tzinfo=timezone.utc).timestamp() * 1000)


def test_dlq_source_reports_every_non_empty_dead_letter_queue():
    redis = FakeRedis(
        {
            "stream:fills:dlq": [("1-0", {}), ("2-0", {})],
            "stream:signals:dlq": [],
            "stream:fills": [("3-0", {})],
        }
    )

    assert asyncio.run(dlq_source(redis)()) == {
        "stream:fills:dlq": 2,
        "stream:signals:dlq": 0,
    }


def test_alerts_source_counts_only_the_windows_alerts_by_priority():
    redis = FakeRedis(
        {
            "stream:alerts": [
                (f"{_ms(date(2026, 8, 3))}-0", {"priority": "high"}),
                (f"{_ms(date(2026, 8, 11))}-0", {"priority": "high"}),
                (f"{_ms(date(2026, 8, 12))}-0", {"priority": "low"}),
            ]
        }
    )

    volume = asyncio.run(alerts_source(redis, window_start=WINDOW_START)())

    assert volume.total == 2
    assert volume.by_priority == {"high": 1, "low": 1}
    assert volume.at_least is False


def test_alerts_source_reports_a_floor_when_the_stream_was_trimmed():
    """A trimmed stream can only give a lower bound, and must say so.

    stream:alerts is capped by DEFAULT_STREAM_MAXLEN. On a loud week the
    oldest surviving entry is newer than the window, so the true count is
    unknowable from here — reporting it as exact would understate a bad week
    precisely when it mattered.
    """
    redis = FakeRedis(
        {
            "stream:alerts": [
                (f"{_ms(date(2026, 8, 15))}-0", {"priority": "high"}),
                (f"{_ms(date(2026, 8, 16))}-0", {"priority": "high"}),
            ]
        }
    )

    volume = asyncio.run(alerts_source(redis, window_start=WINDOW_START)())

    assert volume.total == 2
    assert volume.at_least is True


def _start_epoch(db, started: date = date(2026, 8, 3)) -> None:
    db.add(
        GateEpoch(
            id=1,
            label="v2",
            rung=0,
            manifest=MANIFEST,
            started_at=datetime.combine(started, time.min, tzinfo=timezone.utc),
        )
    )
    db.add(
        GateEpochEvent(
            epoch_id=1,
            event_type="started",
            occurred_at=datetime.combine(started, time.min, tzinfo=timezone.utc),
        )
    )
    db.commit()


def _observed(db, days: list[date]) -> None:
    """A verdict for every manifest sleeve on every session — nothing blind."""
    for day in days:
        for sleeve in MANIFEST["sleeves"]:
            db.add(
                DivergenceDaily(
                    sleeve=sleeve,
                    session_date=day,
                    status="OK",
                    baseline_id=MANIFEST["baseline_id"],
                    window_sessions=30,
                    threshold=0.20,
                    metric_value=0.01,
                    created_at=datetime(2026, 8, 17, tzinfo=timezone.utc),
                )
            )
    db.commit()


def test_epoch_source_derives_the_week_number_from_elapsed_sessions(db, cal):
    _start_epoch(db)
    # 2026-08-03 to 2026-08-17 inclusive is 11 weekday sessions -> week 3.
    _observed(db, _weekdays(date(2026, 8, 3), AS_OF))

    line = epoch_source(db, as_of=AS_OF, calendar=cal)()

    assert (line.label, line.week, line.weeks_total) == ("v2", 3, EPOCH_WEEKS)
    assert line.state == "RUNNING"
    assert set(line.criteria) == set(CRITERIA_KEYS)


def test_blind_sessions_do_not_advance_the_epoch_week(db, cal):
    """The clock the digest reports is the paused one, deliberately.

    A blind session is one the monitor did not observe, and D11 neither counts
    it nor lets it break a run. Reporting calendar weeks instead would let an
    epoch "finish" on evidence nobody ever collected — the exact laundering the
    pause rule exists to prevent.
    """
    _start_epoch(db)
    sessions = _weekdays(date(2026, 8, 3), AS_OF)
    _observed(db, sessions[:6])  # the last 5 sessions were never observed

    line = epoch_source(db, as_of=AS_OF, calendar=cal)()

    assert line.week == 2


def test_epoch_source_is_none_when_no_epoch_has_started(db, cal):
    assert epoch_source(db, as_of=AS_OF, calendar=cal)() is None


def test_blind_source_reports_the_sessions_with_no_verdict(db, cal):
    _start_epoch(db)
    sessions = _weekdays(WINDOW_START, AS_OF)
    _observed(db, sessions[:3])

    report = blind_source(
        db,
        window_start=WINDOW_START,
        as_of=AS_OF,
        sleeves=MANIFEST["sleeves"],
        baseline_id=MANIFEST["baseline_id"],
        calendar=cal,
    )()

    assert report.blind_sessions == sessions[3:]
    assert report.total_sessions == len(sessions)


def test_a_week_with_no_verdicts_at_all_is_blind_even_with_no_epoch(db, cal):
    """Today's actual state, and the one a naive digest reports as healthy.

    Before Rung 0 there is no epoch and therefore no pinned sleeve set, so a
    blindness check keyed off the manifest finds nothing to be blind about. The
    monitor being wholly silent is exactly when the operator most needs to be
    told.
    """
    sources = build_sources(
        db, redis=None, as_of=AS_OF, window_start=WINDOW_START, calendar=cal
    )
    report = sources.blind()

    assert report is not None
    assert report.blind_sessions == _weekdays(WINDOW_START, AS_OF)


def test_main_dry_run_prints_the_digest_and_never_sends(db, cal, capsys, monkeypatch):
    """--dry-run is the operator's pre-deploy check; it must not page anyone."""
    sent: list[str] = []

    monkeypatch.setattr(
        evidence_digest, "_open_session", lambda: (db, None)
    )
    monkeypatch.setattr(
        evidence_digest, "_open_redis", lambda: FakeRedis({"stream:alerts": []})
    )
    monkeypatch.setattr(
        evidence_digest,
        "TelegramChannel",
        lambda *a, **k: sent.append("constructed"),
    )

    exit_code = evidence_digest.main(["--as-of", "2026-08-17", "--dry-run"])

    assert exit_code == 0
    assert sent == []
    assert "No epoch running" in capsys.readouterr().out


def test_a_streak_older_than_the_window_is_reported_at_its_true_length(db, cal):
    """The streak is the ladder's number, not the digest's.

    breach_streak stops walking at the scoring floor it is given. Passing the
    window start would cap every streak at one week, so a sleeve two sessions
    from the 10-session trigger would be reported as "×5" — understating the
    single most consequential figure in the message, in the week it matters
    most.
    """
    days = _weekdays(date(2026, 7, 28), AS_OF)  # 15 sessions, all BREACH
    for day in days:
        _verdict(db, "value_rotation", day, "BREACH", metric=9.0)

    (line,) = sleeve_source(db, window_start=WINDOW_START, as_of=AS_OF, calendar=cal)()

    assert line.breach_sessions == len(days)


class BrokenSession:
    """A session whose every read fails, i.e. Postgres is down."""

    def execute(self, *args, **kwargs):
        raise ConnectionError("could not connect to server")

    def scalars(self, *args, **kwargs):
        raise ConnectionError("could not connect to server")

    def get(self, *args, **kwargs):
        raise ConnectionError("could not connect to server")


def test_a_dead_database_still_produces_a_message(cal):
    """The job's one promise is that it never goes quiet.

    Wiring the sources must not itself touch the database: anything read
    outside collect_snapshot's guard raises straight past it, and the operator
    gets no message at all — silence on the very week the database died.
    """
    sources = build_sources(
        BrokenSession(),
        redis=None,
        as_of=AS_OF,
        window_start=WINDOW_START,
        calendar=cal,
    )
    snapshot = collect_snapshot(sources, as_of=AS_OF, window_start=WINDOW_START)
    rendered = render_digest(snapshot)

    assert "MISSING SOURCES" in rendered
    assert "Epoch unavailable" in rendered
    assert "ConnectionError" in rendered


# ---------------------------------------------------------------------------
# Accepted absences (KAN-67) — a gap with a recorded cause is a footnote, and
# a gap without one is the alarm. 2026-08-18 looked like the first for three
# days while actually being the second.
# ---------------------------------------------------------------------------

MIXED_GAPS = """\
🚨 BLIND — the monitor saw nothing on 1 of 5 sessions (2026-08-11)
◻️ ABSENT (accepted) — 2 of 5 sessions have a recorded cause (2026-08-13, 2026-08-18)
Epoch v2 — week 3 of 6 · RUNNING
  divergence green · drawdown green · safety green · drills green · quantum green
Equity 5,012.34 USD (+1.2% wk)
  momentum OK 0.80/5.00 · sector_rotation OK 1.10/5.00
DLQ clear · alerts 3 (2 high, 1 info) · drills due: none"""


def test_accepted_absences_are_counted_apart_from_the_blind_alarm():
    snapshot = _snapshot(
        blind=BlindReport(
            blind_sessions=[date(2026, 8, 11), date(2026, 8, 13), date(2026, 8, 18)],
            total_sessions=5,
            absent_sessions=[date(2026, 8, 13), date(2026, 8, 18)],
        )
    )
    assert render_digest(snapshot) == MIXED_GAPS


ONLY_ACCEPTED = """\
◻️ ABSENT (accepted) — 2 of 5 sessions have a recorded cause (2026-08-13, 2026-08-18)
Epoch v2 — week 3 of 6 · RUNNING
  divergence green · drawdown green · safety green · drills green · quantum green
Equity 5,012.34 USD (+1.2% wk)
  momentum OK 0.80/5.00 · sector_rotation OK 1.10/5.00
DLQ clear · alerts 3 (2 high, 1 info) · drills due: none"""


def test_a_week_whose_every_gap_is_accepted_raises_no_blind_alarm():
    """The 🚨 is for gaps nobody has accounted for; these have a cause on file."""
    snapshot = _snapshot(
        blind=BlindReport(
            blind_sessions=[date(2026, 8, 13), date(2026, 8, 18)],
            total_sessions=5,
            absent_sessions=[date(2026, 8, 13), date(2026, 8, 18)],
        )
    )
    assert render_digest(snapshot) == ONLY_ACCEPTED


ONE_ACCEPTED = """\
◻️ ABSENT (accepted) — 1 of 5 session has a recorded cause (2026-08-18)
Epoch v2 — week 3 of 6 · RUNNING
  divergence green · drawdown green · safety green · drills green · quantum green
Equity 5,012.34 USD (+1.2% wk)
  momentum OK 0.80/5.00 · sector_rotation OK 1.10/5.00
DLQ clear · alerts 3 (2 high, 1 info) · drills due: none"""


def test_a_single_accepted_absence_reads_as_one_session():
    snapshot = _snapshot(
        blind=BlindReport(
            blind_sessions=[date(2026, 8, 18)],
            total_sessions=5,
            absent_sessions=[date(2026, 8, 18)],
        )
    )
    assert render_digest(snapshot) == ONE_ACCEPTED


def test_the_no_pins_fallback_classifies_accepted_absences_too(db, cal):
    """The same date must not render 🚨 on one path and ◻️ on the other.

    The window 2026-08-10..2026-08-17 contains 2026-08-13, which the registry
    accounts for. Before Rung 0 there is no epoch and this fallback is the path
    that actually runs, so it is the one an operator sees.
    """
    sources = build_sources(
        db, redis=None, as_of=AS_OF, window_start=WINDOW_START, calendar=cal
    )
    report = sources.blind()

    assert report is not None
    assert date(2026, 8, 13) in report.blind_sessions
    assert report.absent_sessions == [date(2026, 8, 13)]


def test_the_accepted_absence_footnote_ranks_below_the_missing_sources_caveat():
    """Loudest first. A gap with a cause on record is the quietest line here."""
    snapshot = _snapshot(
        blind=BlindReport(
            blind_sessions=[date(2026, 8, 11), date(2026, 8, 13)],
            total_sessions=5,
            absent_sessions=[date(2026, 8, 13)],
        ),
        dlq=None,
        missing=["dlq (ConnectionError: redis unreachable)"],
    )

    lines = render_digest(snapshot).splitlines()
    positions = [
        next(i for i, line in enumerate(lines) if line.startswith(marker))
        for marker in ("🚨 BLIND", "⚠️ MISSING SOURCES", "◻️ ABSENT")
    ]
    assert positions == sorted(positions)
