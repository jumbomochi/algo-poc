from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator


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
    # IBKR paper accounts reject fractional API orders (Error 10243; cashQty
    # rejected too, 10244). False = round down to whole shares and skip
    # orders that round to zero. Flip true once trading an account whose
    # fractional permission is verified to work via the API.
    fractional_orders: bool = False
    # Broker-native protective stops (KAN-19). Default OFF: with the flag off
    # behaviour is byte-identical to before the feature existed, which is what
    # makes it safe to merge ahead of the epoch-v2 boundary (KAN-33) where it
    # gets turned on.
    #
    # Turning it OFF does NOT remove stops already resting at IB — they must be
    # cancelled at the broker, or the account keeps orphan protective orders no
    # code knows about.
    broker_stops_enabled: bool = False
    # The account whose positions this service is responsible for protecting.
    # None = whatever the Gateway session reports; a stop on another account's
    # shares protects nothing (KAN-11).
    broker_stops_account_id: str | None = None
    # GTC is the property the design rests on: the KAN-18 spike watched a GTC
    # stop survive a Gateway process restart with every field intact.
    broker_stops_tif: str = "GTC"
    # IB's default (false) leaves the stop dormant outside regular hours, so an
    # overnight gap is uncovered until the open. True arms it outside RTH at
    # the cost of triggering on thin extended-hours prints. Set deliberately —
    # the spike found this is the one property "resting at IB" does not buy.
    broker_stops_outside_rth: bool = False


class SignalStalenessConfig(BaseModel):
    market_data_grace_hours: int = 4
    fundamentals_days: int = 7
    events_hours: int = 48


class SignalsConfig(BaseModel):
    staleness_thresholds: SignalStalenessConfig = Field(default_factory=SignalStalenessConfig)


class SentimentSourceConfig(BaseModel):
    enabled: bool = False


class RedditSentimentConfig(SentimentSourceConfig):
    subreddits: list[str] = Field(
        default_factory=lambda: ["wallstreetbets", "stocks", "investing"]
    )
    posts_per_subreddit: int = 100


class DiscordSentimentConfig(SentimentSourceConfig):
    # Channel ids only — the bot token comes from DISCORD_BOT_TOKEN env.
    channel_ids: list[str] = Field(default_factory=list)


class SentimentConfig(BaseModel):
    finnhub_news: SentimentSourceConfig = Field(default_factory=SentimentSourceConfig)
    stocktwits: SentimentSourceConfig = Field(default_factory=SentimentSourceConfig)
    reddit: RedditSentimentConfig = Field(default_factory=RedditSentimentConfig)
    discord: DiscordSentimentConfig = Field(default_factory=DiscordSentimentConfig)
    zscore_baseline_days: int = 60
    zscore_min_baseline_days: int = 20


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
    # Every concurrent API client needs a DISTINCT id — IB disconnects the
    # older session when a duplicate id connects. execution uses client_id;
    # data_ingestion uses data_client_id. (Backtest/paper scripts use 10+,
    # ad-hoc probes 42+.)
    client_id: int = 1
    data_client_id: int = 2
    # The exact account this system is allowed to trade (e.g. "DUN551088").
    # A DU/U prefix check proves the account *type*, not its identity — a
    # second paper account, or a Gateway repointed at a different one, passes
    # the prefix guard and takes the orders. When set, the execution session
    # must serve exactly this account and every order is stamped with it.
    # None = unpinned (prefix guard only), which is the pre-existing behaviour.
    account_id: str | None = None

    @model_validator(mode="after")
    def _blank_account_id_means_unpinned(self) -> "IBConfig":
        # An empty ALGO_IB_ACCOUNT_ID (the shape .env.example ships, and what
        # compose interpolates when the var is absent) means "not configured",
        # not "pin to the empty account" — which would refuse every session.
        if self.account_id is not None and not self.account_id.strip():
            self.account_id = None
        return self


class DatabaseConfig(BaseModel):
    url: str = "postgresql://algo:algo@localhost:5432/algo_poc"


class RedisConfig(BaseModel):
    url: str = "redis://localhost:6379/0"


class NotificationsConfig(BaseModel):
    slack_enabled: bool = False
    email_enabled: bool = False
    sms_enabled: bool = False
    # Telegram credentials come from env (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID),
    # never from config files.
    telegram_enabled: bool = False


class ObservabilityConfig(BaseModel):
    prometheus_port: int = 9090
    tracing_enabled: bool = False


class CapitalModeConfig(BaseModel):
    deployment_fraction: float = Field(ge=0.0, le=1.0)
    max_deployable_usd: float | None = Field(default=None, ge=0.0)
    entries_enabled: bool = False


class CapitalConfig(BaseModel):
    paper: CapitalModeConfig = Field(
        default_factory=lambda: CapitalModeConfig(
            deployment_fraction=1.0,
            max_deployable_usd=None,
            entries_enabled=False,
        )
    )
    live: CapitalModeConfig = Field(
        default_factory=lambda: CapitalModeConfig(
            deployment_fraction=0.0,
            max_deployable_usd=0.0,
            entries_enabled=False,
        )
    )


class CurrencyConfig(BaseModel):
    expected_base_currency: Literal["SGD"] = "SGD"
    trading_currency: Literal["USD"] = "USD"
    max_fx_age_seconds: int = Field(default=300, gt=0)
    minimum_settled_usd_reserve: float = Field(default=0.0, ge=0.0)
    commission_per_share_usd: float = Field(default=0.005, ge=0.0)
    minimum_commission_usd: float = Field(default=1.0, ge=0.0)


class ResearchConfig(BaseModel):
    shadow_enabled: bool = False
    factor_ids: list[str] = Field(
        default_factory=lambda: [
            "price_momentum_126d",
            "high_52w",
            "low_volatility_63d",
            "liquidity_20d",
        ]
    )


class AppConfig(BaseModel):
    # Literal so a typo'd ALGO_MODE (e.g. "liv") fails at startup instead of
    # silently falling through to the paper port in live deployments.
    mode: Literal["paper", "live", "backtest"] = "paper"
    universe: UniverseConfig = Field(default_factory=UniverseConfig)
    data_ingestion: DataIngestionConfig = Field(default_factory=DataIngestionConfig)
    signals: SignalsConfig = Field(default_factory=SignalsConfig)
    sentiment: SentimentConfig = Field(default_factory=SentimentConfig)
    ml_model: MLModelConfig = Field(default_factory=MLModelConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    ib: IBConfig = Field(default_factory=IBConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    redis: RedisConfig = Field(default_factory=RedisConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    capital: CapitalConfig = Field(default_factory=CapitalConfig)
    currency: CurrencyConfig = Field(default_factory=CurrencyConfig)
    research: ResearchConfig = Field(default_factory=ResearchConfig)

    @model_validator(mode="after")
    def validate_live_currency_safety(self) -> AppConfig:
        if (
            self.capital.live.entries_enabled
            and self.currency.minimum_settled_usd_reserve <= 0
        ):
            raise ValueError("live entries require a positive settled USD reserve")
        return self


ENV_PREFIX = "ALGO_"
ENV_MAP: dict[str, str] = {
    "ALGO_MODE": "mode",
    "ALGO_DATABASE_URL": "database.url",
    "ALGO_REDIS_URL": "redis.url",
    "ALGO_IB_HOST": "ib.host",  # containers set this to host.docker.internal
    "ALGO_IB_ACCOUNT_ID": "ib.account_id",
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
