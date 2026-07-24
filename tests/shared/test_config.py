import os
import pytest
from pydantic import ValidationError

from shared.config import AppConfig, CapitalConfig, CapitalModeConfig, load_config


def test_load_config_returns_app_config(tmp_path):
    yaml_content = """
mode: paper
risk:
  position_entry_limit_pct: 5.0
  sector_concentration_pct: 20.0
  total_exposure_limit_pct: 150.0
  stop_loss_trailing_pct: 15.0
  drawdown_pause_pct: 10.0
  drawdown_circuit_breaker_pct: 20.0
  soft_ceiling_pct: 7.0
  hard_ceiling_pct: 15.0
  margin_warning_pct: 70.0
  margin_critical_pct: 85.0
  double_down_entry_limit_pct: 10.0
  passive_scan_interval_minutes: 30
  min_viable_fill_pct: 40.0
  portfolio_beta_alert_threshold: 1.5
  correlation_alert_threshold: 0.7
  correlation_min_lookback_days: 60
"""
    config_file = tmp_path / "test_config.yaml"
    config_file.write_text(yaml_content)
    config = load_config(str(config_file))
    assert isinstance(config, AppConfig)
    assert config.mode == "paper"
    assert config.risk.position_entry_limit_pct == 5.0
    assert config.risk.hard_ceiling_pct == 15.0


def test_load_config_env_override(tmp_path, monkeypatch):
    yaml_content = "mode: paper\n"
    config_file = tmp_path / "test_config.yaml"
    config_file.write_text(yaml_content)
    monkeypatch.setenv("ALGO_MODE", "live")
    config = load_config(str(config_file))
    assert config.mode == "live"


def test_load_config_nested_env_override(tmp_path, monkeypatch):
    yaml_content = "mode: paper\ndatabase:\n  url: postgresql://old:old@localhost/old\n"
    config_file = tmp_path / "test_config.yaml"
    config_file.write_text(yaml_content)
    monkeypatch.setenv("ALGO_DATABASE_URL", "postgresql://new:new@localhost/new")
    config = load_config(str(config_file))
    assert config.database.url == "postgresql://new:new@localhost/new"


def test_load_config_missing_file():
    with pytest.raises(FileNotFoundError):
        load_config("/nonexistent/path.yaml")


def test_app_config_has_safe_mode_specific_capital_defaults():
    config = AppConfig()

    assert config.capital == CapitalConfig(
        paper=CapitalModeConfig(
            deployment_fraction=1.0,
            max_deployable_usd=None,
            entries_enabled=False,
        ),
        live=CapitalModeConfig(
            deployment_fraction=0.0,
            max_deployable_usd=0.0,
            entries_enabled=False,
        ),
    )


def test_default_yaml_declares_mode_specific_capital_values():
    config = load_config("config/default.yaml")

    assert config.capital.paper.deployment_fraction == 1.0
    assert config.capital.paper.max_deployable_usd is None
    assert config.capital.paper.entries_enabled is False
    assert config.capital.live.deployment_fraction == 0.0
    assert config.capital.live.max_deployable_usd == 0.0
    assert config.capital.live.entries_enabled is False


def test_currency_defaults_are_sgd_base_and_usd_trading():
    cfg = AppConfig()
    assert cfg.currency.expected_base_currency == "SGD"
    assert cfg.currency.trading_currency == "USD"
    assert cfg.currency.max_fx_age_seconds == 300
    assert cfg.currency.minimum_settled_usd_reserve == 0.0


def test_live_entries_require_positive_usd_reserve():
    with pytest.raises(ValidationError, match="settled USD reserve"):
        AppConfig(
            mode="live",
            capital=CapitalConfig(
                live=CapitalModeConfig(
                    deployment_fraction=0.1,
                    max_deployable_usd=10_000,
                    entries_enabled=True,
                )
            ),
        )
