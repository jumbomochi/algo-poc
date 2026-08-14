from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from scripts.ops.broker_stop_spike import (
    ORDERREF_PREFIX,
    SpikeRefusedError,
    describe_order,
    ips_stop_price,
    missing_from_broker_open_order,
    order_ref_for,
    place_stop,
    plan_stop,
    select_spike_orders,
    stop_coverage,
)

ACCOUNT = "DUN551088"


def _pos(symbol, con_id, qty, *, account=ACCOUNT, avg_cost=100.0):
    return SimpleNamespace(
        account=account,
        contract=SimpleNamespace(conId=con_id, symbol=symbol, localSymbol=symbol),
        position=qty,
        avgCost=avg_cost,
    )


def _trade(**overrides):
    order = SimpleNamespace(
        orderId=42,
        permId=987654,
        clientId=118,
        account=ACCOUNT,
        orderRef=f"{ORDERREF_PREFIX}-CSCO",
        action="SELL",
        orderType="STP",
        totalQuantity=17.0,
        auxPrice=55.25,
        lmtPrice=0.0,
        tif="GTC",
        goodTillDate="",
        triggerMethod=0,
        outsideRth=False,
        transmit=True,
        parentId=0,
        ocaGroup="",
        trailStopPrice=0.0,
        trailingPercent=None,
    )
    for key, value in overrides.pop("order", {}).items():
        setattr(order, key, value)
    status = SimpleNamespace(
        status="PreSubmitted",
        filled=0.0,
        remaining=17.0,
        avgFillPrice=0.0,
        lastFillPrice=0.0,
        whyHeld="",
    )
    for key, value in overrides.pop("status", {}).items():
        setattr(status, key, value)
    contract = SimpleNamespace(
        conId=268084, symbol="CSCO", localSymbol="CSCO", secType="STK"
    )
    return SimpleNamespace(order=order, orderStatus=status, contract=contract)


# --- IPS stop level (question 5 sizing hook) ---------------------------------


def test_ips_stop_price_applies_the_trailing_rule_and_rounds_down():
    # 100 * 0.85 = 85.00; 55.55 * 0.85 = 47.2175 -> 47.21, never 47.22.
    assert ips_stop_price(100.0, 15.0) == 85.00
    assert ips_stop_price(55.55, 15.0) == 47.21


def test_ips_stop_price_refuses_nonsense_inputs():
    with pytest.raises(SpikeRefusedError, match="last price"):
        ips_stop_price(0.0, 15.0)
    with pytest.raises(SpikeRefusedError, match="trailing pct"):
        ips_stop_price(100.0, 0.0)
    with pytest.raises(SpikeRefusedError, match="trailing pct"):
        ips_stop_price(100.0, 100.0)


# --- whole-share interaction (question 5) -----------------------------------


def test_stop_coverage_leaves_no_gap_for_whole_shares():
    assert stop_coverage(17.0, allow_fractional=False) == (17.0, 0.0)


def test_stop_coverage_reports_the_uncovered_fraction():
    # _effective_quantity truncates; the remainder has no broker protection.
    assert stop_coverage(17.4, allow_fractional=False) == (17.0, 0.4)


def test_stop_coverage_keeps_the_fraction_when_the_account_supports_it():
    assert stop_coverage(17.4, allow_fractional=True) == (17.4, 0.0)


def test_stop_coverage_refuses_a_holding_that_truncates_to_zero():
    with pytest.raises(SpikeRefusedError, match="truncates to zero"):
        stop_coverage(0.4, allow_fractional=False)


def test_stop_coverage_refuses_a_non_positive_holding():
    with pytest.raises(SpikeRefusedError, match="must be positive"):
        stop_coverage(0.0, allow_fractional=False)


# --- planning guards --------------------------------------------------------


def test_plan_stop_builds_a_gtc_sell_stop_at_the_ips_level():
    plan = plan_stop(
        [_pos("CSCO", 268084, 17)],
        account_id=ACCOUNT,
        symbol="csco",
        last_price=65.00,
        trailing_pct=15.0,
    )
    assert plan.con_id == 268084
    assert plan.symbol == "CSCO"
    assert plan.action == "SELL"
    assert plan.quantity == 17.0
    assert plan.stop_price == 55.25
    assert plan.order_ref == f"{ORDERREF_PREFIX}-CSCO"
    assert plan.uncovered_quantity == 0.0
    assert plan.triggers_immediately is False


def test_plan_stop_refuses_a_live_account():
    with pytest.raises(SpikeRefusedError, match="non-paper"):
        plan_stop(
            [_pos("CSCO", 268084, 17, account="U1234567")],
            account_id="U1234567",
            symbol="CSCO",
            last_price=65.0,
            trailing_pct=15.0,
        )


def test_plan_stop_refuses_a_position_from_another_account():
    with pytest.raises(SpikeRefusedError, match="unexpected account"):
        plan_stop(
            [_pos("CSCO", 268084, 17, account="DU999")],
            account_id=ACCOUNT,
            symbol="CSCO",
            last_price=65.0,
            trailing_pct=15.0,
        )


def test_plan_stop_refuses_a_symbol_that_is_not_held():
    with pytest.raises(SpikeRefusedError, match="no open position"):
        plan_stop(
            [_pos("CSCO", 268084, 17)],
            account_id=ACCOUNT,
            symbol="TLT",
            last_price=90.0,
            trailing_pct=15.0,
        )


def test_plan_stop_refuses_a_short_position():
    with pytest.raises(SpikeRefusedError, match="short"):
        plan_stop(
            [_pos("TLT", 15547, -8)],
            account_id=ACCOUNT,
            symbol="TLT",
            last_price=90.0,
            trailing_pct=15.0,
        )


def test_plan_stop_refuses_a_quantity_larger_than_the_holding():
    with pytest.raises(SpikeRefusedError, match="exceeds"):
        plan_stop(
            [_pos("CSCO", 268084, 17)],
            account_id=ACCOUNT,
            symbol="CSCO",
            last_price=65.0,
            trailing_pct=15.0,
            quantity=18,
        )


def test_plan_stop_reports_the_residue_of_a_partial_quantity():
    plan = plan_stop(
        [_pos("CSCO", 268084, 17)],
        account_id=ACCOUNT,
        symbol="CSCO",
        last_price=65.0,
        trailing_pct=15.0,
        quantity=1,
    )
    assert plan.quantity == 1.0
    assert plan.uncovered_quantity == 16.0


def test_plan_stop_refuses_a_stop_that_would_trigger_on_arrival():
    with pytest.raises(SpikeRefusedError, match="trigger it on arrival"):
        plan_stop(
            [_pos("CSCO", 268084, 17)],
            account_id=ACCOUNT,
            symbol="CSCO",
            last_price=65.0,
            trailing_pct=15.0,
            stop_price=65.50,
        )


def test_plan_stop_allows_an_immediate_trigger_when_asked_explicitly():
    plan = plan_stop(
        [_pos("CSCO", 268084, 17)],
        account_id=ACCOUNT,
        symbol="CSCO",
        last_price=65.0,
        trailing_pct=15.0,
        quantity=1,
        stop_price=65.50,
        allow_trigger=True,
    )
    assert plan.triggers_immediately is True
    assert plan.quantity == 1.0


def test_plan_stop_flags_the_uncovered_fraction_of_a_fractional_holding():
    plan = plan_stop(
        [_pos("CSCO", 268084, 17.4)],
        account_id=ACCOUNT,
        symbol="CSCO",
        last_price=65.0,
        trailing_pct=15.0,
    )
    assert plan.quantity == 17.0
    assert plan.held_quantity == 17.4
    assert plan.uncovered_quantity == 0.4


# --- placement guard --------------------------------------------------------


def test_place_stop_refuses_a_wrong_confirmation_and_places_nothing():
    ib = MagicMock()
    plan = plan_stop(
        [_pos("CSCO", 268084, 17)],
        account_id=ACCOUNT,
        symbol="CSCO",
        last_price=65.0,
        trailing_pct=15.0,
    )
    with pytest.raises(SpikeRefusedError, match="confirmation"):
        place_stop(ib, plan, confirm="yes")
    ib.placeOrder.assert_not_called()


def test_place_stop_sends_a_gtc_stp_stamped_with_the_spike_ref():
    ib = MagicMock()
    plan = plan_stop(
        [_pos("CSCO", 268084, 17)],
        account_id=ACCOUNT,
        symbol="CSCO",
        last_price=65.0,
        trailing_pct=15.0,
    )
    place_stop(ib, plan, confirm="CSCO")
    contract, order = ib.placeOrder.call_args.args
    assert contract.conId == 268084
    assert order.orderType == "STP"
    assert order.action == "SELL"
    assert order.totalQuantity == 17.0
    assert order.auxPrice == 55.25
    assert order.tif == "GTC"
    assert order.orderRef == f"{ORDERREF_PREFIX}-CSCO"


def test_order_ref_is_stable_and_upper_cased():
    assert order_ref_for("csco") == order_ref_for("CSCO") == f"{ORDERREF_PREFIX}-CSCO"


# --- observation (question 3) -----------------------------------------------


def test_describe_order_captures_the_stop_specific_fields():
    record = describe_order(_trade())
    assert record["symbol"] == "CSCO"
    assert record["orderType"] == "STP"
    assert record["auxPrice"] == 55.25
    assert record["tif"] == "GTC"
    assert record["triggerMethod"] == 0
    assert record["outsideRth"] is False
    assert record["status"] == "PreSubmitted"


def test_describe_order_reports_absent_fields_rather_than_hiding_them():
    trade = _trade()
    del trade.order.auxPrice
    record = describe_order(trade)
    assert "auxPrice" in record
    assert record["auxPrice"] is None


def test_missing_from_broker_open_order_names_the_reader_gap():
    missing = missing_from_broker_open_order(describe_order(_trade()))
    assert "orderType" in missing
    assert "auxPrice" in missing
    assert "tif" in missing
    # Fields BrokerOpenOrder already carries are not gaps.
    assert "action" not in missing
    assert "totalQuantity" not in missing


def test_missing_from_broker_open_order_ignores_fields_ib_left_empty():
    trade = _trade()
    trade.order.ocaGroup = ""
    trade.order.trailStopPrice = 0.0
    missing = missing_from_broker_open_order(describe_order(trade))
    assert "ocaGroup" not in missing
    assert "trailStopPrice" not in missing


# --- cleanup selection ------------------------------------------------------


def test_select_spike_orders_matches_only_the_spike_stamp():
    spike = _trade()
    foreign = _trade()
    foreign.order.orderRef = "sleeve-2026-08-14-thematic_momentum-CSCO-buy"
    unstamped = _trade()
    unstamped.order.orderRef = ""
    assert select_spike_orders([spike, foreign, unstamped]) == [spike]


def test_select_spike_orders_tolerates_a_missing_order_ref():
    trade = _trade()
    del trade.order.orderRef
    assert select_spike_orders([trade]) == []
