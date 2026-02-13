from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class RiskConfig(BaseModel):
    position_entry_limit_pct: float = 5.0
    sector_concentration_pct: float = 20.0
    total_exposure_limit_pct: float = 150.0
    stop_loss_trailing_pct: float = 15.0
    drawdown_pause_pct: float = 10.0
    drawdown_circuit_breaker_pct: float = 20.0
    soft_ceiling_pct: float = 7.0
    hard_ceiling_pct: float = 15.0
    margin_warning_pct: float = 70.0
    margin_critical_pct: float = 85.0
    double_down_entry_limit_pct: float = 10.0
    passive_scan_interval_minutes: int = 30
    min_viable_fill_pct: float = 40.0
    portfolio_beta_alert_threshold: float = 1.5
    correlation_alert_threshold: float = 0.7
    correlation_min_lookback_days: int = 60


class ExecutionConfig(BaseModel):
    entry_buffer_pct: float = 0.3
    double_down_buffer_pct: float = 0.75
    reprice_interval_minutes: int = 60
    max_reprice_attempts: int = 3
    order_submission_lag_seconds: int = 5


class SignalStalenessConfig(BaseModel):
    market_data_grace_hours: int = 4
    fundamentals_days: int = 7
    events_hours: int = 48


class SignalsConfig(BaseModel):
    staleness_thresholds: SignalStalenessConfig = Field(default_factory=SignalStalenessConfig)


class MLModelConfig(BaseModel):
    retrain_cadence_months: int = 6
    target_forward_weeks: int = 8
    target_buckets: dict[str, float] = Field(default_factory=lambda: {"sell": -0.05, "buy": 0.05})
    min_training_samples: int = 200
    regime_detection_enabled: bool = True


class DataIngestionConfig(BaseModel):
    market_data_source: str = "ib"
    fundamentals_source: str = "ib"
    events_source: str = "alpha_vantage"
    polling_interval_minutes: int = 15
    ib_rate_limit_per_sec: int = 45
    backfill_years: int = 10


class UniverseConfig(BaseModel):
    watchlist_source: str = "sp500"
    custom_tickers: list[str] = Field(default_factory=list)


class IBConfig(BaseModel):
    host: str = "127.0.0.1"
    live_port: int = 7496
    paper_port: int = 7497
    client_id: int = 1


class DatabaseConfig(BaseModel):
    url: str = "postgresql://algo:algo@localhost:5432/algo_poc"


class RedisConfig(BaseModel):
    url: str = "redis://localhost:6379/0"


class NotificationsConfig(BaseModel):
    slack_enabled: bool = False
    email_enabled: bool = False
    sms_enabled: bool = False


class ObservabilityConfig(BaseModel):
    prometheus_port: int = 9090
    tracing_enabled: bool = False


class AppConfig(BaseModel):
    mode: str = "paper"
    universe: UniverseConfig = Field(default_factory=UniverseConfig)
    data_ingestion: DataIngestionConfig = Field(default_factory=DataIngestionConfig)
    signals: SignalsConfig = Field(default_factory=SignalsConfig)
    ml_model: MLModelConfig = Field(default_factory=MLModelConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    ib: IBConfig = Field(default_factory=IBConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    redis: RedisConfig = Field(default_factory=RedisConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)


ENV_PREFIX = "ALGO_"
ENV_MAP: dict[str, str] = {
    "ALGO_MODE": "mode",
    "ALGO_DATABASE_URL": "database.url",
    "ALGO_REDIS_URL": "redis.url",
}


def _apply_env_overrides(data: dict[str, Any]) -> dict[str, Any]:
    for env_key, config_path in ENV_MAP.items():
        value = os.environ.get(env_key)
        if value is not None:
            parts = config_path.split(".")
            target = data
            for part in parts[:-1]:
                target = target.setdefault(part, {})
            target[parts[-1]] = value
    return data


def load_config(path: str) -> AppConfig:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with open(config_path) as f:
        data = yaml.safe_load(f) or {}
    data = _apply_env_overrides(data)
    return AppConfig(**data)
