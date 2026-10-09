"""Tests for the sleeve-cash top-up tool (KAN-113).

The book is a small paper ledger: three graded sleeves, one of them holding a
position and an open buy, and a drill sleeve. Every write path goes through
``main`` where it can, so the TTY, phrase, artifact and verification rules are
exercised exactly as the operator meets them.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from scripts.ops import topup_sleeve_cash as tool
from scripts.ops.topup_sleeve_cash import (
    CONFIRMATION,
    TopupRefusedError,
    allocate,
    apply_topup,
    main,
    plan_topup,
    requested_amounts,
    write_exclusive,
)
from shared.broker_state import BrokerAccountSnapshot
from shared.capital_flows import recorded_flows
from shared.config import load_config
from shared.models import (
    Base,
    CapitalAdjustment,
    OrderIntent,
    PortfolioConfig,
    Position,
)

ACCOUNT = "DUN551088"
CREATED = datetime(2026, 9, 24, 13, 30, tzinfo=timezone.utc)
#: 14:00 SGT on a Friday: well outside the 04:00-07:00 SGT paper run.
NOW = datetime(2026, 10, 9, 6, 0, tzinfo=timezone.utc)
WEIGHTS = {"momentum": 0.5, "sector_rotation": 0.3, "thematic_momentum": 0.2}


def _book(tmp_path):
    url = f"sqlite:///{tmp_path / 'book.db'}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        for name, capital, cash in (
            ("momentum", 20_000.0, 2_000.0),
            ("sector_rotation", 15_000.0, 15_000.0),
            ("thematic_momentum", 14_000.0, 3_000.0),
            ("__drill__", 500.0, 500.0),
        ):
            session.add(PortfolioConfig(
                portfolio=name, capital=capital, cash=cash,
                created_at=CREATED, updated_at=CREATED,
            ))
        # momentum holds $18,000 at its last mark; thematic $11,000.
        for portfolio, ticker, con_id, qty, price in (
            ("momentum", "NVDA", 4815, 100.0, 180.0),
            ("thematic_momentum", "ARKW", 168647416, 100.0, 110.0),
        ):
            session.add(Position(
                account_id=ACCOUNT, ticker=ticker, portfolio=portfolio,
                con_id=con_id, exchange="SMART", currency="USD",
                quantity=qty, avg_entry_price=price, current_price=price,
                peak_price=price, highest_price_since_entry=price,
                opened_at=CREATED, status="open",
            ))
        # thematic has a working buy: 10 @ 150 = 1,500 + $1 commission.
        session.add(OrderIntent(
            recommendation_id="sleeve-2026-10-08-DUN551088-paper-"
                              "thematic_momentum-ARKK-buy",
            account_id=ACCOUNT, mode="paper", portfolio="thematic_momentum",
            con_id=1, symbol="ARKK", exchange="SMART", currency="USD",
            action="BUY", requested_quantity=10.0, limit_price=150.0,
            order_type="LMT", reserved_notional=1_500.0,
            status="SUBMITTED", filled_quantity=0.0,
            created_at=CREATED, updated_at=CREATED,
        ))
        session.commit()
    return url, engine


def _state(engine) -> dict:
    """Every row of every table: 'writes nothing' means this is unchanged."""
    with engine.connect() as conn:
        return {
            table.name: sorted(
                (tuple(map(repr, row)) for row in conn.execute(table.select())),
            )
            for table in Base.metadata.sorted_tables
        }


def _cash(engine, portfolio) -> tuple[float, float]:
    with Session(engine) as session:
        row = session.scalar(select(PortfolioConfig).where(
            PortfolioConfig.portfolio == portfolio
        ))
        return float(row.cash), float(row.capital)


def _snapshot(*, cash=60_000.0, account=ACCOUNT, nav_sgd=135_000.0, at=NOW):
    return BrokerAccountSnapshot(
        account_id=account,
        mode="paper" if account.startswith("DU") else "live",
        base_currency="SGD",
        trading_currency="USD",
        net_liquidation_base=nav_sgd,
        fx_base_per_trading=1.35,
        net_liquidation_trading_equivalent=nav_sgd / 1.35,
        settled_cash_trading=cash,
        fx_source="IB ExchangeRate",
        fx_captured_at=at,
        captured_at=at,
    )


@pytest.fixture(autouse=True)
def _pinned(monkeypatch):
    monkeypatch.setattr(tool, "_now", lambda: NOW)
    monkeypatch.setattr(tool, "capital_allocations", lambda: dict(WEIGHTS))
    reads: list[dict] = []

    def fake_read(**kwargs):
        reads.append(kwargs)
        return _snapshot(at=tool._now())

    monkeypatch.setattr(tool, "read_broker_snapshot", fake_read)
    return reads


def _confirm(monkeypatch, answer=CONFIRMATION, tty=True):
    asked: list = []
    monkeypatch.setattr("sys.stdin.isatty", lambda: tty, raising=False)
    monkeypatch.setattr(
        "builtins.input", lambda *a: asked.append(a) or answer
    )
    return asked


def _cli(url, tmp_path, *extra):
    return main([
        "--database-url", url,
        "--artifact-dir", str(tmp_path / "reconciliation"),
        "--account", ACCOUNT,
        *extra,
    ])


def _artifacts(tmp_path) -> list[dict]:
    directory = tmp_path / "reconciliation"
    if not directory.exists():
        return []
    return [
        json.loads(path.read_text())
        for path in sorted(directory.glob("topup-sleeve-cash-*.json"))
    ]


def _config():
    return load_config("config/default.yaml")


# --------------------------------------------------------------------------
# The dry run: exact before/after, every check, nothing written.
# --------------------------------------------------------------------------


def test_the_dry_run_shows_before_and_after_and_writes_nothing(
    tmp_path, capsys
):
    url, engine = _book(tmp_path)
    before = _state(engine)

    assert _cli(url, tmp_path, "--portfolio", "momentum",
                "--amount-usd", "5000") == 0

    out = capsys.readouterr().out
    assert "$2,000.00" in out and "+5,000.00" in out and "$7,000.00" in out
    # Graded ledger capital: cash 20,000 + positions 29,000 = 49,000 -> 54,000.
    assert "$49,000.00 -> $54,000.00" in out
    assert "max_deployable_usd $100,000.00" in out
    assert "USD cash $60,000.00" in out
    for name in (
        "paper account", "amount", "sleeve exists", "within max_deployable_usd",
        "broker account matches", "IB backs the credit",
        "within today's deployable capital", "not a repeat",
        "outside the paper-run window",
    ):
        assert f"[ok  ] {name}" in out
    assert "Dry-run only" in out
    assert _state(engine) == before
    assert not (tmp_path / "reconciliation").exists()


def test_planning_issues_only_reads(tmp_path):
    _, engine = _book(tmp_path)
    statements: list[str] = []
    event.listen(
        engine, "before_cursor_execute",
        lambda conn, cursor, statement, *a: statements.append(statement),
    )
    with Session(engine) as session:
        plan = plan_topup(
            session, kind="allocate",
            amounts=requested_amounts("allocate", amount=10_000,
                                      weights=WEIGHTS),
            account=ACCOUNT, config=_config(),
            broker_snapshot=_snapshot(), now=NOW,
        )
        session.rollback()
    assert plan.legs and not plan.problems
    assert statements
    assert all(
        s.lstrip().upper().startswith(("SELECT", "PRAGMA")) for s in statements
    )


def test_the_dry_run_on_postgres_is_read_only(tmp_path, monkeypatch):
    """The CLI's first statement on Postgres is SET TRANSACTION READ ONLY."""
    url, _ = _book(tmp_path)
    seen: list[str] = []
    real = tool.create_engine

    def recording_engine(u):
        engine = real(u)
        engine.dialect.name = "postgresql"
        event.listen(
            engine, "before_cursor_execute",
            lambda conn, cursor, statement, *a: seen.append(statement),
            retval=False,
        )
        return engine

    monkeypatch.setattr(tool, "create_engine", recording_engine)
    # sqlite cannot run the Postgres statement; prove it is the first one sent.
    with pytest.raises(Exception):
        _cli(url, tmp_path, "--portfolio", "momentum", "--amount-usd", "10")
    assert seen and seen[0] == "SET TRANSACTION READ ONLY"


# --------------------------------------------------------------------------
# --apply: credit, allocate, transfer — atomic, recorded, verified.
# --------------------------------------------------------------------------


def test_apply_credits_one_sleeve_and_records_the_flow(
    tmp_path, monkeypatch, capsys
):
    url, engine = _book(tmp_path)
    _confirm(monkeypatch)

    assert _cli(url, tmp_path, "--portfolio", "momentum",
                "--amount-usd", "5000", "--apply") == 0

    assert _cash(engine, "momentum") == (7_000.0, 25_000.0)
    assert _cash(engine, "sector_rotation") == (15_000.0, 15_000.0)
    with Session(engine) as session:
        flows = recorded_flows(session)
    assert [(f.portfolio, f.amount) for f in flows] == [("momentum", 5_000.0)]
    assert flows[0].reason.startswith("KAN-113 credit flow ")
    assert flows[0].account_id == ACCOUNT
    out = capsys.readouterr().out
    assert "MISMATCH" not in out
    [artifact] = _artifacts(tmp_path)
    assert artifact["outcome"] == "applied"
    assert artifact["broker_check"] == "verified"
    assert artifact["broker"]["settled_cash_usd"] == 60_000.0
    assert artifact["verification"]["problems"] == []
    assert artifact["applied"]["legs"][0]["cash_after"] == 7_000.0


def test_apply_allocates_by_weights_to_the_cent(tmp_path, monkeypatch):
    url, engine = _book(tmp_path)
    _confirm(monkeypatch)

    assert _cli(url, tmp_path, "--allocate", "10000.01", "--apply") == 0

    assert _cash(engine, "momentum")[0] == pytest.approx(2_000 + 5_000.01)
    assert _cash(engine, "sector_rotation")[0] == pytest.approx(15_000 + 3_000)
    assert _cash(engine, "thematic_momentum")[0] == pytest.approx(3_000 + 2_000)
    assert _cash(engine, "__drill__") == (500.0, 500.0)
    with Session(engine) as session:
        flows = recorded_flows(session)
    assert sum(f.amount for f in flows) == pytest.approx(10_000.01)
    assert len({f.reason for f in flows}) == 1


def test_allocation_remainder_goes_to_the_largest_weight():
    shares = allocate(100.0, {"a": 1 / 3, "b": 1 / 3 + 1e-9, "c": 1 / 3 - 1e-9})
    assert sum(shares.values()) == pytest.approx(100.0, abs=1e-9)
    assert shares["b"] == 33.34


def test_allocation_refuses_weights_that_do_not_sum_to_one():
    with pytest.raises(TopupRefusedError, match="sum to"):
        allocate(100.0, {"a": 0.5, "b": 0.4})


def test_apply_transfers_between_sleeves_without_new_money(
    tmp_path, monkeypatch, _pinned
):
    url, engine = _book(tmp_path)
    _confirm(monkeypatch)

    assert _cli(url, tmp_path, "--from", "sector_rotation", "--to",
                "momentum", "--amount-usd", "4000", "--apply") == 0

    assert _cash(engine, "sector_rotation") == (11_000.0, 11_000.0)
    assert _cash(engine, "momentum") == (6_000.0, 24_000.0)
    with Session(engine) as session:
        flows = recorded_flows(session)
    assert sorted((f.portfolio, f.amount) for f in flows) == [
        ("momentum", 4_000.0), ("sector_rotation", -4_000.0),
    ]
    # A transfer brings no money in, so IB is not consulted.
    assert _pinned == []
    [artifact] = _artifacts(tmp_path)
    assert artifact["broker_check"] == "not needed (transfer)"


def test_a_failure_inside_apply_rolls_everything_back(
    tmp_path, monkeypatch, capsys
):
    url, engine = _book(tmp_path)
    before = _state(engine)
    _confirm(monkeypatch)

    real_add = Session.add

    def exploding_add(self, instance, *a, **k):
        if isinstance(instance, CapitalAdjustment) and (
            instance.portfolio == "thematic_momentum"
        ):
            raise RuntimeError("disk on fire")
        return real_add(self, instance, *a, **k)

    monkeypatch.setattr(Session, "add", exploding_add)

    assert _cli(url, tmp_path, "--allocate", "10000", "--apply") == 1

    monkeypatch.setattr(Session, "add", real_add)
    assert _state(engine) == before
    out = capsys.readouterr().out
    assert "disk on fire" in out and "rolled back completely" in out
    [artifact] = _artifacts(tmp_path)
    assert artifact["outcome"] == "rolled_back"
    assert "disk on fire" in artifact["failure"]
    assert artifact["applied"] is None


def test_a_book_that_moved_since_the_plan_is_refused_under_the_lock(tmp_path):
    _, engine = _book(tmp_path)
    with Session(engine) as session:
        plan = plan_topup(
            session, kind="credit", amounts=[("momentum", 1_000.0)],
            account=ACCOUNT, config=_config(), broker_snapshot=_snapshot(),
            now=NOW,
        )
    # A fill lands between the dry run and the write.
    with Session(engine) as session:
        row = session.scalar(select(PortfolioConfig).where(
            PortfolioConfig.portfolio == "momentum"
        ))
        row.cash = 1_500.0
        session.commit()
    before = _state(engine)
    with Session(engine) as session:
        with pytest.raises(TopupRefusedError, match="the book moved"):
            apply_topup(session, plan, confirm=CONFIRMATION,
                        operator="test", note="t", now=NOW)
    assert _state(engine) == before


def test_apply_topup_demands_the_phrase_and_a_clean_plan(tmp_path):
    _, engine = _book(tmp_path)
    with Session(engine) as session:
        plan = plan_topup(
            session, kind="credit", amounts=[("momentum", 1_000.0)],
            account="U1234567", config=_config(), skip_broker_check=True,
            now=NOW,
        )
        with pytest.raises(TopupRefusedError, match="exact confirmation"):
            apply_topup(session, plan, confirm="yes", operator="t", note="t")
        with pytest.raises(TopupRefusedError, match="refusals"):
            apply_topup(session, plan, confirm=CONFIRMATION, operator="t",
                        note="t")


# --------------------------------------------------------------------------
# Refusals.
# --------------------------------------------------------------------------


def _refused(capsys, *names):
    out = capsys.readouterr().out
    for name in names:
        assert f"[FAIL] {name}" in out, out
    assert "Refusing to apply" in out
    return out


def test_a_live_account_is_refused(tmp_path, capsys, monkeypatch):
    url, engine = _book(tmp_path)
    before = _state(engine)
    monkeypatch.setattr(
        tool, "read_broker_snapshot",
        lambda **k: _snapshot(account="U1234567"),
    )
    assert main([
        "--database-url", url, "--account", "U1234567",
        "--portfolio", "momentum", "--amount-usd", "100", "--apply",
    ]) == 1
    _refused(capsys, "paper account")
    assert _state(engine) == before


def test_exceeding_max_deployable_is_refused(tmp_path, capsys):
    url, _ = _book(tmp_path)
    # 49,000 graded + 51,001 > the configured 100,000.
    assert _cli(url, tmp_path, "--portfolio", "momentum",
                "--amount-usd", "51001") == 1
    _refused(capsys, "within max_deployable_usd")


def test_exceeding_todays_deployable_capital_is_refused(
    tmp_path, capsys, monkeypatch
):
    url, _ = _book(tmp_path)
    # NAV 67,500 SGD / 1.35 = 50,000 USD deployable; 49,000 + 2,000 > that.
    monkeypatch.setattr(
        tool, "read_broker_snapshot", lambda **k: _snapshot(nav_sgd=67_500.0)
    )
    assert _cli(url, tmp_path, "--portfolio", "momentum",
                "--amount-usd", "2000") == 1
    _refused(capsys, "within today's deployable capital")


def test_ib_not_backing_the_credit_is_refused(tmp_path, capsys, monkeypatch):
    url, _ = _book(tmp_path)
    # Ledger cash is 20,500 across every sleeve; +5,000 needs 25,500 at IB.
    monkeypatch.setattr(
        tool, "read_broker_snapshot", lambda **k: _snapshot(cash=25_000.0)
    )
    assert _cli(url, tmp_path, "--portfolio", "momentum",
                "--amount-usd", "5000") == 1
    out = _refused(capsys, "IB backs the credit")
    assert "$25,500.00" in out


def test_an_unreadable_gateway_is_a_refusal_not_a_pass(
    tmp_path, capsys, monkeypatch
):
    url, _ = _book(tmp_path)

    def down(**kwargs):
        raise ConnectionRefusedError("Gateway down")

    monkeypatch.setattr(tool, "read_broker_snapshot", down)
    assert _cli(url, tmp_path, "--portfolio", "momentum",
                "--amount-usd", "100") == 1
    out = _refused(capsys, "IB backs the credit")
    assert "Gateway down" in out


def test_skip_broker_check_is_explicit_and_audited(
    tmp_path, monkeypatch, capsys, _pinned
):
    url, _ = _book(tmp_path)
    _confirm(monkeypatch)
    assert _cli(url, tmp_path, "--portfolio", "momentum", "--amount-usd",
                "100", "--skip-broker-check", "--apply") == 0
    assert _pinned == []
    out = capsys.readouterr().out
    assert "SKIPPED (--skip-broker-check)" in out
    [artifact] = _artifacts(tmp_path)
    assert artifact["broker"] is None
    assert artifact["broker_check"].startswith("SKIPPED")


def test_a_transfer_that_strands_the_sources_reservations_is_refused(
    tmp_path, capsys
):
    url, engine = _book(tmp_path)
    before = _state(engine)
    # thematic holds 3,000 and has 1,501 reserved: at most 1,499 can leave.
    assert _cli(url, tmp_path, "--from", "thematic_momentum", "--to",
                "momentum", "--amount-usd", "1500") == 1
    out = _refused(capsys, "source keeps its reservations")
    assert "$1,501.00" in out
    assert _state(engine) == before
    assert _cli(url, tmp_path, "--from", "thematic_momentum", "--to",
                "momentum", "--amount-usd", "1499") == 0


def test_an_unknown_or_synthetic_sleeve_is_refused(tmp_path, capsys):
    url, _ = _book(tmp_path)
    assert _cli(url, tmp_path, "--portfolio", "momentum_x",
                "--amount-usd", "10") == 1
    _refused(capsys, "sleeve exists")
    assert _cli(url, tmp_path, "--portfolio", "__drill__",
                "--amount-usd", "10") == 1
    _refused(capsys, "sleeve exists")


@pytest.mark.parametrize("amount", ["0", "-5", "nan", "inf", "10.001"])
def test_a_bad_amount_is_refused(tmp_path, capsys, amount):
    url, engine = _book(tmp_path)
    before = _state(engine)
    assert _cli(url, tmp_path, "--portfolio", "momentum",
                "--amount-usd", amount) == 2
    assert "amount" in capsys.readouterr().err
    assert _state(engine) == before


#: 04:30 SGT, inside the paper run.
IN_RUN = datetime(2026, 10, 8, 20, 30, tzinfo=timezone.utc)


def test_the_paper_run_window_warns_in_a_dry_run_and_refuses_apply(
    tmp_path, capsys, monkeypatch
):
    url, engine = _book(tmp_path)
    before = _state(engine)
    monkeypatch.setattr(tool, "_now", lambda: IN_RUN)
    args = ("--portfolio", "momentum", "--amount-usd", "10",
            "--skip-broker-check")

    # The preview still runs, and says what --apply would do.
    assert _cli(url, tmp_path, *args) == 0
    out = capsys.readouterr().out
    assert "[WARN] outside the paper-run window -- WARNING:" in out
    assert "--apply would refuse now" in out

    asked = _confirm(monkeypatch)
    assert _cli(url, tmp_path, *args, "--apply") == 1
    _refused(capsys, "outside the paper-run window")
    assert asked == []
    assert _state(engine) == before


def _plan(session, **kwargs):
    defaults = dict(
        kind="credit", amounts=[("momentum", 1_000.0)], account=ACCOUNT,
        config=_config(), broker_snapshot=_snapshot(), now=NOW,
        for_apply=True,
    )
    defaults.update(kwargs)
    return plan_topup(session, **defaults)


def test_the_window_is_judged_again_at_the_write(tmp_path):
    """Planned at 03:58 SGT, phrase typed at 04:03: refused under the lock."""
    _, engine = _book(tmp_path)
    planned_at = datetime(2026, 10, 8, 19, 58, tzinfo=timezone.utc)
    with Session(engine) as session:
        plan = _plan(session, now=planned_at,
                     broker_snapshot=_snapshot(at=planned_at))
        assert not plan.problems
    before = _state(engine)
    with Session(engine) as session:
        with pytest.raises(TopupRefusedError, match="04:00-07:00 SGT"):
            apply_topup(
                session, plan, confirm=CONFIRMATION, operator="t", note="t",
                now=planned_at + timedelta(minutes=5),
            )
    assert _state(engine) == before


def _snapshot_row(engine, written):
    from shared.models import EquitySnapshot

    with Session(engine) as session:
        session.add(EquitySnapshot(
            portfolio="momentum", date=date(2026, 10, 9),
            session_date=date(2026, 10, 8), equity=20_000.0, cash=2_000.0,
            market_value=18_000.0, created_at=written,
        ))
        session.commit()


def test_a_snapshot_in_the_last_15_minutes_warns_then_refuses(
    tmp_path, capsys, monkeypatch
):
    """A late or manual paper run is writing: a flow now could be misfiled."""
    url, engine = _book(tmp_path)
    _snapshot_row(engine, NOW - timedelta(minutes=10))
    before = _state(engine)

    assert _cli(url, tmp_path, "--portfolio", "momentum",
                "--amount-usd", "10") == 0
    assert "[WARN] no snapshot in the last 15 minutes" in (
        capsys.readouterr().out
    )
    _confirm(monkeypatch)
    assert _cli(url, tmp_path, "--portfolio", "momentum",
                "--amount-usd", "10", "--apply") == 1
    _refused(capsys, "no snapshot in the last 15 minutes")
    assert _state(engine) == before


def test_a_snapshot_written_after_the_plan_is_caught_at_the_write(tmp_path):
    _, engine = _book(tmp_path)
    with Session(engine) as session:
        plan = _plan(session)
    _snapshot_row(engine, NOW + timedelta(minutes=1))
    with Session(engine) as session:
        with pytest.raises(TopupRefusedError, match="paper run"):
            apply_topup(session, plan, confirm=CONFIRMATION, operator="t",
                        note="t", now=NOW + timedelta(minutes=2))


def test_a_stale_broker_snapshot_is_refused_at_the_write(tmp_path):
    _, engine = _book(tmp_path)
    with Session(engine) as session:
        plan = _plan(session)
    before = _state(engine)
    with Session(engine) as session:
        with pytest.raises(TopupRefusedError, match="minutes old"):
            apply_topup(session, plan, confirm=CONFIRMATION, operator="t",
                        note="t", now=NOW + timedelta(minutes=6))
    assert _state(engine) == before
    # Inside the limit it goes through.
    with Session(engine) as session:
        apply_topup(session, plan, confirm=CONFIRMATION, operator="t",
                    note="t", now=NOW + timedelta(minutes=4))
    assert _cash(engine, "momentum")[0] == 3_000.0


def test_the_flow_is_stamped_at_the_write_not_at_the_prompt(
    tmp_path, monkeypatch
):
    """No ``now``: the stamp is taken under the lock, after the checks
    (the database clock on Postgres; the process clock on sqlite)."""
    _, engine = _book(tmp_path)
    with Session(engine) as session:
        plan = _plan(session)
    written = NOW + timedelta(minutes=3)
    monkeypatch.setattr(tool, "_now", lambda: written)
    with Session(engine) as session:
        applied = apply_topup(session, plan, confirm=CONFIRMATION,
                              operator="t", note="t")
    with Session(engine) as session:
        [flow] = recorded_flows(session)
    assert flow.at == written
    assert applied.applied_at == written.isoformat()


def test_the_database_clock_is_used_on_postgres():
    seen = []

    class Fake:
        def get_bind(self):
            return type("B", (), {"dialect": type("D", (), {"name": "postgresql"})})()

        def scalar(self, statement):
            seen.append(str(statement))
            return datetime(2026, 10, 9, 14, 0, tzinfo=timezone(timedelta(hours=8)))

    assert tool._db_now(Fake()) == NOW
    assert seen == ["SELECT clock_timestamp()"]


def test_one_mode_at_a_time(tmp_path):
    url, _ = _book(tmp_path)
    with pytest.raises(SystemExit):
        _cli(url, tmp_path, "--portfolio", "momentum", "--allocate", "10")
    with pytest.raises(SystemExit):
        _cli(url, tmp_path, "--from", "momentum", "--amount-usd", "10")
    with pytest.raises(SystemExit):
        main(["--database-url", url, "--allocate", "10"])  # no --account


def test_apply_without_a_tty_is_refused(tmp_path, monkeypatch, capsys):
    url, engine = _book(tmp_path)
    before = _state(engine)
    asked = _confirm(monkeypatch, tty=False)
    assert _cli(url, tmp_path, "--portfolio", "momentum",
                "--amount-usd", "10", "--apply") == 2
    assert "interactive TTY" in capsys.readouterr().err
    assert asked == []
    assert _state(engine) == before
    assert _artifacts(tmp_path) == []


def test_apply_with_the_wrong_phrase_is_refused(tmp_path, monkeypatch, capsys):
    url, engine = _book(tmp_path)
    before = _state(engine)
    _confirm(monkeypatch, answer="top up sleeve cash")
    assert _cli(url, tmp_path, "--portfolio", "momentum",
                "--amount-usd", "10", "--apply") == 2
    assert "exact confirmation" in capsys.readouterr().err
    assert _state(engine) == before


def test_an_unwritable_artifact_dir_refuses_before_any_write(
    tmp_path, monkeypatch, capsys
):
    url, engine = _book(tmp_path)
    before = _state(engine)
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("a file where the directory should be")
    asked = _confirm(monkeypatch)
    assert main([
        "--database-url", url, "--artifact-dir", str(blocker / "sub"),
        "--account", ACCOUNT, "--portfolio", "momentum",
        "--amount-usd", "10", "--apply",
    ]) == 2
    assert "not writable" in capsys.readouterr().err
    assert asked == []
    assert _state(engine) == before


# --------------------------------------------------------------------------
# Idempotency and the audit artifact.
# --------------------------------------------------------------------------


def test_an_identical_flow_within_a_day_is_refused_unless_allowed(
    tmp_path, monkeypatch, capsys
):
    url, engine = _book(tmp_path)
    _confirm(monkeypatch)
    args = ("--portfolio", "momentum", "--amount-usd", "500", "--apply")
    assert _cli(url, tmp_path, *args) == 0
    capsys.readouterr()

    assert _cli(url, tmp_path, *args) == 1
    _refused(capsys, "not a repeat")
    assert _cash(engine, "momentum")[0] == 2_500.0

    # A different amount is not a repeat; neither is anything a day later.
    assert _cli(url, tmp_path, "--portfolio", "momentum",
                "--amount-usd", "501") == 0
    monkeypatch.setattr(tool, "_now", lambda: NOW + timedelta(hours=25))
    assert _cli(url, tmp_path, "--portfolio", "momentum",
                "--amount-usd", "500") == 0

    monkeypatch.setattr(tool, "_now", lambda: NOW)
    assert _cli(url, tmp_path, *args, "--allow-repeat") == 0
    assert _cash(engine, "momentum")[0] == 3_000.0


def test_two_applies_in_one_second_never_share_an_artifact(
    tmp_path, monkeypatch
):
    url, _ = _book(tmp_path)
    _confirm(monkeypatch)
    assert _cli(url, tmp_path, "--portfolio", "momentum",
                "--amount-usd", "100", "--apply") == 0
    assert _cli(url, tmp_path, "--portfolio", "momentum",
                "--amount-usd", "200", "--apply") == 0
    artifacts = _artifacts(tmp_path)
    assert len(artifacts) == 2
    assert {a["applied"]["legs"][0]["amount"] for a in artifacts} == {
        100.0, 200.0,
    }


def test_write_exclusive_never_overwrites(tmp_path):
    first = write_exclusive(tmp_path, "topup-sleeve-cash-x", {"n": 1})
    second = write_exclusive(tmp_path, "topup-sleeve-cash-x", {"n": 2})
    assert first != second
    assert json.loads(first.read_text()) == {"n": 1}
    assert json.loads(second.read_text()) == {"n": 2}


def test_verify_flags_a_book_that_moved_after_the_commit(tmp_path):
    _, engine = _book(tmp_path)
    with Session(engine) as session:
        plan = plan_topup(
            session, kind="credit", amounts=[("momentum", 1_000.0)],
            account=ACCOUNT, config=_config(), broker_snapshot=_snapshot(),
            now=NOW,
        )
        applied = apply_topup(session, plan, confirm=CONFIRMATION,
                              operator="t", note="t", now=NOW)
    with Session(engine) as session:
        row = session.scalar(select(PortfolioConfig).where(
            PortfolioConfig.portfolio == "momentum"
        ))
        row.cash = 1.0
        session.commit()
    with Session(engine) as session:
        result = tool.verify(session, applied, plan)
    # The projector moves cash too: a fill after the commit is not a failure,
    # but the line says what it may be.
    assert result.problems == ()
    assert any(
        "momentum cash" in line and "may be a fill that landed after commit"
        in line for line in result.lines
    )
    # capital is this tool's alone: a mismatch there IS a problem.
    with Session(engine) as session:
        row = session.scalar(select(PortfolioConfig).where(
            PortfolioConfig.portfolio == "momentum"
        ))
        row.capital = 1.0
        session.commit()
    with Session(engine) as session:
        result = tool.verify(session, applied, plan)
    assert any("momentum capital" in p for p in result.problems)
    assert not any("capital_adjustments" in p for p in result.problems)
    # ...and so are the flow rows.
    with Session(engine) as session:
        session.execute(CapitalAdjustment.__table__.delete())
        session.commit()
    with Session(engine) as session:
        result = tool.verify(session, applied, plan)
    assert any("capital_adjustments" in p for p in result.problems)


def test_the_flow_timestamp_is_the_commit_instant(tmp_path):
    _, engine = _book(tmp_path)
    with Session(engine) as session:
        plan = plan_topup(
            session, kind="credit", amounts=[("momentum", 1_000.0)],
            account=ACCOUNT, config=_config(), broker_snapshot=_snapshot(),
            now=NOW,
        )
        apply_topup(session, plan, confirm=CONFIRMATION, operator="ops",
                    note="deposit 2026-10-09", now=NOW)
    with Session(engine) as session:
        [flow] = recorded_flows(session)
        config_row = session.scalar(select(PortfolioConfig).where(
            PortfolioConfig.portfolio == "momentum"
        ))
        adjustment = session.scalar(select(CapitalAdjustment))
    assert flow.at == NOW
    assert adjustment.operator == "ops"
    assert adjustment.reason.endswith(": deposit 2026-10-09")
    updated = config_row.updated_at
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    assert updated == NOW


def test_recorded_flows_is_empty_without_the_table(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'bare.db'}")
    PortfolioConfig.__table__.create(engine)
    with Session(engine) as session:
        assert recorded_flows(session) == []


def test_the_paper_run_window_is_in_sgt():
    # Guards the fixture: NOW must be outside the paper-run window.
    assert not tool.in_paper_run_window(NOW)
    assert tool.in_paper_run_window(
        datetime.combine(date(2026, 10, 9), datetime.min.time(),
                         tzinfo=timezone.utc) - timedelta(hours=3, minutes=45)
    )


def test_an_applied_credit_reads_as_a_flow_not_a_return(tmp_path, monkeypatch):
    """End to end: the tool's own record is what the readers adjust by."""
    from shared.evidence_store import equity_series, max_drawdown_pct
    from shared.models import EquitySnapshot

    url, engine = _book(tmp_path)
    with Session(engine) as session:
        # momentum: 20,000 on 10-07, 19,000 on 10-08 (both written before
        # the credit), then 24,000 on 10-09 after +5,000: flat, not +26%.
        for session_date, equity, written in (
            (date(2026, 10, 7), 20_000.0, NOW - timedelta(days=1, hours=10)),
            (date(2026, 10, 8), 19_000.0, NOW - timedelta(hours=10)),
        ):
            session.add(EquitySnapshot(
                portfolio="momentum", date=session_date + timedelta(days=1),
                session_date=session_date, equity=equity, cash=equity,
                market_value=0.0, created_at=written,
            ))
        session.commit()
    _confirm(monkeypatch)
    assert _cli(url, tmp_path, "--portfolio", "momentum",
                "--amount-usd", "5000", "--apply") == 0
    # Tomorrow's paper run values the credited sleeve.
    with Session(engine) as session:
        session.add(EquitySnapshot(
            portfolio="momentum", date=date(2026, 10, 10),
            session_date=date(2026, 10, 9), equity=24_000.0, cash=24_000.0,
            market_value=0.0, created_at=NOW + timedelta(hours=14),
        ))
        session.commit()

    with Session(engine) as session:
        rows = equity_series(
            session, start=date(2026, 10, 7), end=date(2026, 10, 9)
        )
    values = [value for _, value, _ in rows]
    assert values[-1] == 24_000.0
    assert values[2] / values[1] == pytest.approx(1.0)
    assert max_drawdown_pct(rows) == pytest.approx(5.0)


# --------------------------------------------------------------------------
# max_deployable: marks-based, with the remedy and the contributed figure.
# --------------------------------------------------------------------------


def test_the_max_deployable_refusal_names_the_remedy(tmp_path, capsys):
    url, _ = _book(tmp_path)
    assert _cli(url, tmp_path, "--portfolio", "momentum",
                "--amount-usd", "51001") == 1
    out = _refused(capsys, "within max_deployable_usd")
    assert "raise capital.paper.max_deployable_usd first" in out
    assert "--from/--to instead" in out
    # Contributed 49,000 + 51,001 beside the marked 49,000 + 51,001.
    assert "at the marks (contributed $100,001.00)" in out


def test_contributed_capital_is_printed_beside_the_marks(tmp_path, capsys):
    url, _ = _book(tmp_path)
    assert _cli(url, tmp_path, "--portfolio", "momentum",
                "--amount-usd", "100") == 0
    out = capsys.readouterr().out
    # capital column: 20,000 + 15,000 + 14,000 (the drill excluded).
    assert "contributed capital (sum of portfolio_config.capital): " \
        "$49,000.00 -> $49,100.00" in out
    assert "marks vs contributed: +0.00" in out


def test_a_credit_past_the_sleeves_share_is_a_warning(
    tmp_path, capsys, monkeypatch
):
    url, _ = _book(tmp_path)
    # Deployable 100,000 x thematic's 0.2 = 20,000; it is worth 14,000.
    assert _cli(url, tmp_path, "--portfolio", "thematic_momentum",
                "--amount-usd", "7000") == 0
    out = capsys.readouterr().out
    assert "[WARN] within each sleeve's share -- WARNING: thematic_momentum " \
        "would be worth $21,000.00" in out
    assert "above its 20.00% share $20,000.00" in out
