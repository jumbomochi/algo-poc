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


def test_load_config_nested_env_override_redis_url_preserves_auth(tmp_path, monkeypatch):
    """ALGO_REDIS_URL must round-trip an auth-bearing URL untouched.

    T3 message-bus lockdown: docker-compose.yml now injects
    redis://:${REDIS_PASSWORD}@redis:6379/0 via this env var. Nothing in the
    config layer should strip, re-encode, or otherwise mangle the embedded
    password.
    """
    yaml_content = "mode: paper\nredis:\n  url: redis://localhost:6379/0\n"
    config_file = tmp_path / "test_config.yaml"
    config_file.write_text(yaml_content)
    monkeypatch.setenv(
        "ALGO_REDIS_URL", "redis://:s3cr3t-p@ss@redis:6379/0"
    )
    config = load_config(str(config_file))
    assert config.redis.url == "redis://:s3cr3t-p@ss@redis:6379/0"


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
    assert config.capital.paper.max_deployable_usd == 100000
    assert config.capital.paper.entries_enabled is True
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


def test_paper_entries_enabled_and_guarded_by_reconciliation():
    from services.execution.reconciliation import ReconciliationResult

    cfg = load_config("config/default.yaml")
    assert cfg.capital.paper.entries_enabled is True

    # A major reconciliation must still fail-close entries regardless of config.
    major = ReconciliationResult(
        matched=[],
        discrepancies=[{"type": "missing_in_db", "auto_correct": False}],
        severity="major",
        account_id="DUN551088",
    )
    assert major.entries_allowed is False


def test_ib_account_id_defaults_to_none(tmp_path):
    """Unset means "no pin" — behaviour is unchanged for anyone who has not
    configured an account id."""
    path = tmp_path / "config.yaml"
    path.write_text("mode: paper\n")

    config = load_config(str(path))

    assert config.ib.account_id is None


def test_ib_account_id_from_yaml_and_env_override(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("mode: paper\nib:\n  account_id: DUN551088\n")

    assert load_config(str(path)).ib.account_id == "DUN551088"

    monkeypatch.setenv("ALGO_IB_ACCOUNT_ID", "DU9999999")
    assert load_config(str(path)).ib.account_id == "DU9999999"


def test_blank_ib_account_id_is_unpinned_not_an_empty_pin(tmp_path, monkeypatch):
    """`.env.example` ships ALGO_IB_ACCOUNT_ID= empty, and compose interpolates
    an absent var to "". That must mean unpinned — an empty pin would refuse
    every Gateway session."""
    path = tmp_path / "config.yaml"
    path.write_text("mode: paper\nib:\n  account_id: DUN551088\n")

    monkeypatch.setenv("ALGO_IB_ACCOUNT_ID", "")

    assert load_config(str(path)).ib.account_id is None


def test_research_shadow_is_disabled_by_default(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("mode: paper\n")

    config = load_config(str(path))

    assert config.research.shadow_enabled is False
    assert config.research.factor_ids == [
        "price_momentum_126d",
        "high_52w",
        "low_volatility_63d",
        "liquidity_20d",
    ]
