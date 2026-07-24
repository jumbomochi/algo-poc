from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from shared.broker_state import BrokerAccountSnapshot
from shared.capital import CapitalDisabledError, calculate_capital_budget
from shared.config import CapitalConfig, CapitalModeConfig, CurrencyConfig


def make_snapshot(**overrides) -> BrokerAccountSnapshot:
    captured_at = overrides.pop("captured_at", datetime.now(UTC))
    values = {
        "account_id": "DUN551088",
        "mode": "paper",
        "base_currency": "SGD",
        "trading_currency": "USD",
        "net_liquidation_base": 1_000_000.0,
        "fx_base_per_trading": 1.25,
        "net_liquidation_trading_equivalent": 800_000.0,
        "settled_cash_trading": 25_000.0,
        "fx_source": "$LEDGER:ALL/ExchangeRate",
        "fx_captured_at": captured_at,
        "captured_at": captured_at,
    }
    values.update(overrides)
    return BrokerAccountSnapshot(**values)


def test_sgd_nav_builds_usd_sleeve_budgets():
    snapshot = make_snapshot(
        net_liquidation_base=1_001_757.23,
        fx_base_per_trading=1.2928304,
        net_liquidation_trading_equivalent=774_855.87,
        settled_cash_trading=25_000,
    )
    budget = calculate_capital_budget(
        snapshot,
        "paper",
        CapitalConfig(),
        CurrencyConfig(),
        {"momentum": 0.6, "hedge": 0.4},
        now=snapshot.captured_at,
    )

    assert budget.base_currency == "SGD"
    assert budget.trading_currency == "USD"
    assert budget.deployable_capital == pytest.approx(774_855.87)
    assert budget.sleeve_budgets["momentum"] == pytest.approx(464_913.522)
    assert budget.net_liquidation_base == pytest.approx(1_001_757.23)
    assert budget.net_liquidation_trading_equivalent == pytest.approx(774_855.87)
    assert budget.fx_base_per_trading == pytest.approx(1.2928304)
    assert budget.settled_cash_trading == pytest.approx(25_000)


def test_usd_cap_is_applied_after_converting_fractional_sgd_budget():
    cfg = CapitalConfig(
        paper=CapitalModeConfig(
            deployment_fraction=0.5, max_deployable_usd=450_000.0
        )
    )
    budget = calculate_capital_budget(
        make_snapshot(),
        "paper",
        cfg,
        CurrencyConfig(),
        {"x": 1.0},
    )

    assert budget.fractional_base == pytest.approx(500_000.0)
    assert budget.deployable_capital == pytest.approx(400_000.0)


def test_stale_fx_blocks_capital_calculation():
    snapshot = make_snapshot(
        fx_captured_at=datetime.now(UTC) - timedelta(seconds=301)
    )
    with pytest.raises(ValueError, match="FX quote is stale"):
        calculate_capital_budget(
            snapshot,
            "paper",
            CapitalConfig(),
            CurrencyConfig(max_fx_age_seconds=300),
            {"x": 1.0},
        )


def test_future_fx_quote_blocks_capital_calculation():
    now = datetime.now(UTC)
    snapshot = make_snapshot(fx_captured_at=now + timedelta(microseconds=1))

    with pytest.raises(ValueError, match="FX quote age cannot be negative"):
        calculate_capital_budget(
            snapshot,
            "paper",
            CapitalConfig(),
            CurrencyConfig(),
            {"x": 1.0},
            now=now,
        )


@pytest.mark.parametrize(
    ("snapshot", "error"),
    [
        (replace(make_snapshot(), base_currency="EUR"), "base currency"),
        (replace(make_snapshot(), trading_currency="SGD"), "trading currency"),
        (replace(make_snapshot(), net_liquidation_base=0), "NetLiquidation"),
        (
            replace(make_snapshot(), net_liquidation_base=float("nan")),
            "NetLiquidation",
        ),
        (replace(make_snapshot(), fx_base_per_trading=0), "FX rate"),
        (
            replace(make_snapshot(), fx_base_per_trading=float("nan")),
            "FX rate",
        ),
    ],
)
def test_invalid_currency_or_valuation_blocks_capital_calculation(snapshot, error):
    with pytest.raises(ValueError, match=error):
        calculate_capital_budget(
            snapshot,
            "paper",
            CapitalConfig(),
            CurrencyConfig(),
            {"x": 1.0},
            now=snapshot.captured_at,
        )


def test_sleeve_weights_must_sum_to_one():
    snapshot = make_snapshot()

    with pytest.raises(ValueError, match="sleeve weights must sum to 1.0"):
        calculate_capital_budget(
            snapshot,
            "paper",
            CapitalConfig(),
            CurrencyConfig(),
            {"x": 0.9},
            now=snapshot.captured_at,
        )


def test_live_is_disabled_by_default():
    snapshot = make_snapshot(mode="live")

    with pytest.raises(CapitalDisabledError, match="fraction and cap"):
        calculate_capital_budget(
            snapshot,
            "live",
            CapitalConfig(),
            CurrencyConfig(),
            {"x": 1.0},
            now=snapshot.captured_at,
        )


def test_fraction_outside_unit_interval_is_invalid():
    with pytest.raises(ValidationError):
        CapitalModeConfig(deployment_fraction=1.01)
