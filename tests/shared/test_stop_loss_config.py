"""Per-sleeve account trailing-stop thresholds (risk.stop_loss_trailing_pct_by_sleeve).

docs/decisions/qv-risk-stop-overlay-2026-11.md: the account-wide 15% stop costs
quality_value Sharpe and worsens its drawdown; the record recommends 30% for
quality_value and licenses no change for any other sleeve. The override map is
how that one sleeve gets its own threshold while every other sleeve keeps 15%.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from shared.config import AppConfig, ExecutionConfig, RiskConfig, load_config
from shared.universe import ACTIVE_SLEEVES

REPO_ROOT = Path(__file__).parents[2]


def test_a_listed_sleeve_gets_its_override_and_the_rest_the_default():
    risk = RiskConfig(stop_loss_trailing_pct_by_sleeve={"quality_value": 30.0})

    assert risk.stop_loss_pct_for("quality_value") == 30.0
    assert risk.stop_loss_pct_for("momentum") == 15.0


def test_an_unattributed_or_synthetic_scope_gets_the_default():
    # Unit paths carry no portfolio; synthetic "_"-prefixed portfolios are not
    # sleeves, so neither can match an override.
    risk = RiskConfig(stop_loss_trailing_pct_by_sleeve={"quality_value": 30.0})

    assert risk.stop_loss_pct_for(None) == 15.0
    assert risk.stop_loss_pct_for("_unattributed") == 15.0


def test_no_overrides_is_the_default():
    assert RiskConfig().stop_loss_trailing_pct_by_sleeve == {}


def test_a_misspelled_sleeve_is_refused_by_name():
    # A typo would otherwise leave quality_value at 15% with nothing to say so.
    with pytest.raises(ValidationError, match="quality-value") as exc:
        RiskConfig(stop_loss_trailing_pct_by_sleeve={"quality-value": 30.0})
    assert "quality_value" in str(exc.value)  # the valid names are listed


@pytest.mark.parametrize("pct", [0.0, 100.0, -5.0, 150.0])
def test_an_override_outside_0_to_100_is_refused(pct):
    with pytest.raises(ValidationError, match="quality_value"):
        RiskConfig(stop_loss_trailing_pct_by_sleeve={"quality_value": pct})


@pytest.mark.parametrize("pct", [0.0, 100.0, -5.0, 150.0])
def test_a_default_outside_0_to_100_is_refused(pct):
    with pytest.raises(ValidationError, match="stop_loss_trailing_pct"):
        RiskConfig(stop_loss_trailing_pct=pct)


def _app(broker_stops: bool, overrides: dict[str, float]) -> AppConfig:
    return AppConfig(
        risk=RiskConfig(stop_loss_trailing_pct_by_sleeve=overrides),
        execution=ExecutionConfig(broker_stops_enabled=broker_stops),
    )


def test_broker_stops_with_a_differing_override_are_refused():
    # BrokerStopManager rests every stop at the account default; with an
    # override it would put a 15% stop at IB under a 30% sleeve.
    with pytest.raises(ValidationError) as exc:
        _app(True, {"quality_value": 30.0})
    message = str(exc.value)
    assert "broker_stops_enabled" in message
    assert "stop_loss_trailing_pct_by_sleeve" in message
    assert "quality_value" in message


def test_broker_stops_load_with_no_override_or_one_equal_to_the_default():
    _app(True, {})
    _app(True, {"quality_value": 15.0})
    _app(False, {"quality_value": 30.0})


def test_the_shipped_config_stops_quality_value_at_30_and_the_rest_at_15():
    risk = load_config(REPO_ROOT / "config" / "default.yaml").risk

    assert risk.stop_loss_pct_for("quality_value") == 30.0
    for sleeve in ACTIVE_SLEEVES:
        if sleeve != "quality_value":
            assert risk.stop_loss_pct_for(sleeve) == 15.0, sleeve
