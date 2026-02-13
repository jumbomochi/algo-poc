import os
import pytest
from shared.config import load_config, AppConfig


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
