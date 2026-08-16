"""KAN-28 — epoch manifest recording and the rung-transition engine.

Every test runs against a fake calendar and a throwaway git repository, so the
suite needs neither the network nor the real checkout's history. The transition
table is exercised through the pure planner (:func:`plan_transition`); the
manifest, drift and write paths go through the real CLI functions against the
sqlite fixture.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

import scripts.ops.record_epoch as record_epoch
from scripts.ops.record_epoch import (
    MAX_RUNG,
    EpochAlreadyRunningError,
    build_manifest,
    evaluate_epoch,
    main,
    manifest_drift,
    money_path_hashes,
    plan_transition,
    record_drill,
    record_event,
    start_epoch,
)
from shared.evidence_store import current_epoch_state, epoch_progress
from shared.models.base import Base
from shared.models.equity_snapshot import EquitySnapshot
from shared.models.evidence import (
    MANIFEST_MONEY_PATH_KEYS,
    DivergenceDaily,
    DivergenceStatus,
    DrillOutcome,
    DrillType,
    EpochState,
    GateEpoch,
    GateEpochEvent,
    validate_manifest,
)
from shared.models.portfolio import Trade

SLEEVES = [
    "momentum",
    "sector_rotation",
    "thematic_momentum",
    "quality_value",
    "earnings_drift",
    "tail_risk_hedge",
]

AS_OF = date(2026, 8, 14)  # a Friday
SCORED_START = date(2026, 6, 1)  # far enough back that the 30-session window closed
NEWEST_BASELINE = "backtest_multi_20260812_101500.json"
NEWEST_BASELINE_PATH = f"output/{NEWEST_BASELINE}"
MEMBERSHIP_PATH = "data/universe/sp500_membership.json"
MEMBERSHIP_BODY = '{"2020-01-01": ["AAPL"]}'


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


class FakeCalendar:
    """A calendar over an explicit session list — no exchange data, no network."""

    def __init__(self, sessions: list[date]) -> None:
        self._sessions = sorted(sessions)
        self._lookup = set(self._sessions)

    def is_trading_day(self, d: date) -> bool:
        return d in self._lookup

    def trading_sessions(self, start: date, end: date) -> list[date]:
        return [d for d in self._sessions if start <= d <= end]


def weekday_sessions(start: date, end: date) -> list[date]:
    days: list[date] = []
    day = start
    while day <= end:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


@pytest.fixture
def cal() -> FakeCalendar:
    return FakeCalendar(weekday_sessions(date(2024, 1, 1), date(2027, 12, 31)))


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    yield db
    db.close()
    engine.dispose()


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=T", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A throwaway checkout carrying every path the manifest pins."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    for name in MANIFEST_MONEY_PATH_KEYS:
        path = root / name
        if path.suffix == ".py":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("MONEY = 1\n")
        else:
            path.mkdir(parents=True, exist_ok=True)
            (path / "runner.py").write_text("MONEY = 1\n")
    (root / "docs").mkdir()
    (root / "docs" / "notes.md").write_text("prose\n")
    (root / "output").mkdir()
    (root / "output" / "backtest_multi_20260701_090000.json").write_text("{}")
    (root / NEWEST_BASELINE_PATH).write_text("{}")
    (root / "data" / "universe").mkdir(parents=True)
    (root / MEMBERSHIP_PATH).write_text(MEMBERSHIP_BODY)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    return root


def _now() -> datetime:
    return datetime(2026, 8, 15, 4, 45, tzinfo=timezone.utc)


def _manifest(repo: Path) -> dict:
    return build_manifest(repo_root=repo, membership_snapshot=MEMBERSHIP_PATH)


# ---------------------------------------------------------------------------
# manifest capture (AC3)
# ---------------------------------------------------------------------------


def test_money_path_hashes_cover_every_pinned_path(repo):
    hashes = money_path_hashes(repo)

    assert set(hashes) == set(MANIFEST_MONEY_PATH_KEYS)
    assert all(len(value) == 40 for value in hashes.values())


def test_build_manifest_records_all_seven_d13_items(repo):
    manifest = _manifest(repo)

    validate_manifest(manifest)  # raises if any item is missing or malformed
    assert manifest["baseline_id"] == NEWEST_BASELINE
    assert manifest["sleeves"] == SLEEVES
    assert set(manifest["weights"]) == set(SLEEVES)
    assert manifest["membership_snapshot"] == MEMBERSHIP_PATH
    assert (
        manifest["membership_snapshot_sha256"]
        == hashlib.sha256(MEMBERSHIP_BODY.encode()).hexdigest()
    )
    assert manifest["divergence"] == {"window_sessions": 30, "threshold": 0.20}
    assert set(manifest["cost_model"]) == {
        "slippage_bps",
        "commission_per_share",
        "commission_minimum",
    }
    assert manifest["money_path"] == money_path_hashes(repo)


def test_build_manifest_overrides_replace_captured_values(repo):
    manifest = build_manifest(
        repo_root=repo,
        membership_snapshot=MEMBERSHIP_PATH,
        overrides={"baseline_id": "backtest_multi_pinned.json"},
    )

    assert manifest["baseline_id"] == "backtest_multi_pinned.json"
    validate_manifest(manifest)


def test_build_manifest_refuses_when_no_baseline_exists(repo):
    for stale in (repo / "output").glob("*.json"):
        stale.unlink()

    with pytest.raises(ValueError, match="baseline"):
        build_manifest(repo_root=repo, membership_snapshot=MEMBERSHIP_PATH)


# ---------------------------------------------------------------------------
# start (AC3)
# ---------------------------------------------------------------------------


def test_start_writes_the_epoch_row_and_a_started_event(session, repo):
    epoch = start_epoch(
        session, label="v2", rung=0, manifest=_manifest(repo), now=_now()
    )

    stored = session.get(GateEpoch, epoch.id)
    assert stored.label == "v2"
    assert stored.rung == 0
    assert stored.manifest["money_path"] == money_path_hashes(repo)
    events = session.scalars(
        select(GateEpochEvent).where(GateEpochEvent.epoch_id == epoch.id)
    ).all()
    assert [event.event_type for event in events] == ["started"]


def test_start_refuses_a_second_running_epoch_and_writes_nothing(session, repo):
    start_epoch(session, label="v2", rung=0, manifest=_manifest(repo), now=_now())

    with pytest.raises(EpochAlreadyRunningError, match="v2"):
        start_epoch(
            session, label="v3", rung=1, manifest=_manifest(repo), now=_now()
        )

    assert session.scalars(select(GateEpoch.label)).all() == ["v2"]


def test_start_allows_a_new_epoch_once_the_previous_one_ended(session, repo):
    first = start_epoch(
        session, label="v2", rung=0, manifest=_manifest(repo), now=_now()
    )
    record_event(session, label="v2", event_type="clean", now=_now())

    second = start_epoch(
        session, label="v3", rung=1, manifest=_manifest(repo), now=_now()
    )

    assert second.id != first.id
    assert second.rung == 1


# ---------------------------------------------------------------------------
# the transition table (AC1, AC5, AC6)
# ---------------------------------------------------------------------------


GREEN = {
    "divergence": "green",
    "drawdown": "green",
    "safety": "green",
    "drills": "green",
    "evidence_quantum": "green",
}


def _plan(**kwargs):
    defaults = dict(
        rung=0,
        criteria=dict(GREEN),
        window_complete=True,
        drift=(),
        previous_breached=False,
        incident_id="inc-1",
    )
    defaults.update(kwargs)
    return plan_transition(**defaults)


def _types(decision) -> list[str]:
    return [event.event_type for event in decision.events]


def _by_type(decision, event_type):
    matches = [e for e in decision.events if e.event_type == event_type]
    assert len(matches) == 1, f"expected exactly one {event_type}: {_types(decision)}"
    return matches[0]


def test_a_clean_epoch_at_rung_zero_promotes_to_rung_one():
    decision = _plan(rung=0)

    assert decision.verdict == EpochState.CLEAN
    assert _by_type(decision, "rung_change").rung_after == 1


def test_a_breach_at_rung_one_de_scales_to_rung_zero():
    decision = _plan(rung=1, criteria={**GREEN, "divergence": "red"})

    assert decision.verdict == EpochState.BREACHED
    assert _by_type(decision, "rung_change").rung_after == 0
    assert "disarmed" not in _types(decision)


def test_a_breach_at_rung_zero_disarms_the_live_account():
    decision = _plan(rung=0, criteria={**GREEN, "drawdown": "red"})

    assert decision.verdict == EpochState.DISARMED
    assert _by_type(decision, "disarmed").rung_after == 0
    assert "rung_change" not in _types(decision)


def test_two_consecutive_breached_epochs_drop_to_rung_zero_with_an_incident():
    decision = _plan(
        rung=2, criteria={**GREEN, "safety": "red"}, previous_breached=True
    )

    assert decision.verdict == EpochState.BREACHED
    assert _by_type(decision, "rung_change").rung_after == 0
    assert "safety_incident" in _types(decision)


def test_a_single_breach_at_rung_two_drops_exactly_one_rung():
    decision = _plan(rung=2, criteria={**GREEN, "safety": "red"})

    assert _by_type(decision, "rung_change").rung_after == 1
    assert "safety_incident" not in _types(decision)


def test_an_evidence_quantum_shortfall_extends_and_never_breaches():
    decision = _plan(criteria={**GREEN, "evidence_quantum": "amber"})

    assert decision.verdict == EpochState.EXTENDED
    assert _types(decision) == ["extended"]


def test_incomplete_drills_extend_the_epoch():
    decision = _plan(criteria={**GREEN, "drills": "amber"})

    assert decision.verdict == EpochState.EXTENDED
    assert _types(decision) == ["extended"]


def test_an_epoch_whose_window_has_not_closed_is_still_running():
    decision = _plan(window_complete=False, criteria={**GREEN, "drills": "amber"})

    assert decision.verdict == EpochState.RUNNING
    assert decision.events == ()


def test_a_breach_acts_immediately_even_before_the_window_closes():
    decision = _plan(rung=1, window_complete=False, criteria={**GREEN, "safety": "red"})

    assert decision.verdict == EpochState.BREACHED
    assert _by_type(decision, "rung_change").rung_after == 0


def test_manifest_drift_restarts_the_epoch():
    decision = _plan(drift=("services/execution",))

    assert decision.verdict == EpochState.RESTARTED
    assert "services/execution" in _by_type(decision, "restarted").reason


def test_a_breach_outranks_drift_so_code_changes_cannot_void_it():
    decision = _plan(
        rung=1, criteria={**GREEN, "divergence": "red"}, drift=("services/execution",)
    )

    assert decision.verdict == EpochState.BREACHED
    assert "restarted" not in _types(decision)


def test_a_clean_epoch_at_the_top_rung_does_not_scale_silently():
    decision = _plan(rung=MAX_RUNG)

    assert decision.verdict == EpochState.CLEAN
    assert "rung_change" not in _types(decision)
    assert any("amendment" in reason for reason in decision.reasons)


def test_promotion_into_rung_three_waits_on_the_capacity_review():
    decision = _plan(rung=2)

    assert decision.verdict == EpochState.CLEAN
    assert "rung_change" not in _types(decision)
    assert any("capacity review" in reason for reason in decision.reasons)


def test_one_incident_produces_one_primary_response_not_three():
    decision = _plan(
        rung=2, criteria={**GREEN, "safety": "red"}, previous_breached=True
    )

    assert {event.incident_id for event in decision.events} == {"inc-1"}
    primaries = [e for e in decision.events if e.detail["chain_role"] == "primary"]
    assert [e.event_type for e in primaries] == ["rung_change"]
    assert len([e for e in decision.events if e.rung_after is not None]) == 1


def test_a_planned_chain_never_writes_a_nonterminal_event_after_a_terminal_one(
    session, repo
):
    """The store reports a post-terminal event as an anomaly — the chain must
    order its records so a legitimate de-scale never looks like one."""
    epoch = start_epoch(
        session, label="v2", rung=2, manifest=_manifest(repo), now=_now()
    )
    decision = _plan(
        rung=2, criteria={**GREEN, "safety": "red"}, previous_breached=True
    )

    for event in decision.events:
        session.add(
            GateEpochEvent(
                epoch_id=epoch.id,
                event_type=event.event_type,
                rung_after=event.rung_after,
                incident_id=event.incident_id,
                reason=event.reason,
                detail=event.detail,
                occurred_at=_now(),
            )
        )
    session.flush()

    state, anomalies = current_epoch_state(session, epoch_id=epoch.id)
    assert state == EpochState.BREACHED
    assert anomalies == []


# ---------------------------------------------------------------------------
# drills (AC2)
# ---------------------------------------------------------------------------


def test_a_passing_drill_row_is_tied_to_the_epoch(session, repo):
    start_epoch(session, label="v2", rung=0, manifest=_manifest(repo), now=_now())

    outcome = record_drill(
        session,
        label="v2",
        drill_type=DrillType.RESTART_HALT.value,
        passed=True,
        detail="killed the container mid-session",
        now=_now(),
    )

    stored = session.get(DrillOutcome, outcome.id)
    assert stored.drill_type == "restart_halt"
    assert stored.passed is True
    assert stored.detail == "killed the container mid-session"


def test_drills_stay_amber_until_both_types_have_a_passing_row(session, cal, repo):
    epoch, _ = _green_epoch(session, cal, repo, drills=())

    record_drill(
        session,
        label="v2",
        drill_type=DrillType.RESTART_HALT.value,
        passed=True,
        now=_now(),
    )
    amber = epoch_progress(
        session, epoch_id=epoch.id, as_of=AS_OF, calendar=cal
    ).criteria["drills"]

    record_drill(
        session,
        label="v2",
        drill_type=DrillType.SYNTHETIC_STOP.value,
        passed=True,
        now=_now(),
    )
    green = epoch_progress(
        session, epoch_id=epoch.id, as_of=AS_OF, calendar=cal
    ).criteria["drills"]

    assert (amber, green) == ("amber", "green")


# ---------------------------------------------------------------------------
# money-path drift (AC4)
# ---------------------------------------------------------------------------


def _commit(root: Path, path: str, body: str) -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", f"touch {path}")


def test_changing_a_money_path_is_drift(repo):
    manifest = _manifest(repo)
    _commit(repo, "services/execution/runner.py", "MONEY = 2\n")

    assert manifest_drift(manifest, repo_root=repo) == ["services/execution"]


def test_changing_a_docs_only_path_is_not_drift(repo):
    manifest = _manifest(repo)
    _commit(repo, "docs/notes.md", "different prose\n")

    assert manifest_drift(manifest, repo_root=repo) == []


def test_changing_the_sleeve_weights_is_drift(repo):
    manifest = _manifest(repo)
    manifest["weights"] = {"momentum": 1.0}

    assert manifest_drift(manifest, repo_root=repo) == ["weights"]


def test_changing_the_membership_snapshot_contents_is_drift(repo):
    manifest = _manifest(repo)
    (repo / MEMBERSHIP_PATH).write_text('{"2020-01-01": ["MSFT"]}')

    assert manifest_drift(manifest, repo_root=repo) == ["membership_snapshot"]


# ---------------------------------------------------------------------------
# evaluate — end to end against the evidence store
# ---------------------------------------------------------------------------


def _add_verdicts(db: Session, cal: FakeCalendar, start: date, end: date) -> None:
    for day in cal.trading_sessions(start, end):
        for sleeve in SLEEVES:
            db.add(
                DivergenceDaily(
                    sleeve=sleeve,
                    session_date=day,
                    status=DivergenceStatus.OK.value,
                    baseline_id=NEWEST_BASELINE,
                    window_sessions=30,
                    threshold=0.20,
                    metric_value=0.01,
                    created_at=_now(),
                )
            )
    db.flush()


def _green_epoch(
    db: Session,
    cal: FakeCalendar,
    repo: Path,
    *,
    rung: int = 0,
    label: str = "v2",
    start: date = SCORED_START,
    equity: float = 4000.0,
    drills: tuple[str, ...] = tuple(d.value for d in DrillType),
) -> tuple[GateEpoch, list[date]]:
    """An otherwise-green, window-complete epoch each test perturbs once."""
    epoch = start_epoch(
        db,
        label=label,
        rung=rung,
        manifest=_manifest(repo),
        now=datetime.combine(start, time(13, 30), tzinfo=timezone.utc),
    )
    days = cal.trading_sessions(start, AS_OF)
    _add_verdicts(db, cal, start, AS_OF)
    for day in days:
        db.add(
            EquitySnapshot(
                portfolio="momentum",
                date=day,
                equity=equity,
                cash=equity - 100.0,
                market_value=100.0,
                created_at=_now(),
            )
        )
    for index in range(20):
        executed_at = datetime.combine(
            days[index % len(days)], time(20, 0), tzinfo=timezone.utc
        )
        db.add(
            Trade(
                ticker="AAPL",
                portfolio="momentum",
                side="sell",
                quantity=10.0,
                price=101.0,
                entry_price=100.0,
                entry_date=executed_at.date() - timedelta(days=5),
                pnl=10.0,
                executed_at=executed_at,
            )
        )
    for drill in drills:
        record_drill(db, label=label, drill_type=drill, passed=True, now=_now())
    db.flush()
    return epoch, days


def _evaluate(db, cal, repo, **kwargs):
    defaults = dict(label="v2", as_of=AS_OF, repo_root=repo, calendar=cal)
    defaults.update(kwargs)
    return evaluate_epoch(db, **defaults)


def test_a_green_window_complete_epoch_evaluates_clean_and_promotes(
    session, cal, repo
):
    _green_epoch(session, cal, repo)

    result = _evaluate(session, cal, repo)

    assert result.verdict == EpochState.CLEAN
    types = [event.event_type for event in result.events_written]
    assert types == ["rung_change", "clean"]
    assert result.rung_after == 1


def test_evaluate_emits_restarted_naming_the_changed_money_path(session, cal, repo):
    _green_epoch(session, cal, repo)
    _commit(repo, "shared/order_ledger.py", "MONEY = 99\n")

    result = _evaluate(session, cal, repo)

    assert result.verdict == EpochState.RESTARTED
    assert result.drift == ["shared/order_ledger.py"]
    assert "shared/order_ledger.py" in result.events_written[0].reason


def test_evaluate_ignores_a_docs_only_change(session, cal, repo):
    _green_epoch(session, cal, repo)
    _commit(repo, "docs/notes.md", "rewritten\n")

    result = _evaluate(session, cal, repo)

    assert result.drift == []
    assert result.verdict == EpochState.CLEAN


def test_an_evidence_quantum_shortfall_extends_a_real_epoch(session, cal, repo):
    _green_epoch(session, cal, repo)
    session.execute(Trade.__table__.delete())
    session.flush()

    result = _evaluate(session, cal, repo)

    assert result.verdict == EpochState.EXTENDED
    assert result.rung_after is None


def test_evaluate_reuses_the_incident_id_of_the_incident_that_caused_it(
    session, cal, repo
):
    _green_epoch(session, cal, repo, rung=2)
    record_event(
        session,
        label="v2",
        event_type="safety_incident",
        reason="unattributed order; halted and demoted momentum",
        detail={"levels": ["safety_halt", "sleeve_demotion"]},
        incident_id="inc-7",
        now=_now(),
    )

    result = _evaluate(session, cal, repo)

    assert result.incident_id == "inc-7"
    assert {event.incident_id for event in result.events_written} == {"inc-7"}
    descales = [e for e in result.events_written if e.rung_after is not None]
    assert [e.rung_after for e in descales] == [1]


def test_evaluate_dry_run_writes_nothing(session, cal, repo):
    epoch, _ = _green_epoch(session, cal, repo)
    before = session.scalars(
        select(GateEpochEvent.id).where(GateEpochEvent.epoch_id == epoch.id)
    ).all()

    result = _evaluate(session, cal, repo, dry_run=True)

    after = session.scalars(
        select(GateEpochEvent.id).where(GateEpochEvent.epoch_id == epoch.id)
    ).all()
    assert result.verdict == EpochState.CLEAN
    assert result.events_written == []
    assert after == before


def test_evaluate_json_is_machine_readable(session, cal, repo):
    _green_epoch(session, cal, repo)

    payload = json.loads(json.dumps(_evaluate(session, cal, repo).to_json()))

    assert payload["epoch"] == "v2"
    assert payload["verdict"] == "CLEAN"
    assert payload["rung"] == 0
    assert payload["rung_after"] == 1
    assert set(payload["criteria"]) == {
        "divergence",
        "drawdown",
        "safety",
        "drills",
        "evidence_quantum",
    }
    assert payload["as_of"] == AS_OF.isoformat()
    assert payload["drift"] == []
    assert payload["events_written"][0]["event_type"] == "rung_change"


def test_evaluate_does_not_duplicate_a_nonterminal_verdict(session, cal, repo):
    _green_epoch(session, cal, repo)
    session.execute(Trade.__table__.delete())
    session.flush()

    first = _evaluate(session, cal, repo)
    second = _evaluate(session, cal, repo)

    assert first.events_written != []
    assert second.events_written == []
    assert second.verdict == EpochState.EXTENDED


def test_evaluate_refuses_to_write_after_a_terminal_event(session, cal, repo):
    epoch, _ = _green_epoch(session, cal, repo)
    record_event(session, label="v2", event_type="breached", now=_now())

    result = _evaluate(session, cal, repo)

    assert result.events_written == []
    assert result.state == EpochState.BREACHED
    assert any("terminal" in reason for reason in result.reasons)
    remaining = session.scalars(
        select(GateEpochEvent.event_type).where(GateEpochEvent.epoch_id == epoch.id)
    ).all()
    assert remaining == ["started", "breached"]


# ---------------------------------------------------------------------------
# the CLI itself (AC3's "exits nonzero", AC7's --json)
# ---------------------------------------------------------------------------


@pytest.fixture
def cli(tmp_path, repo, monkeypatch):
    """Point the CLI at the throwaway repo and a throwaway sqlite database."""
    db_path = tmp_path / "evidence.sqlite"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    engine.dispose()
    monkeypatch.setattr(record_epoch, "_REPO_ROOT", repo)
    monkeypatch.setenv("ALGO_DATABASE_URL", f"sqlite:///{db_path}")
    return db_path


def _read(db_path: Path) -> Session:
    engine = create_engine(f"sqlite:///{db_path}")
    return sessionmaker(bind=engine)()


def test_cli_start_records_the_epoch_and_refuses_a_second_one(cli, capsys):
    first = main(["start", "--label", "v2", "--rung", "0",
                  "--membership-snapshot", MEMBERSHIP_PATH])
    capsys.readouterr()

    second = main(["start", "--label", "v3", "--rung", "1",
                   "--membership-snapshot", MEMBERSHIP_PATH])

    assert (first, second) == (0, 2)
    assert "has not ended" in capsys.readouterr().err
    with _read(cli) as db:
        assert db.scalars(select(GateEpoch.label)).all() == ["v2"]


def test_cli_evaluate_json_round_trips(cli, capsys):
    main(["start", "--label", "v2", "--rung", "0",
          "--membership-snapshot", MEMBERSHIP_PATH])
    capsys.readouterr()

    # The epoch's start is stamped in UTC, so it is the only as-of that is
    # guaranteed not to precede it whatever the runner's local timezone is.
    today = datetime.now(timezone.utc).date().isoformat()
    code = main(["evaluate", "--epoch", "v2", "--as-of", today, "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["epoch"] == "v2"
    # Started today: the 30-session window cannot have closed.
    assert payload["verdict"] == EpochState.RUNNING
    assert payload["events_written"] == []


def test_cli_evaluate_exits_nonzero_when_the_epoch_breaches(cli, capsys):
    main(["start", "--label", "v2", "--rung", "0",
          "--membership-snapshot", MEMBERSHIP_PATH])
    main(["event", "--epoch", "v2", "--type", "safety_incident",
          "--reason", "unattributed order"])
    capsys.readouterr()

    today = datetime.now(timezone.utc).date().isoformat()
    code = main(["evaluate", "--epoch", "v2", "--as-of", today])

    out = capsys.readouterr().out
    assert code == 1
    assert EpochState.DISARMED in out
    with _read(cli) as db:
        recorded = db.scalars(
            select(GateEpochEvent.event_type).order_by(GateEpochEvent.id)
        ).all()
        assert recorded == ["started", "safety_incident", "breached", "disarmed"]


def test_cli_drill_records_a_failure(cli, capsys):
    main(["start", "--label", "v2", "--rung", "0",
          "--membership-snapshot", MEMBERSHIP_PATH])
    capsys.readouterr()

    code = main(["drill", "--epoch", "v2", "--type", "synthetic_stop", "--failed"])

    assert code == 0
    assert "FAILED" in capsys.readouterr().out
    with _read(cli) as db:
        outcome = db.scalars(select(DrillOutcome)).one()
        assert (outcome.drill_type, outcome.passed) == ("synthetic_stop", False)
