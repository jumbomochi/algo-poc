"""Tests for the missed-entry restore (KAN-88).

Statement parsing first, then the operator CLI. The 15 positions this tool
exists to recover are the 2026-09-18 04:15 SGT BUY batch, orders 189-205;
the AMD row below is the first of them.
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone

import pytest

from scripts.ops.restore_missed_entries import (
    REQUIRED_COLUMNS,
    StatementRefusedError,
    load_statement,
    parse_statement,
)


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
