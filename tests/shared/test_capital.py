import pytest
from pydantic import ValidationError

from shared.capital import CapitalDisabledError, calculate_capital_budget
from shared.config import CapitalConfig, CapitalModeConfig


def test_paper_defaults_to_full_nav_without_cap():
    cfg = CapitalConfig()
    budget = calculate_capital_budget(
        1_000_000.0, "paper", cfg, {"momentum": 0.6, "hedge": 0.4}
    )
    assert budget.deployable_capital == 1_000_000.0
    assert budget.sleeve_budgets == {"momentum": 600_000.0, "hedge": 400_000.0}


def test_cap_limits_fractional_budget():
    cfg = CapitalConfig(
        paper=CapitalModeConfig(
            deployment_fraction=0.5, max_deployable_usd=200_000.0
        )
    )
    assert (
        calculate_capital_budget(1_000_000.0, "paper", cfg, {"x": 1.0}).deployable_capital
        == 200_000.0
    )


def test_live_is_disabled_by_default():
    with pytest.raises(CapitalDisabledError, match="fraction and cap"):
        calculate_capital_budget(1_000_000.0, "live", CapitalConfig(), {"x": 1.0})


def test_fraction_outside_unit_interval_is_invalid():
    with pytest.raises(ValidationError):
        CapitalModeConfig(deployment_fraction=1.01)
