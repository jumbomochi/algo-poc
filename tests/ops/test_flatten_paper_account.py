from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from scripts.ops.flatten_paper_account import (
    FlattenOrder,
    FlattenRefusedError,
    execute_flatten,
    plan_flatten,
)


def _pos(account, symbol, con_id, qty):
    return SimpleNamespace(
        account=account,
        contract=SimpleNamespace(conId=con_id, symbol=symbol, localSymbol=symbol),
        position=qty,
        avgCost=100.0,
    )


def test_plan_flatten_builds_sell_orders_for_longs():
    positions = [
        _pos("DUN551088", "CSCO", 268084, 17),
        _pos("DUN551088", "SH", 738523410, 19),
    ]
    plan = plan_flatten(positions, account_id="DUN551088")
    assert plan == [
        FlattenOrder(con_id=268084, symbol="CSCO", action="SELL", quantity=17.0),
        FlattenOrder(con_id=738523410, symbol="SH", action="SELL", quantity=19.0),
    ]


def test_plan_flatten_refuses_non_paper_account():
    positions = [_pos("U1234567", "CSCO", 268084, 17)]
    with pytest.raises(FlattenRefusedError, match="non-paper"):
        plan_flatten(positions, account_id="U1234567")


def test_plan_flatten_covers_shorts_with_buy():
    positions = [_pos("DUN551088", "TLT", 15547, -8)]
    plan = plan_flatten(positions, account_id="DUN551088")
    assert plan == [FlattenOrder(con_id=15547, symbol="TLT", action="BUY", quantity=8.0)]


def test_plan_flatten_skips_zero_quantity():
    positions = [_pos("DUN551088", "GLD", 111, 0)]
    assert plan_flatten(positions, account_id="DUN551088") == []


def test_execute_flatten_refuses_wrong_confirmation_and_places_nothing():
    ib = MagicMock()
    plan = [FlattenOrder(con_id=268084, symbol="CSCO", action="SELL", quantity=17.0)]
    with pytest.raises(FlattenRefusedError, match="confirmation"):
        execute_flatten(ib, plan, confirm="99")
    ib.placeOrder.assert_not_called()


def test_execute_flatten_places_one_market_order_per_plan_item():
    ib = MagicMock()
    plan = [
        FlattenOrder(con_id=268084, symbol="CSCO", action="SELL", quantity=17.0),
        FlattenOrder(con_id=738523410, symbol="SH", action="SELL", quantity=19.0),
    ]
    execute_flatten(ib, plan, confirm="2")
    assert ib.placeOrder.call_count == 2
    actions = [call.args[1].action for call in ib.placeOrder.call_args_list]
    assert actions == ["SELL", "SELL"]
