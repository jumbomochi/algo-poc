"""Tests for the missed-entry restore (KAN-88).

Statement parsing first, then the operator CLI. The 15 positions this tool
exists to recover are the 2026-09-18 04:15 SGT BUY batch, orders 189-205;
the AMD row below is the first of them.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone

import pytest

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from scripts.ops.restore_missed_entries import (
    CONFIRMATION,
    REQUIRED_COLUMNS,
    RecoveryRefusedError,
    StatementRefusedError,
    apply_recovery,
    load_statement,
    main,
    parse_statement,
)
from services.execution.execution_sweep import (
    RECOVERY_SOURCE_STATEMENT,
    plan_sweep,
)
from shared.models import (
    Base,
    ExecutionFill,
    OrderIntent,
    OrderStatus,
    PortfolioConfig,
    Position,
    Trade,
)
from shared.order_ledger import ABSENT_AT_IB_REASON, OrderLedger

NOW = datetime(2026, 9, 18, 13, 31, 2, tzinfo=timezone.utc)


def _row(**overrides) -> dict[str, str]:
    """One AMD row from the 2026-09-18 batch, in IB Flex "Trades" shape.

    ``DateTime`` is UTC: 13:31:02Z is 09:31:02 EDT, a minute after the open.
    """
    row = {
        "ClientAccountID": "DUN551088",
        "TradeID": "0000e0d5.68cb1234.01.01",
        "IBOrderID": "189",
        "ConID": "4391",
        "Symbol": "AMD",
        "Exchange": "NASDAQ",
        "CurrencyPrimary": "USD",
        "Buy/Sell": "BUY",
        "Quantity": "5",
        "TradePrice": "161.42",
        "IBCommission": "-1.00",
        "IBCommissionCurrency": "USD",
        "DateTime": "2026-09-18 13:31:02",
    }
    row.update(overrides)
    return row


def _write_csv(path, rows):
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=sorted(REQUIRED_COLUMNS))
        writer.writeheader()
        writer.writerows(rows)
    return path


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def test_a_statement_row_becomes_an_execution():
    """AC2. Every economic value comes from the file; none is defaulted."""
    [execution] = parse_statement([_row()])

    assert execution.execution_id == "0000e0d5.68cb1234.01.01"
    assert execution.account_id == "DUN551088"
    assert execution.ib_order_id == "189"
    assert execution.con_id == 4391
    assert execution.ticker == "AMD"
    assert execution.exchange == "NASDAQ"
    assert execution.currency == "USD"
    assert execution.side == "buy"
    assert execution.quantity == 5.0
    assert execution.cumulative_quantity == 5.0
    assert execution.price == 161.42
    assert execution.commission == 1.0
    assert execution.commission_currency == "USD"
    assert execution.executed_at == datetime(
        2026, 9, 18, 13, 31, 2, tzinfo=timezone.utc
    )


def test_a_missing_column_is_refused_by_name():
    """A statement exported with the wrong field set must not be half-read.
    The operator has to go back and re-export it, not fill it in by hand."""
    row = _row()
    del row["TradePrice"]

    with pytest.raises(StatementRefusedError, match="TradePrice"):
        parse_statement([row])


def test_a_commission_is_recorded_as_a_magnitude():
    """IB reports a commission CHARGE as negative. FillProjector._validate
    rejects a negative commission outright, so a verbatim copy would refuse
    every real row in the statement."""
    [execution] = parse_statement([_row(IBCommission="-1.00")])

    assert execution.commission == 1.0


def test_an_unreadable_price_is_refused_rather_than_defaulted():
    """Never invent a price: a wrong one is a wrong cost basis forever, and
    a zero would book a plausible-looking free position."""
    with pytest.raises(StatementRefusedError, match="TradePrice"):
        parse_statement([_row(TradePrice="")])


def test_a_zero_price_is_refused():
    with pytest.raises(StatementRefusedError, match="TradePrice"):
        parse_statement([_row(TradePrice="0")])


def test_a_zero_quantity_is_refused():
    """LLY, V and SCHW rounded to zero shares and were correctly refused at
    placement time; a zero-quantity row here means the export is wrong."""
    with pytest.raises(StatementRefusedError, match="Quantity"):
        parse_statement([_row(Quantity="0")])


def test_a_live_account_is_refused():
    """A live ledger is never reconstructed by a script."""
    with pytest.raises(StatementRefusedError, match="paper"):
        parse_statement([_row(ClientAccountID="U1234567")])


def test_a_row_for_another_account_is_refused():
    """A statement covering two accounts must not leak one into the other."""
    with pytest.raises(StatementRefusedError, match="DUN551088"):
        parse_statement([_row(ClientAccountID="DU9999999")], account_id="DUN551088")


def test_an_unknown_side_is_refused():
    with pytest.raises(StatementRefusedError, match="Buy/Sell"):
        parse_statement([_row(**{"Buy/Sell": "SHORT"})])


def test_partial_executions_of_one_order_accumulate():
    """cumulative_quantity is what the projector advances the intent on. A
    per-row copy of quantity would terminalize a 2-of-5 fill as complete and
    release the rest of the reservation."""
    rows = [
        _row(TradeID="a", Quantity="2", **{"DateTime": "2026-09-18 13:31:02"}),
        _row(TradeID="b", Quantity="3", **{"DateTime": "2026-09-18 13:34:00"}),
    ]

    first, second = parse_statement(rows)

    assert (first.quantity, first.cumulative_quantity) == (2.0, 2.0)
    assert (second.quantity, second.cumulative_quantity) == (3.0, 5.0)


def test_executions_of_one_order_accumulate_in_time_order():
    """Rows arrive in whatever order the export produced. Accumulating in
    file order would give the later fill the smaller running total."""
    rows = [
        _row(TradeID="b", Quantity="3", **{"DateTime": "2026-09-18 13:34:00"}),
        _row(TradeID="a", Quantity="2", **{"DateTime": "2026-09-18 13:31:02"}),
    ]

    first, second = parse_statement(rows)

    assert (first.execution_id, first.cumulative_quantity) == ("a", 2.0)
    assert (second.execution_id, second.cumulative_quantity) == ("b", 5.0)


def test_separate_orders_do_not_share_a_running_total():
    rows = [_row(TradeID="a"), _row(TradeID="b", IBOrderID="190", Symbol="NVDA")]

    first, second = parse_statement(rows)

    assert first.cumulative_quantity == 5.0
    assert second.cumulative_quantity == 5.0


def test_both_ib_datetime_formats_are_read():
    """Flex serves 'YYYYMMDD;HHMMSS'; an Activity Statement CSV serves ISO."""
    [flex] = parse_statement([_row(**{"DateTime": "20260918;133102"})])
    [iso] = parse_statement([_row(**{"DateTime": "2026-09-18 13:31:02"})])

    assert flex.executed_at == iso.executed_at


def test_an_unparseable_datetime_is_refused():
    """A silent misread would file the fill against the wrong session."""
    with pytest.raises(StatementRefusedError, match="DateTime"):
        parse_statement([_row(**{"DateTime": "last Tuesday"})])


def test_a_duplicate_trade_id_is_refused():
    """execution_id is the idempotency key. Two rows sharing one would make
    the second look already-recorded and be dropped in silence."""
    with pytest.raises(StatementRefusedError, match="TradeID"):
        parse_statement([_row(), _row()])


def test_an_empty_statement_is_refused():
    """A dry run reporting "0 to recover" against an empty export looks
    exactly like a book that needs nothing."""
    with pytest.raises(StatementRefusedError, match="no rows"):
        parse_statement([])


def test_a_statement_file_is_read_from_disk(tmp_path):
    path = _write_csv(tmp_path / "trades.csv", [_row()])

    [execution] = load_statement(path)

    assert execution.ticker == "AMD"


# --------------------------------------------------------------------------
# Applying
# --------------------------------------------------------------------------


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db_session:
        db_session.add(PortfolioConfig(
            portfolio="momentum", capital=30_000, cash=30_000,
            created_at=NOW, updated_at=NOW,
        ))
        db_session.commit()
        yield db_session


def _expired_intent(
    session,
    *,
    recommendation_id: str = "rec-amd",
    ib_order_id: str = "189",
    ticker: str = "AMD",
    con_id: int = 4391,
    requested: float = 5.0809,
    reason: str = ABSENT_AT_IB_REASON,
) -> OrderIntent:
    """An intent as the 2026-09-19 restart left it.

    ``restore_broker_tracking -> restore_order_by_ref`` terminalized all 17
    intents for orders 189-205 EXPIRED with ABSENT_AT_IB_REASON. For the 15
    that filled, that status is factually wrong.
    """
    intent = OrderIntent(
        recommendation_id=recommendation_id,
        account_id="DUN551088",
        mode="paper",
        portfolio="momentum",
        con_id=con_id,
        symbol=ticker,
        exchange="NASDAQ",
        currency="USD",
        action="BUY",
        requested_quantity=requested,
        limit_price=162.0,
        order_type="LMT",
        reserved_notional=requested * 162.0,
        filled_quantity=0,
        status=OrderStatus.EXPIRED.value,
        reason=reason,
        ib_order_id=ib_order_id,
        created_at=NOW,
        updated_at=NOW,
        submitted_at=NOW,
        terminal_at=NOW,
    )
    session.add(intent)
    session.commit()
    return intent


def _plan(session, rows=None):
    return plan_sweep(
        parse_statement(rows if rows is not None else [_row()]),
        OrderLedger(session),
        recovery_source=RECOVERY_SOURCE_STATEMENT,
    )


def test_a_statement_fill_opens_the_position(session):
    """AC1/AC2/AC3. The position, the trade at the statement price and the
    cash movement all come out of the ordinary projector path -- the same
    one a live fill takes, which is why the accounting is trustworthy."""
    _expired_intent(session)

    apply_recovery(session, _plan(session), confirm=CONFIRMATION)

    position = session.scalar(
        select(Position).where(Position.con_id == 4391)
    )
    assert position is not None
    assert position.quantity == 5.0
    assert position.avg_entry_price == 161.42

    # AC2. `trades` rows are ROUND TRIPS in this schema -- paper_state.py:277
    # writes one only on the sell, carrying entry_price off the position. So
    # an entry's real fill price lives on the position and on the immutable
    # execution_fills audit row, and reaches `trades` when the position is
    # closed. Writing a buy-side Trade row here would be a shape nothing else
    # in the book produces, and would double-count on the eventual sell.
    fill = session.scalar(select(ExecutionFill))
    assert fill.price == 161.42
    assert fill.commission == 1.0
    assert session.scalar(select(func.count()).select_from(Trade)) == 0

    # 30,000 - (5 x 161.42) - 1.00 commission
    config = session.scalar(
        select(PortfolioConfig).where(PortfolioConfig.portfolio == "momentum")
    )
    assert config.cash == pytest.approx(30_000 - 807.10 - 1.0)


def test_a_wrongly_expired_intent_is_restored_then_filled(session):
    """AC4. _advance_intent returns early on a terminal status, so the
    projector alone would leave these EXPIRED forever even once the fill is
    recorded. 5.0809 requested against 5 filled is whole-share rounding --
    a sub-one-share shortfall -- so FILLED is the correct end state."""
    _expired_intent(session)

    apply_recovery(session, _plan(session), confirm=CONFIRMATION)

    intent = session.scalar(select(OrderIntent))
    assert intent.status == OrderStatus.FILLED.value
    assert intent.reason is None
    assert intent.filled_quantity == 5.0


def test_the_intent_correction_survives_into_the_projection(session):
    """restore_absent_terminalization only FLUSHES, and FillProjector.apply
    opens with _end_read_only_autobegin, which rolls back a clean
    in-transaction session. Without a commit in between, the AC4 correction
    is silently undone and the fill lands against an EXPIRED intent -- which
    the projector accepts, leaving the intent terminal-and-wrong forever.
    apply_recovery owns that commit so no caller can forget it."""
    _expired_intent(session)

    outcome = _plan(session)          # deliberately NO commit here
    apply_recovery(session, outcome, confirm=CONFIRMATION)

    assert session.scalar(select(OrderIntent)).status == OrderStatus.FILLED.value


def test_an_intent_expired_for_a_real_reason_is_left_alone(session):
    """restore_absent_terminalization refuses any reason but absence. A
    genuinely expired order stays expired, and the execution is reported
    untracked rather than forced through."""
    _expired_intent(session, reason="cancelled by risk")

    outcome = _plan(session)

    assert outcome.recovered == ()
    assert outcome.untracked == ("189",)
    assert session.scalar(select(OrderIntent)).status == OrderStatus.EXPIRED.value


def test_an_execution_with_no_intent_is_never_projected(session):
    """No order_intents row means the book cannot attribute the fill to a
    sleeve, and the projector would reject it anyway."""
    outcome = _plan(session)

    assert outcome.recovered == ()
    assert outcome.untracked == ("189",)


def test_re_running_the_repair_changes_nothing(session):
    """AC7. Idempotent: the second run recognises the execution and skips."""
    _expired_intent(session)
    apply_recovery(session, _plan(session), confirm=CONFIRMATION)

    second = _plan(session)
    assert second.recovered == ()
    assert second.already_recorded == 1
    assert apply_recovery(session, second, confirm=CONFIRMATION) == []

    assert session.scalar(
        select(func.count()).select_from(Position)
    ) == 1
    assert session.scalar(select(func.count()).select_from(ExecutionFill)) == 1
    assert session.scalar(select(Position)).quantity == 5.0


def test_every_recovered_fill_is_marked(session):
    """AC6. Evidence rebuilt from a statement weeks later must never be
    indistinguishable from evidence observed on the day."""
    _expired_intent(session)

    apply_recovery(session, _plan(session), confirm=CONFIRMATION)

    fill = session.scalar(select(ExecutionFill))
    assert fill.recovery_source == "ib_statement"
    assert fill.recovered_at is not None
    assert fill.projection_applied is True


def test_a_wrong_confirmation_writes_nothing(session):
    _expired_intent(session)
    outcome = _plan(session)

    with pytest.raises(RecoveryRefusedError, match="RESTORE MISSED ENTRIES"):
        apply_recovery(session, outcome, confirm="yes")

    assert session.scalar(
        select(func.count()).select_from(ExecutionFill)
    ) == 0
    assert session.scalar(select(func.count()).select_from(Position)) == 0


def test_a_dry_run_writes_nothing(tmp_path, capsys):
    """The default is report-only; --apply is the only way to touch a book."""
    url = f"sqlite:///{tmp_path / 'book.db'}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with Session(engine) as db_session:
        _expired_intent(db_session)

    statement = _write_csv(tmp_path / "trades.csv", [_row()])
    assert main(["--statement", str(statement), "--database-url", url]) == 0

    out = capsys.readouterr().out
    assert "Dry-run" in out
    assert "AMD" in out
    with Session(engine) as db_session:
        assert db_session.scalar(
            select(func.count()).select_from(ExecutionFill)
        ) == 0
        # plan_sweep un-expires intents as it plans (AC4). On a dry run that
        # correction must not survive: a report-only command that quietly
        # moves 15 intents out of a terminal state is a write.
        assert db_session.scalar(
            select(OrderIntent)
        ).status == OrderStatus.EXPIRED.value


def test_a_dry_run_names_what_it_cannot_recover(tmp_path, capsys):
    """An untracked execution must be visible BEFORE --apply: a partial
    repair leaves the book half-right and reconciliation still fail-closed."""
    url = f"sqlite:///{tmp_path / 'book.db'}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)

    statement = _write_csv(tmp_path / "trades.csv", [_row()])
    assert main(["--statement", str(statement), "--database-url", url]) == 1

    assert "untracked" in capsys.readouterr().out


def test_the_rollback_dump_records_the_state_before_the_repair(tmp_path, monkeypatch):
    """The dump is the ROLLBACK path, so it must show the book as it stood
    BEFORE anything was corrected. Planning un-expires intents in the
    session, and a dump taken through that session would autoflush and
    record them SUBMITTED -- restoring from it would leave 15 non-terminal
    intents holding reservations nothing will ever release."""
    url = f"sqlite:///{tmp_path / 'book.db'}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with Session(engine) as db_session:
        db_session.add(PortfolioConfig(
            portfolio="momentum", capital=30_000, cash=30_000,
            created_at=NOW, updated_at=NOW,
        ))
        db_session.commit()
        _expired_intent(db_session)

    statement = _write_csv(tmp_path / "trades.csv", [_row()])
    monkeypatch.setattr("sys.stdin", io.StringIO(CONFIRMATION + "\n"))
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda *a: CONFIRMATION)

    assert main([
        "--statement", str(statement), "--database-url", url,
        "--artifact-dir", str(tmp_path / "repairs"), "--apply",
    ]) == 0

    [dump] = list((tmp_path / "repairs").glob("paper_state_pre_entry_restore_*.json"))
    payload = json.loads(dump.read_text())
    [intent] = payload["order_intents"]
    assert intent["status"] == "EXPIRED"
    assert intent["reason"] == ABSENT_AT_IB_REASON

    # And the repair itself still landed.
    with Session(engine) as db_session:
        assert db_session.scalar(select(OrderIntent)).status == "FILLED"


def test_the_applied_artifact_dates_and_explains_the_equity_gap(tmp_path, monkeypatch):
    """AC3's second branch. The ledger is corrected; equity_snapshots is a
    RECORDED series and is not rewritten, so the artifact is where that
    discontinuity is dated and explained."""
    url = f"sqlite:///{tmp_path / 'book.db'}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with Session(engine) as db_session:
        db_session.add(PortfolioConfig(
            portfolio="momentum", capital=30_000, cash=30_000,
            created_at=NOW, updated_at=NOW,
        ))
        db_session.commit()
        _expired_intent(db_session)

    statement = _write_csv(tmp_path / "trades.csv", [_row()])
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda *a: CONFIRMATION)

    main([
        "--statement", str(statement), "--database-url", url,
        "--artifact-dir", str(tmp_path / "repairs"), "--apply",
    ])

    [artifact] = list((tmp_path / "repairs").glob("entry_restore_*.json"))
    payload = json.loads(artifact.read_text())
    assert payload["recovery_source"] == "ib_statement"
    assert payload["execution_dates"] == ["2026-09-18"]
    assert [a["execution_id"] for a in payload["applied"]] == [
        "0000e0d5.68cb1234.01.01"
    ]
    assert payload["applied"][0]["price"] == 161.42
    assert "equity_snapshots" in payload["equity_series_note"]
    assert payload["statement_sha256"]
