# Trading Bot Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build an automated US equities trading bot with microservices architecture, ML-assisted signal combination, Interactive Brokers execution, and comprehensive risk management.

**Architecture:** 7 microservices communicating via Redis Streams, backed by PostgreSQL. Services: Data Ingestion, Signal Generation, ML Model, Risk Management, Execution, API (FastAPI), Notifications. A backtesting engine replays historical data through the same pipeline.

**Tech Stack:** Python 3.12+, Redis Streams, PostgreSQL, SQLAlchemy + Alembic, Pydantic, FastAPI, LightGBM, ib_insync, Docker Compose, pytest, exchange_calendars, OpenTelemetry, Prometheus

**Design doc:** `docs/plans/2026-02-13-trading-bot-design.md` — read this for full context on every decision.

---

## Phase 1: Project Foundation

### Task 1: Project Scaffolding

**Files:**
- Create: `pyproject.toml`
- Create: `services/__init__.py`
- Create: `services/data_ingestion/__init__.py`
- Create: `services/signal_generation/__init__.py`
- Create: `services/ml_model/__init__.py`
- Create: `services/risk_management/__init__.py`
- Create: `services/execution/__init__.py`
- Create: `services/api/__init__.py`
- Create: `services/notifications/__init__.py`
- Create: `shared/__init__.py`
- Create: `shared/models/__init__.py`
- Create: `shared/schemas/__init__.py`
- Create: `backtest/__init__.py`
- Create: `config/default.yaml`
- Create: `config/.gitignore`
- Create: `tests/__init__.py`
- Create: `.gitignore`

**Step 1: Create pyproject.toml**

```toml
[project]
name = "algo-poc"
version = "0.1.0"
description = "Automated US equities trading bot"
requires-python = ">=3.12"
dependencies = [
    "sqlalchemy>=2.0",
    "alembic>=1.13",
    "psycopg2-binary>=2.9",
    "redis>=5.0",
    "pydantic>=2.0",
    "pydantic-settings>=2.0",
    "fastapi>=0.110",
    "uvicorn>=0.27",
    "httpx>=0.27",
    "ib_insync>=0.9",
    "lightgbm>=4.0",
    "pandas>=2.0",
    "numpy>=1.26",
    "exchange-calendars>=4.0",
    "pyyaml>=6.0",
    "structlog>=24.0",
    "opentelemetry-api>=1.20",
    "opentelemetry-sdk>=1.20",
    "prometheus-client>=0.20",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "pytest-cov>=4.0",
    "fakeredis>=2.21",
    "factory-boy>=3.3",
    "testcontainers[postgres,redis]>=3.7",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
```

**Step 2: Create directory structure**

Create all `__init__.py` files (empty) for:
- `services/` and each sub-service
- `shared/`, `shared/models/`, `shared/schemas/`
- `backtest/`
- `tests/`

**Step 3: Create config/default.yaml**

```yaml
mode: paper  # live | backtest | paper

universe:
  watchlist_source: sp500
  custom_tickers: []

data_ingestion:
  market_data_source: ib
  fundamentals_source: ib
  events_source: alpha_vantage
  polling_interval_minutes: 15
  ib_rate_limit_per_sec: 45  # buffer below IB's 50/sec
  backfill_years: 10

signals:
  staleness_thresholds:
    market_data_grace_hours: 4  # hours after expected session close
    fundamentals_days: 7
    events_hours: 48

ml_model:
  retrain_cadence_months: 6
  target_forward_weeks: 8
  target_buckets:
    sell: -0.05  # below -5% return
    buy: 0.05    # above +5% return
  min_training_samples: 200
  regime_detection_enabled: true

risk:
  position_entry_limit_pct: 5.0      # % of NAV
  sector_concentration_pct: 20.0      # % of NAV
  total_exposure_limit_pct: 150.0     # % of NAV (margin)
  stop_loss_trailing_pct: 15.0        # trailing stop %
  drawdown_pause_pct: 10.0            # pause new buys
  drawdown_circuit_breaker_pct: 20.0  # liquidate all
  soft_ceiling_pct: 7.0               # notify only
  hard_ceiling_pct: 15.0              # auto-trim to soft_ceiling
  margin_warning_pct: 70.0            # of IB maintenance margin
  margin_critical_pct: 85.0           # of IB maintenance margin
  double_down_entry_limit_pct: 10.0   # of NAV
  passive_scan_interval_minutes: 30
  min_viable_fill_pct: 40.0           # partial fill threshold
  portfolio_beta_alert_threshold: 1.5
  correlation_alert_threshold: 0.7
  correlation_min_lookback_days: 60

execution:
  entry_buffer_pct: 0.3          # limit order buffer
  double_down_buffer_pct: 0.75   # wider buffer
  reprice_interval_minutes: 60
  max_reprice_attempts: 3
  order_submission_lag_seconds: 5  # simulated lag for backtest

ib:
  host: "127.0.0.1"
  live_port: 7496
  paper_port: 7497
  client_id: 1

notifications:
  slack_enabled: false
  email_enabled: false
  sms_enabled: false

database:
  url: "postgresql://algo:algo@localhost:5432/algo_poc"

redis:
  url: "redis://localhost:6379/0"

observability:
  prometheus_port: 9090
  tracing_enabled: false
```

**Step 4: Create config/.gitignore**

```
secrets.yaml
```

**Step 5: Create .gitignore**

```
__pycache__/
*.pyc
.venv/
*.egg-info/
dist/
.pytest_cache/
.coverage
config/secrets.yaml
*.db
.env
```

**Step 6: Commit**

```bash
git add -A
git commit -m "feat: project scaffolding with directory structure and config"
```

---

### Task 2: Configuration System

**Files:**
- Create: `shared/config.py`
- Test: `tests/shared/test_config.py`

**Step 1: Write the failing test**

```python
# tests/shared/__init__.py (empty)
# tests/shared/test_config.py
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


def test_load_config_missing_file():
    with pytest.raises(FileNotFoundError):
        load_config("/nonexistent/path.yaml")
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/shared/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'shared.config'`

**Step 3: Write minimal implementation**

```python
# shared/config.py
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel


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
    staleness_thresholds: SignalStalenessConfig = SignalStalenessConfig()


class MLModelConfig(BaseModel):
    retrain_cadence_months: int = 6
    target_forward_weeks: int = 8
    target_buckets: dict[str, float] = {"sell": -0.05, "buy": 0.05}
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
    custom_tickers: list[str] = []


class IBConfig(BaseModel):
    host: str = "127.0.0.1"
    live_port: int = 7496
    paper_port: int = 7497
    client_id: int = 1


class DatabaseConfig(BaseModel):
    url: str = "postgresql://algo:algo@localhost:5432/algo_poc"


class RedisConfig(BaseModel):
    url: str = "redis://localhost:6379/0"


class AppConfig(BaseModel):
    mode: str = "paper"
    universe: UniverseConfig = UniverseConfig()
    data_ingestion: DataIngestionConfig = DataIngestionConfig()
    signals: SignalsConfig = SignalsConfig()
    ml_model: MLModelConfig = MLModelConfig()
    risk: RiskConfig = RiskConfig()
    execution: ExecutionConfig = ExecutionConfig()
    ib: IBConfig = IBConfig()
    database: DatabaseConfig = DatabaseConfig()
    redis: RedisConfig = RedisConfig()


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
```

**Step 4: Run test to verify it passes**

Run: `python -m pytest tests/shared/test_config.py -v`
Expected: 3 PASSED

**Step 5: Commit**

```bash
git add shared/config.py tests/shared/
git commit -m "feat: configuration system with YAML loading and env overrides"
```

---

### Task 3: Database Models and Migrations

**Files:**
- Create: `shared/models/base.py`
- Create: `shared/models/market_data.py`
- Create: `shared/models/fundamentals.py`
- Create: `shared/models/events.py`
- Create: `shared/models/signals.py`
- Create: `shared/models/portfolio.py`
- Create: `shared/models/audit.py`
- Create: `shared/models/ml_models.py`
- Create: `alembic.ini`
- Create: `migrations/env.py`
- Create: `migrations/versions/.gitkeep`
- Test: `tests/shared/test_models.py`

**Step 1: Write the failing test**

```python
# tests/shared/test_models.py
from datetime import datetime, timezone
from shared.models.base import Base
from shared.models.market_data import OHLCVDaily
from shared.models.portfolio import Position, Trade
from shared.models.audit import AuditLog
from shared.models.signals import SignalRecord
from shared.models.fundamentals import FundamentalRecord
from shared.models.events import EventRecord
from shared.models.ml_models import ModelVersion


def test_ohlcv_model_has_required_columns():
    cols = {c.name for c in OHLCVDaily.__table__.columns}
    assert cols >= {"id", "ticker", "date", "open", "high", "low", "close", "volume", "ingested_at"}


def test_position_model_has_nav_fields():
    cols = {c.name for c in Position.__table__.columns}
    assert cols >= {"id", "ticker", "quantity", "avg_entry_price", "sector", "opened_at", "status"}


def test_trade_model_has_audit_fields():
    cols = {c.name for c in Trade.__table__.columns}
    assert cols >= {"id", "ticker", "side", "quantity", "price", "order_type", "recommendation_id", "executed_at"}


def test_audit_log_has_required_fields():
    cols = {c.name for c in AuditLog.__table__.columns}
    assert cols >= {"id", "timestamp", "service", "action", "decision", "context"}


def test_fundamental_record_has_point_in_time_fields():
    cols = {c.name for c in FundamentalRecord.__table__.columns}
    assert cols >= {"id", "ticker", "effective_at", "ingested_at", "source_revision"}


def test_event_record_has_point_in_time_fields():
    cols = {c.name for c in EventRecord.__table__.columns}
    assert cols >= {"id", "ticker", "effective_at", "ingested_at", "source_revision"}


def test_signal_record_has_computed_at():
    cols = {c.name for c in SignalRecord.__table__.columns}
    assert cols >= {"id", "ticker", "signal_name", "signal_value", "confidence", "computed_at"}


def test_model_version_has_registry_fields():
    cols = {c.name for c in ModelVersion.__table__.columns}
    assert cols >= {"id", "version", "training_window_start", "training_window_end", "metrics", "model_path", "created_at"}


def test_all_models_share_base():
    assert issubclass(OHLCVDaily, Base)
    assert issubclass(Position, Base)
    assert issubclass(Trade, Base)
    assert issubclass(AuditLog, Base)
    assert issubclass(SignalRecord, Base)
    assert issubclass(FundamentalRecord, Base)
    assert issubclass(EventRecord, Base)
    assert issubclass(ModelVersion, Base)
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/shared/test_models.py -v`
Expected: FAIL with `ModuleNotFoundError`

**Step 3: Write minimal implementation**

```python
# shared/models/base.py
from sqlalchemy.orm import DeclarativeBase

class Base(DeclarativeBase):
    pass
```

```python
# shared/models/market_data.py
from datetime import date, datetime
from sqlalchemy import String, Date, Float, BigInteger, DateTime, Index
from sqlalchemy.orm import Mapped, mapped_column
from shared.models.base import Base


class OHLCVDaily(Base):
    __tablename__ = "ohlcv_daily"
    __table_args__ = (
        Index("ix_ohlcv_ticker_date", "ticker", "date", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker: Mapped[str] = mapped_column(String(10), nullable=False)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    open: Mapped[float] = mapped_column(Float, nullable=False)
    high: Mapped[float] = mapped_column(Float, nullable=False)
    low: Mapped[float] = mapped_column(Float, nullable=False)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    volume: Mapped[int] = mapped_column(BigInteger, nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
```

```python
# shared/models/fundamentals.py
from datetime import datetime
from sqlalchemy import String, DateTime, JSON
from sqlalchemy.orm import Mapped, mapped_column
from shared.models.base import Base


class FundamentalRecord(Base):
    __tablename__ = "fundamental_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker: Mapped[str] = mapped_column(String(10), nullable=False)
    metric_type: Mapped[str] = mapped_column(String(50), nullable=False)
    data: Mapped[dict] = mapped_column(JSON, nullable=False)
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_revision: Mapped[str] = mapped_column(String(100), nullable=False)
```

```python
# shared/models/events.py
from datetime import datetime
from sqlalchemy import String, DateTime, JSON, Float
from sqlalchemy.orm import Mapped, mapped_column
from shared.models.base import Base


class EventRecord(Base):
    __tablename__ = "event_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker: Mapped[str] = mapped_column(String(10), nullable=False)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    data: Mapped[dict] = mapped_column(JSON, nullable=False)
    sentiment_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_revision: Mapped[str] = mapped_column(String(100), nullable=False)
```

```python
# shared/models/signals.py
from datetime import datetime
from sqlalchemy import String, DateTime, Float, Index
from sqlalchemy.orm import Mapped, mapped_column
from shared.models.base import Base


class SignalRecord(Base):
    __tablename__ = "signal_records"
    __table_args__ = (
        Index("ix_signal_ticker_name_computed", "ticker", "signal_name", "computed_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker: Mapped[str] = mapped_column(String(10), nullable=False)
    signal_name: Mapped[str] = mapped_column(String(50), nullable=False)
    signal_value: Mapped[float] = mapped_column(Float, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
```

```python
# shared/models/portfolio.py
from datetime import datetime
from sqlalchemy import String, DateTime, Float, Integer, Index
from sqlalchemy.orm import Mapped, mapped_column
from shared.models.base import Base


class Position(Base):
    __tablename__ = "positions"
    __table_args__ = (
        Index("ix_positions_ticker_status", "ticker", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker: Mapped[str] = mapped_column(String(10), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    avg_entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    current_price: Mapped[float] = mapped_column(Float, nullable=False)
    sector: Mapped[str] = mapped_column(String(50), nullable=False)
    highest_price_since_entry: Mapped[float] = mapped_column(Float, nullable=False)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open")


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker: Mapped[str] = mapped_column(String(10), nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)  # buy / sell
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    order_type: Mapped[str] = mapped_column(String(20), nullable=False)
    recommendation_id: Mapped[str] = mapped_column(String(50), nullable=False)
    commission: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    slippage: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
```

```python
# shared/models/audit.py
from datetime import datetime
from sqlalchemy import String, DateTime, JSON, Text
from sqlalchemy.orm import Mapped, mapped_column
from shared.models.base import Base


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    service: Mapped[str] = mapped_column(String(50), nullable=False)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    decision: Mapped[str] = mapped_column(String(50), nullable=False)
    context: Mapped[dict] = mapped_column(JSON, nullable=False)
    actor: Mapped[str | None] = mapped_column(String(50), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
```

```python
# shared/models/ml_models.py
from datetime import datetime, date
from sqlalchemy import String, DateTime, Date, JSON
from sqlalchemy.orm import Mapped, mapped_column
from shared.models.base import Base


class ModelVersion(Base):
    __tablename__ = "model_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    training_window_start: Mapped[date] = mapped_column(Date, nullable=False)
    training_window_end: Mapped[date] = mapped_column(Date, nullable=False)
    metrics: Mapped[dict] = mapped_column(JSON, nullable=False)
    model_path: Mapped[str] = mapped_column(String(500), nullable=False)
    is_active: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
```

Update `shared/models/__init__.py`:

```python
from shared.models.base import Base
from shared.models.market_data import OHLCVDaily
from shared.models.fundamentals import FundamentalRecord
from shared.models.events import EventRecord
from shared.models.signals import SignalRecord
from shared.models.portfolio import Position, Trade
from shared.models.audit import AuditLog
from shared.models.ml_models import ModelVersion

__all__ = [
    "Base", "OHLCVDaily", "FundamentalRecord", "EventRecord",
    "SignalRecord", "Position", "Trade", "AuditLog", "ModelVersion",
]
```

**Step 4: Run test to verify it passes**

Run: `python -m pytest tests/shared/test_models.py -v`
Expected: 9 PASSED

**Step 5: Set up Alembic**

Run: `alembic init migrations`

Edit `alembic.ini` to set `sqlalchemy.url = postgresql://algo:algo@localhost:5432/algo_poc`

Edit `migrations/env.py` to import `from shared.models import Base` and set `target_metadata = Base.metadata`.

**Step 6: Commit**

```bash
git add shared/models/ tests/shared/test_models.py alembic.ini migrations/
git commit -m "feat: database models with point-in-time fields and Alembic setup"
```

---

### Task 4: Redis Streams Client

**Files:**
- Create: `shared/redis_client.py`
- Test: `tests/shared/test_redis_client.py`

**Step 1: Write the failing test**

```python
# tests/shared/test_redis_client.py
import pytest
import json
from unittest.mock import AsyncMock, MagicMock, patch
from shared.redis_client import RedisStreamClient, StreamMessage


class TestRedisStreamClient:
    def test_stream_message_dataclass(self):
        msg = StreamMessage(
            stream="stream:test",
            message_id="1234-0",
            data={"ticker": "AAPL", "value": "100.0"},
        )
        assert msg.stream == "stream:test"
        assert msg.data["ticker"] == "AAPL"

    @pytest.mark.asyncio
    async def test_publish_adds_to_stream(self):
        mock_redis = AsyncMock()
        mock_redis.xadd = AsyncMock(return_value="1234-0")
        client = RedisStreamClient(mock_redis)
        msg_id = await client.publish("stream:test", {"ticker": "AAPL", "price": "150.0"})
        mock_redis.xadd.assert_called_once_with(
            "stream:test",
            {"ticker": "AAPL", "price": "150.0"},
        )
        assert msg_id == "1234-0"

    @pytest.mark.asyncio
    async def test_publish_with_idempotency_key(self):
        mock_redis = AsyncMock()
        mock_redis.xadd = AsyncMock(return_value="1234-0")
        client = RedisStreamClient(mock_redis)
        await client.publish(
            "stream:test",
            {"ticker": "AAPL"},
            idempotency_key="rec-001",
        )
        call_data = mock_redis.xadd.call_args[0][1]
        assert call_data["_idempotency_key"] == "rec-001"

    @pytest.mark.asyncio
    async def test_create_consumer_group(self):
        mock_redis = AsyncMock()
        mock_redis.xgroup_create = AsyncMock()
        client = RedisStreamClient(mock_redis)
        await client.create_consumer_group("stream:test", "my-group")
        mock_redis.xgroup_create.assert_called_once()

    @pytest.mark.asyncio
    async def test_ack_message(self):
        mock_redis = AsyncMock()
        mock_redis.xack = AsyncMock(return_value=1)
        client = RedisStreamClient(mock_redis)
        result = await client.ack("stream:test", "my-group", "1234-0")
        assert result == 1
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/shared/test_redis_client.py -v`
Expected: FAIL with `ModuleNotFoundError`

**Step 3: Write minimal implementation**

```python
# shared/redis_client.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import redis.asyncio as aioredis


DEAD_LETTER_SUFFIX = ":dlq"


@dataclass
class StreamMessage:
    stream: str
    message_id: str
    data: dict[str, str]


class RedisStreamClient:
    def __init__(self, redis: aioredis.Redis):
        self._redis = redis

    async def publish(
        self,
        stream: str,
        data: dict[str, str],
        idempotency_key: str | None = None,
    ) -> str:
        if idempotency_key:
            data = {**data, "_idempotency_key": idempotency_key}
        return await self._redis.xadd(stream, data)

    async def create_consumer_group(
        self,
        stream: str,
        group: str,
        start_id: str = "0",
    ) -> None:
        try:
            await self._redis.xgroup_create(stream, group, start_id, mkstream=True)
        except aioredis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

    async def read_group(
        self,
        stream: str,
        group: str,
        consumer: str,
        count: int = 10,
        block_ms: int = 5000,
    ) -> list[StreamMessage]:
        results = await self._redis.xreadgroup(
            group, consumer, {stream: ">"}, count=count, block=block_ms,
        )
        messages = []
        for stream_name, entries in results:
            s = stream_name if isinstance(stream_name, str) else stream_name.decode()
            for msg_id, fields in entries:
                mid = msg_id if isinstance(msg_id, str) else msg_id.decode()
                decoded = {
                    (k if isinstance(k, str) else k.decode()): (v if isinstance(v, str) else v.decode())
                    for k, v in fields.items()
                }
                messages.append(StreamMessage(stream=s, message_id=mid, data=decoded))
        return messages

    async def ack(self, stream: str, group: str, message_id: str) -> int:
        return await self._redis.xack(stream, group, message_id)

    async def send_to_dead_letter(
        self,
        stream: str,
        message: StreamMessage,
        error: str,
    ) -> str:
        dlq_stream = stream + DEAD_LETTER_SUFFIX
        data = {**message.data, "_error": error, "_original_id": message.message_id}
        return await self._redis.xadd(dlq_stream, data)
```

**Step 4: Run test to verify it passes**

Run: `python -m pytest tests/shared/test_redis_client.py -v`
Expected: 5 PASSED

**Step 5: Commit**

```bash
git add shared/redis_client.py tests/shared/test_redis_client.py
git commit -m "feat: Redis Streams client with consumer groups, ack, and dead-letter"
```

---

### Task 5: Market Calendar Module

**Files:**
- Create: `shared/market_calendar.py`
- Test: `tests/shared/test_market_calendar.py`

**Step 1: Write the failing test**

```python
# tests/shared/test_market_calendar.py
from datetime import datetime, date
from zoneinfo import ZoneInfo
from shared.market_calendar import MarketCalendar

ET = ZoneInfo("America/New_York")


def test_is_market_open_during_trading_hours():
    cal = MarketCalendar()
    # A known trading day: 2025-01-06 (Monday) at 10:00 AM ET
    dt = datetime(2025, 1, 6, 10, 0, tzinfo=ET)
    assert cal.is_market_open(dt) is True


def test_is_market_closed_on_weekend():
    cal = MarketCalendar()
    dt = datetime(2025, 1, 4, 10, 0, tzinfo=ET)  # Saturday
    assert cal.is_market_open(dt) is False


def test_is_market_closed_on_holiday():
    cal = MarketCalendar()
    # MLK Day 2025 = Jan 20
    dt = datetime(2025, 1, 20, 10, 0, tzinfo=ET)
    assert cal.is_market_open(dt) is False


def test_get_last_session_close():
    cal = MarketCalendar()
    # On a Tuesday morning before open, last session close was Monday 4 PM
    dt = datetime(2025, 1, 7, 8, 0, tzinfo=ET)  # Tuesday 8 AM
    last_close = cal.get_last_session_close(dt)
    assert last_close.date() == date(2025, 1, 6)  # Monday


def test_get_next_market_close():
    cal = MarketCalendar()
    dt = datetime(2025, 1, 6, 10, 0, tzinfo=ET)  # Monday 10 AM
    next_close = cal.get_next_market_close(dt)
    assert next_close.date() == date(2025, 1, 6)
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/shared/test_market_calendar.py -v`
Expected: FAIL with `ModuleNotFoundError`

**Step 3: Write minimal implementation**

```python
# shared/market_calendar.py
from __future__ import annotations

from datetime import datetime, date
from zoneinfo import ZoneInfo

import exchange_calendars as xcals

ET = ZoneInfo("America/New_York")


class MarketCalendar:
    def __init__(self, exchange: str = "XNYS"):
        self._cal = xcals.get_calendar(exchange)

    def is_market_open(self, dt: datetime) -> bool:
        return self._cal.is_open_on_minute(dt.astimezone(ET).replace(tzinfo=None), _parse=False) if self._is_session(dt) else False

    def _is_session(self, dt: datetime) -> bool:
        d = dt.astimezone(ET).date()
        return self._cal.is_session(d)

    def is_trading_day(self, d: date) -> bool:
        return self._cal.is_session(d)

    def get_last_session_close(self, dt: datetime) -> datetime:
        d = dt.astimezone(ET).date()
        prev = self._cal.previous_close(d)
        return prev.to_pydatetime().replace(tzinfo=ET)

    def get_next_market_close(self, dt: datetime) -> datetime:
        d = dt.astimezone(ET).date()
        if self._cal.is_session(d):
            close = self._cal.session_close(d)
            return close.to_pydatetime().replace(tzinfo=ET)
        next_session = self._cal.next_session(d)
        close = self._cal.session_close(next_session)
        return close.to_pydatetime().replace(tzinfo=ET)
```

**Step 4: Run test to verify it passes**

Run: `python -m pytest tests/shared/test_market_calendar.py -v`
Expected: 5 PASSED

**Step 5: Commit**

```bash
git add shared/market_calendar.py tests/shared/test_market_calendar.py
git commit -m "feat: market calendar with open/close and holiday awareness"
```

---

### Task 6: Shared Pydantic Schemas

**Files:**
- Create: `shared/schemas/messages.py`
- Test: `tests/shared/test_schemas.py`

**Step 1: Write the failing test**

```python
# tests/shared/test_schemas.py
from datetime import datetime, timezone
from shared.schemas.messages import (
    MarketDataMessage,
    FundamentalMessage,
    EventMessage,
    SignalMessage,
    RecommendationMessage,
    ApprovedOrderMessage,
    FillMessage,
    AlertMessage,
    KillMessage,
)


def test_market_data_message():
    msg = MarketDataMessage(
        ticker="AAPL", timestamp=datetime.now(timezone.utc),
        open=150.0, high=155.0, low=149.0, close=153.0, volume=1000000,
    )
    assert msg.ticker == "AAPL"
    data = msg.to_stream_dict()
    assert isinstance(data["timestamp"], str)
    assert data["ticker"] == "AAPL"


def test_signal_message_normalization():
    msg = SignalMessage(
        ticker="AAPL",
        timestamp=datetime.now(timezone.utc),
        signal_name="support_proximity",
        signal_value=0.85,
        confidence=0.9,
        computed_at=datetime.now(timezone.utc),
    )
    assert -1.0 <= msg.signal_value <= 1.0


def test_recommendation_message_has_top_features():
    msg = RecommendationMessage(
        ticker="AAPL",
        timestamp=datetime.now(timezone.utc),
        action="buy",
        confidence=0.82,
        top_features={"support_proximity": 0.4, "valuation": 0.3},
        recommendation_id="rec-001",
    )
    assert "support_proximity" in msg.top_features


def test_alert_message_has_priority():
    msg = AlertMessage(
        timestamp=datetime.now(timezone.utc),
        event_type="soft_ceiling_breach",
        priority="medium",
        message="AAPL drifted above 7% of NAV",
        context={"ticker": "AAPL", "pct_of_nav": 7.5},
    )
    assert msg.priority in ("low", "medium", "high", "critical")


def test_kill_message():
    msg = KillMessage(
        timestamp=datetime.now(timezone.utc),
        triggered_by="operator",
        reason="Manual kill switch",
    )
    assert msg.triggered_by == "operator"
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/shared/test_schemas.py -v`
Expected: FAIL with `ModuleNotFoundError`

**Step 3: Write minimal implementation**

```python
# shared/schemas/messages.py
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel


class StreamSerializable(BaseModel):
    def to_stream_dict(self) -> dict[str, str]:
        data = self.model_dump(mode="json")
        return {k: str(v) if not isinstance(v, str) else v for k, v in data.items()}

    @classmethod
    def from_stream_dict(cls, data: dict[str, str]):
        return cls.model_validate_json(
            "{" + ",".join(f'"{k}":{v}' if v.replace(".", "").replace("-", "").isdigit() else f'"{k}":"{v}"' for k, v in data.items()) + "}"
        ) if False else cls.model_validate(data)


class MarketDataMessage(StreamSerializable):
    ticker: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


class FundamentalMessage(StreamSerializable):
    ticker: str
    timestamp: datetime
    metric_type: str
    data: dict[str, Any]
    effective_at: datetime
    ingested_at: datetime
    source_revision: str


class EventMessage(StreamSerializable):
    ticker: str
    timestamp: datetime
    event_type: str
    data: dict[str, Any]
    sentiment_score: float | None = None
    effective_at: datetime
    ingested_at: datetime
    source_revision: str


class SignalMessage(StreamSerializable):
    ticker: str
    timestamp: datetime
    signal_name: str
    signal_value: float  # -1.0 to 1.0
    confidence: float    # 0.0 to 1.0
    computed_at: datetime


class RecommendationMessage(StreamSerializable):
    ticker: str
    timestamp: datetime
    action: Literal["buy", "sell", "hold"]
    confidence: float
    top_features: dict[str, float]
    recommendation_id: str


class ApprovedOrderMessage(StreamSerializable):
    ticker: str
    timestamp: datetime
    action: Literal["buy", "sell"]
    quantity: int
    order_type: Literal["limit", "market"]
    limit_price: float | None = None
    recommendation_id: str
    risk_adjustments: dict[str, Any] = {}


class FillMessage(StreamSerializable):
    ticker: str
    timestamp: datetime
    side: Literal["buy", "sell"]
    quantity: int
    fill_price: float
    commission: float
    recommendation_id: str
    order_id: str


class AlertMessage(StreamSerializable):
    timestamp: datetime
    event_type: str
    priority: Literal["low", "medium", "high", "critical"]
    message: str
    context: dict[str, Any] = {}


class KillMessage(StreamSerializable):
    timestamp: datetime
    triggered_by: str
    reason: str
```

Update `shared/schemas/__init__.py`:

```python
from shared.schemas.messages import *
```

**Step 4: Run test to verify it passes**

Run: `python -m pytest tests/shared/test_schemas.py -v`
Expected: 5 PASSED

**Step 5: Commit**

```bash
git add shared/schemas/ tests/shared/test_schemas.py
git commit -m "feat: shared Pydantic message schemas for all Redis streams"
```

---

### Task 7: Structured Logging

**Files:**
- Create: `shared/logging.py`
- Test: `tests/shared/test_logging.py`

**Step 1: Write the failing test**

```python
# tests/shared/test_logging.py
import json
from io import StringIO
from shared.logging import get_logger


def test_get_logger_returns_bound_logger():
    logger = get_logger("test-service")
    assert logger is not None


def test_logger_outputs_json(capsys):
    logger = get_logger("test-service")
    logger.info("test message", ticker="AAPL")
    captured = capsys.readouterr()
    # structlog outputs to stderr by default or stdout depending on config
    output = captured.err or captured.out
    assert "test message" in output or "test-service" in output
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/shared/test_logging.py -v`
Expected: FAIL

**Step 3: Write implementation**

```python
# shared/logging.py
from __future__ import annotations

import structlog


def configure_logging() -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(service: str) -> structlog.BoundLogger:
    configure_logging()
    return structlog.get_logger(service=service)
```

**Step 4: Run test to verify it passes**

Run: `python -m pytest tests/shared/test_logging.py -v`
Expected: 2 PASSED

**Step 5: Commit**

```bash
git add shared/logging.py tests/shared/test_logging.py
git commit -m "feat: structured JSON logging with structlog"
```

---

## Phase 2: Data Ingestion Service

### Task 8: Market Data Pipeline

**Files:**
- Create: `services/data_ingestion/market_data.py`
- Create: `services/data_ingestion/ib_client.py`
- Test: `tests/services/data_ingestion/test_market_data.py`

**Step 1: Write the failing test**

```python
# tests/services/__init__.py (empty)
# tests/services/data_ingestion/__init__.py (empty)
# tests/services/data_ingestion/test_market_data.py
import pytest
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from services.data_ingestion.market_data import MarketDataPipeline


class TestMarketDataPipeline:
    @pytest.mark.asyncio
    async def test_fetch_daily_bars_returns_normalized_data(self):
        mock_ib = MagicMock()
        mock_ib.get_daily_bars = AsyncMock(return_value=[
            {"date": date(2025, 1, 6), "open": 150.0, "high": 155.0, "low": 149.0, "close": 153.0, "volume": 1000000},
        ])
        pipeline = MarketDataPipeline(ib_client=mock_ib, redis_client=AsyncMock(), db_session=MagicMock())
        bars = await pipeline.fetch_daily_bars("AAPL", date(2025, 1, 6), date(2025, 1, 6))
        assert len(bars) == 1
        assert bars[0]["ticker"] == "AAPL"
        assert bars[0]["close"] == 153.0

    @pytest.mark.asyncio
    async def test_publish_to_stream(self):
        mock_redis = AsyncMock()
        mock_redis.publish = AsyncMock(return_value="1234-0")
        mock_ib = MagicMock()
        mock_ib.get_daily_bars = AsyncMock(return_value=[
            {"date": date(2025, 1, 6), "open": 150.0, "high": 155.0, "low": 149.0, "close": 153.0, "volume": 1000000},
        ])
        pipeline = MarketDataPipeline(ib_client=mock_ib, redis_client=mock_redis, db_session=MagicMock())
        await pipeline.ingest("AAPL", date(2025, 1, 6), date(2025, 1, 6))
        mock_redis.publish.assert_called_once()

    @pytest.mark.asyncio
    async def test_rate_limiting(self):
        mock_ib = MagicMock()
        mock_ib.get_daily_bars = AsyncMock(return_value=[])
        pipeline = MarketDataPipeline(
            ib_client=mock_ib, redis_client=AsyncMock(), db_session=MagicMock(),
            rate_limit_per_sec=2,
        )
        # Should not raise even with multiple rapid calls
        for ticker in ["AAPL", "MSFT", "GOOG"]:
            await pipeline.fetch_daily_bars(ticker, date(2025, 1, 6), date(2025, 1, 6))
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/services/data_ingestion/test_market_data.py -v`
Expected: FAIL

**Step 3: Write implementation**

```python
# services/data_ingestion/ib_client.py
from __future__ import annotations

import asyncio
from datetime import date
from typing import Any, Protocol


class IBClientProtocol(Protocol):
    async def get_daily_bars(self, ticker: str, start: date, end: date) -> list[dict[str, Any]]: ...
    async def get_fundamentals(self, ticker: str) -> dict[str, Any]: ...
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...


class IBClient:
    """Wrapper around ib_insync for market data. Implementation connects to real IB."""

    def __init__(self, host: str, port: int, client_id: int):
        self._host = host
        self._port = port
        self._client_id = client_id
        self._ib = None

    async def connect(self) -> None:
        from ib_insync import IB
        self._ib = IB()
        await self._ib.connectAsync(self._host, self._port, clientId=self._client_id)

    async def disconnect(self) -> None:
        if self._ib:
            self._ib.disconnect()

    async def get_daily_bars(self, ticker: str, start: date, end: date) -> list[dict[str, Any]]:
        from ib_insync import Stock
        contract = Stock(ticker, "SMART", "USD")
        bars = await self._ib.reqHistoricalDataAsync(
            contract, endDateTime=end.strftime("%Y%m%d 23:59:59"),
            durationStr=f"{(end - start).days + 1} D",
            barSizeSetting="1 day", whatToShow="TRADES", useRTH=True,
        )
        return [
            {"date": b.date, "open": b.open, "high": b.high, "low": b.low, "close": b.close, "volume": b.volume}
            for b in bars
        ]

    async def get_fundamentals(self, ticker: str) -> dict[str, Any]:
        from ib_insync import Stock
        contract = Stock(ticker, "SMART", "USD")
        data = await self._ib.reqFundamentalDataAsync(contract, reportType="ReportSnapshot")
        return {"raw": data}
```

```python
# services/data_ingestion/market_data.py
from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from typing import Any

from shared.redis_client import RedisStreamClient
from shared.schemas.messages import MarketDataMessage
from services.data_ingestion.ib_client import IBClientProtocol


class MarketDataPipeline:
    def __init__(
        self,
        ib_client: IBClientProtocol,
        redis_client: RedisStreamClient,
        db_session: Any,
        rate_limit_per_sec: int = 45,
    ):
        self._ib = ib_client
        self._redis = redis_client
        self._db = db_session
        self._semaphore = asyncio.Semaphore(rate_limit_per_sec)

    async def fetch_daily_bars(self, ticker: str, start: date, end: date) -> list[dict[str, Any]]:
        async with self._semaphore:
            bars = await self._ib.get_daily_bars(ticker, start, end)
            return [
                {
                    "ticker": ticker,
                    "date": b["date"],
                    "open": b["open"],
                    "high": b["high"],
                    "low": b["low"],
                    "close": b["close"],
                    "volume": b["volume"],
                }
                for b in bars
            ]

    async def ingest(self, ticker: str, start: date, end: date) -> None:
        bars = await self.fetch_daily_bars(ticker, start, end)
        for bar in bars:
            msg = MarketDataMessage(
                ticker=bar["ticker"],
                timestamp=datetime.now(timezone.utc),
                open=bar["open"],
                high=bar["high"],
                low=bar["low"],
                close=bar["close"],
                volume=bar["volume"],
            )
            await self._redis.publish("stream:market_data", msg.to_stream_dict())
```

**Step 4: Run test to verify it passes**

Run: `python -m pytest tests/services/data_ingestion/test_market_data.py -v`
Expected: 3 PASSED

**Step 5: Commit**

```bash
git add services/data_ingestion/ tests/services/
git commit -m "feat: market data pipeline with IB client and rate limiting"
```

---

### Task 9: Fundamentals and Events Pipelines

**Files:**
- Create: `services/data_ingestion/fundamentals.py`
- Create: `services/data_ingestion/events.py`
- Test: `tests/services/data_ingestion/test_fundamentals.py`
- Test: `tests/services/data_ingestion/test_events.py`

Follow the same TDD pattern as Task 8. Key requirements:
- Both pipelines must attach `effective_at`, `ingested_at`, `source_revision` to every record (addendum #4).
- Fundamentals pipeline pulls from IB fundamental data.
- Events pipeline polls a news API (initially stub the external call behind an interface).
- Both publish to their respective Redis streams.
- Both persist to PostgreSQL via SQLAlchemy.

**Step 5: Commit**

```bash
git add services/data_ingestion/ tests/services/data_ingestion/
git commit -m "feat: fundamentals and events pipelines with point-in-time tracking"
```

---

### Task 10: Data Ingestion Service Runner

**Files:**
- Create: `services/data_ingestion/runner.py`
- Test: `tests/services/data_ingestion/test_runner.py`

This is the service entrypoint that:
- Loads config from YAML
- Connects to IB, Redis, PostgreSQL
- Runs all three pipelines on their configured schedules
- Respects market hours (uses `MarketCalendar`)
- Exposes `/health` endpoint
- Handles graceful shutdown on SIGTERM

**Step 5: Commit**

```bash
git add services/data_ingestion/runner.py tests/services/data_ingestion/test_runner.py
git commit -m "feat: data ingestion service runner with scheduling and health check"
```

---

## Phase 3: Signal Generation Service

### Task 11: Signal Base Class and Plugin Architecture

**Files:**
- Create: `services/signal_generation/base.py`
- Test: `tests/services/signal_generation/test_base.py`

**Step 1: Write the failing test**

```python
# tests/services/signal_generation/__init__.py (empty)
# tests/services/signal_generation/test_base.py
import pytest
from datetime import datetime, timezone
from services.signal_generation.base import Signal, SignalResult


class DummySignal(Signal):
    name = "dummy"

    def compute(self, data: dict) -> SignalResult:
        return SignalResult(value=0.5, confidence=0.9)


def test_signal_compute_returns_result():
    sig = DummySignal()
    result = sig.compute({"close": [100, 105, 110]})
    assert isinstance(result, SignalResult)
    assert -1.0 <= result.value <= 1.0
    assert 0.0 <= result.confidence <= 1.0


def test_signal_result_clamps_value():
    r = SignalResult(value=1.5, confidence=0.5)
    assert r.value == 1.0  # clamped


def test_signal_has_name():
    sig = DummySignal()
    assert sig.name == "dummy"


def test_signal_is_abstract():
    with pytest.raises(TypeError):
        Signal()
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/services/signal_generation/test_base.py -v`
Expected: FAIL

**Step 3: Write implementation**

```python
# services/signal_generation/base.py
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class SignalResult:
    value: float      # -1.0 to 1.0
    confidence: float  # 0.0 to 1.0

    def __post_init__(self):
        self.value = max(-1.0, min(1.0, self.value))
        self.confidence = max(0.0, min(1.0, self.confidence))


class Signal(ABC):
    name: str = ""

    @abstractmethod
    def compute(self, data: dict[str, Any]) -> SignalResult:
        ...
```

**Step 4: Run test to verify it passes**

Run: `python -m pytest tests/services/signal_generation/test_base.py -v`
Expected: 4 PASSED

**Step 5: Commit**

```bash
git add services/signal_generation/base.py tests/services/signal_generation/
git commit -m "feat: signal base class with plugin architecture"
```

---

### Task 12: Technical Signals — Support Levels

**Files:**
- Create: `services/signal_generation/technical.py`
- Test: `tests/services/signal_generation/test_technical.py`

**Step 1: Write the failing test**

```python
# tests/services/signal_generation/test_technical.py
import pytest
import numpy as np
import pandas as pd
from datetime import date, timedelta
from services.signal_generation.technical import (
    SupportProximitySignal,
    SupportStrengthSignal,
    SupportTrendSignal,
    find_support_levels,
)


def make_ohlcv(days=252, base_price=100.0):
    """Generate 1 year of synthetic OHLCV data with a known support level around base_price."""
    dates = [date(2024, 1, 2) + timedelta(days=i) for i in range(days)]
    np.random.seed(42)
    closes = base_price + np.cumsum(np.random.randn(days) * 0.5)
    # Inject a support level: price bounces off 95 several times
    for i in [50, 100, 150, 200]:
        closes[i:i+3] = 95.0
    return {
        "dates": dates,
        "open": closes + np.random.rand(days),
        "high": closes + abs(np.random.randn(days)),
        "low": closes - abs(np.random.randn(days)),
        "close": closes,
        "volume": np.random.randint(100000, 1000000, days),
    }


def test_find_support_levels_detects_bounces():
    data = make_ohlcv()
    levels = find_support_levels(data, lookback_days=252)
    assert len(levels) > 0
    # Should find something near 95
    assert any(abs(level - 95.0) < 3.0 for level in levels)


def test_support_proximity_signal():
    data = make_ohlcv()
    data["close"][-1] = 96.0  # close to support at 95
    sig = SupportProximitySignal()
    result = sig.compute(data)
    assert result.value > 0  # positive = near support = bullish signal


def test_support_strength_signal():
    data = make_ohlcv()
    sig = SupportStrengthSignal()
    result = sig.compute(data)
    assert -1.0 <= result.value <= 1.0


def test_support_trend_signal():
    data = make_ohlcv()
    sig = SupportTrendSignal()
    result = sig.compute(data)
    assert -1.0 <= result.value <= 1.0
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/services/signal_generation/test_technical.py -v`
Expected: FAIL

**Step 3: Write implementation**

Implement `find_support_levels()` using local minima detection on the 1-year price data. Implement the three signal classes that use it. Each returns a normalized value between -1.0 and 1.0.

Key algorithm for `find_support_levels`:
- Find local minima in the low prices (points where price reversed upward)
- Cluster nearby minima into support zones
- Score each zone by number of touches
- Return sorted list of support level prices

**Step 4: Run tests, Step 5: Commit**

```bash
git add services/signal_generation/technical.py tests/services/signal_generation/test_technical.py
git commit -m "feat: technical signals - support proximity, strength, and trend"
```

---

### Task 13: Fundamental Signals

**Files:**
- Create: `services/signal_generation/fundamental.py`
- Test: `tests/services/signal_generation/test_fundamental.py`

Implement three signal classes:
- `ValuationSignal` — P/E, P/B, EV/EBITDA relative to sector median. Returns positive if undervalued.
- `QualitySignal` — ROE, debt/equity, margin trends. Returns positive if high quality.
- `GrowthSignal` — Revenue/earnings growth rate. Returns positive if growing.

Each reads from fundamental data dict with keys like `pe_ratio`, `pb_ratio`, `roe`, `debt_equity`, `revenue_growth`, `earnings_growth`, `sector_median_pe`, etc.

Follow same TDD pattern: test first, fail, implement, pass, commit.

```bash
git commit -m "feat: fundamental signals - valuation, quality, growth"
```

---

### Task 14: Event Signals

**Files:**
- Create: `services/signal_generation/event.py`
- Test: `tests/services/signal_generation/test_event.py`

Implement three signal classes:
- `EarningsSurpriseSignal` — actual vs estimate earnings. Positive if beat.
- `NewsSentimentSignal` — sentiment score from news headlines. Pass-through with normalization.
- `InsiderActivitySignal` — net insider buying/selling. Positive if insiders buying.

Follow same TDD pattern.

```bash
git commit -m "feat: event signals - earnings surprise, news sentiment, insider activity"
```

---

### Task 15: Signal Staleness Detection

**Files:**
- Create: `services/signal_generation/staleness.py`
- Test: `tests/services/signal_generation/test_staleness.py`

**Step 1: Write the failing test**

```python
# tests/services/signal_generation/test_staleness.py
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
from services.signal_generation.staleness import StalenessChecker
from shared.schemas.messages import SignalMessage


def test_fresh_signal_is_not_stale():
    cal = MagicMock()
    cal.get_last_session_close.return_value = datetime(2025, 1, 6, 16, 0, tzinfo=timezone.utc)
    checker = StalenessChecker(calendar=cal, grace_hours=4, fundamentals_days=7, events_hours=48)
    now = datetime(2025, 1, 6, 18, 0, tzinfo=timezone.utc)
    assert checker.is_stale("market_data", datetime(2025, 1, 6, 16, 30, tzinfo=timezone.utc), now) is False


def test_stale_market_data_after_grace_window():
    cal = MagicMock()
    cal.get_last_session_close.return_value = datetime(2025, 1, 6, 16, 0, tzinfo=timezone.utc)
    checker = StalenessChecker(calendar=cal, grace_hours=4, fundamentals_days=7, events_hours=48)
    now = datetime(2025, 1, 7, 12, 0, tzinfo=timezone.utc)
    # Signal from yesterday morning, last session close was yesterday 4pm
    # Now is next day noon — well past the 4-hour grace
    assert checker.is_stale("market_data", datetime(2025, 1, 6, 10, 0, tzinfo=timezone.utc), now) is True


def test_fundamentals_stale_after_7_days():
    cal = MagicMock()
    checker = StalenessChecker(calendar=cal, grace_hours=4, fundamentals_days=7, events_hours=48)
    now = datetime(2025, 1, 15, 12, 0, tzinfo=timezone.utc)
    assert checker.is_stale("fundamentals", datetime(2025, 1, 5, 12, 0, tzinfo=timezone.utc), now) is True


def test_weekend_does_not_trigger_false_positive():
    cal = MagicMock()
    # Friday close
    cal.get_last_session_close.return_value = datetime(2025, 1, 3, 16, 0, tzinfo=timezone.utc)
    checker = StalenessChecker(calendar=cal, grace_hours=4, fundamentals_days=7, events_hours=48)
    # Saturday morning — signal from Friday close should not be stale
    now = datetime(2025, 1, 4, 10, 0, tzinfo=timezone.utc)
    assert checker.is_stale("market_data", datetime(2025, 1, 3, 16, 30, tzinfo=timezone.utc), now) is False
```

**Step 2–5: Implement, test, commit**

```bash
git commit -m "feat: signal staleness detection with calendar-aware thresholds"
```

---

### Task 16: Signal Generation Service Runner

**Files:**
- Create: `services/signal_generation/runner.py`
- Test: `tests/services/signal_generation/test_runner.py`

The runner:
- Subscribes to `stream:market_data`, `stream:fundamentals`, `stream:events` via consumer groups
- For each message, runs the appropriate signal plugins
- Checks staleness before publishing
- Publishes results to `stream:signals`
- Acks messages after processing; sends poison messages to dead-letter
- Exposes `/health`

Follow TDD pattern.

```bash
git commit -m "feat: signal generation service runner with consumer groups"
```

---

## Phase 4: ML Model Service

### Task 17: Feature Assembly

**Files:**
- Create: `services/ml_model/feature_assembly.py`
- Test: `tests/services/ml_model/test_feature_assembly.py`

Collects signals per ticker into feature vectors. Checks staleness (skips incomplete vectors). Stores in PostgreSQL for training data.

```bash
git commit -m "feat: feature assembly with staleness-aware vector construction"
```

---

### Task 18: LightGBM Model Training and Prediction

**Files:**
- Create: `services/ml_model/trainer.py`
- Create: `services/ml_model/predictor.py`
- Test: `tests/services/ml_model/test_trainer.py`
- Test: `tests/services/ml_model/test_predictor.py`

**Step 1: Write the failing test (trainer)**

```python
# tests/services/ml_model/__init__.py (empty)
# tests/services/ml_model/test_trainer.py
import pytest
import numpy as np
import pandas as pd
from services.ml_model.trainer import ModelTrainer


def test_train_produces_model_and_metrics():
    np.random.seed(42)
    n = 500
    features = pd.DataFrame({
        "support_proximity": np.random.randn(n),
        "support_strength": np.random.randn(n),
        "support_trend": np.random.randn(n),
        "valuation": np.random.randn(n),
        "quality": np.random.randn(n),
        "growth": np.random.randn(n),
        "earnings_surprise": np.random.randn(n),
        "news_sentiment": np.random.randn(n),
        "insider_activity": np.random.randn(n),
    })
    # Target: bucketed forward returns
    targets = pd.Series(np.random.choice(["sell", "hold", "buy"], n))

    trainer = ModelTrainer()
    model, metrics = trainer.train(features, targets)
    assert model is not None
    assert "accuracy" in metrics
    assert "feature_importance" in metrics
    assert len(metrics["feature_importance"]) == 9


def test_train_rejects_insufficient_samples():
    trainer = ModelTrainer(min_samples=200)
    features = pd.DataFrame({"a": [1, 2, 3]})
    targets = pd.Series(["buy", "sell", "hold"])
    with pytest.raises(ValueError, match="Insufficient"):
        trainer.train(features, targets)
```

**Step 2–5: Implement using LightGBM multiclass classification, test, commit**

```bash
git commit -m "feat: LightGBM model training with feature importance"
```

---

### Task 19: Regime Detection

**Files:**
- Create: `services/ml_model/regime.py`
- Test: `tests/services/ml_model/test_regime.py`

Detect market regime (bull/bear/sideways) using rolling SPY returns and volatility. Output a regime label and a familiarity score (how similar current regime is to training data). If unfamiliar, trigger caution mode.

```bash
git commit -m "feat: market regime detection with familiarity scoring"
```

---

### Task 20: Model Registry

**Files:**
- Create: `services/ml_model/registry.py`
- Test: `tests/services/ml_model/test_registry.py`

Save/load models with version, metrics, training window. Support rollback to previous version. Uses `ModelVersion` DB model.

```bash
git commit -m "feat: model registry with versioning and rollback"
```

---

### Task 21: ML Service Runner

**Files:**
- Create: `services/ml_model/runner.py`
- Test: `tests/services/ml_model/test_runner.py`

Subscribes to `stream:signals`, assembles features, runs prediction, publishes to `stream:recommendations`. Handles 6-month retrain schedule. Exposes `/health`.

```bash
git commit -m "feat: ML model service runner"
```

---

## Phase 5: Risk Management Service

### Task 22: Risk Engine — Entry Controls

**Files:**
- Create: `services/risk_management/engine.py`
- Test: `tests/services/risk_management/test_engine.py`

**Step 1: Write the failing test**

```python
# tests/services/risk_management/__init__.py (empty)
# tests/services/risk_management/test_engine.py
import pytest
from services.risk_management.engine import RiskEngine, RiskDecision, PortfolioState


def make_portfolio(nav=100_000, positions=None):
    return PortfolioState(
        nav=nav,
        peak_nav=nav,
        positions=positions or {},
        sector_exposure={},
        total_exposure_pct=0.0,
        margin_utilization_pct=0.0,
    )


def test_position_entry_limit_scales_down():
    engine = RiskEngine(position_entry_limit_pct=5.0, sector_concentration_pct=20.0, total_exposure_limit_pct=150.0)
    portfolio = make_portfolio(nav=100_000)
    decision = engine.check_entry("AAPL", quantity=100, price=100.0, sector="Technology", portfolio=portfolio)
    # 100 shares * $100 = $10,000 = 10% of NAV, exceeds 5% limit
    assert decision.approved is True
    assert decision.adjusted_quantity < 100  # scaled down to 5% = $5000 = 50 shares


def test_sector_concentration_rejects():
    engine = RiskEngine(position_entry_limit_pct=5.0, sector_concentration_pct=20.0, total_exposure_limit_pct=150.0)
    portfolio = make_portfolio(nav=100_000, positions={})
    portfolio.sector_exposure = {"Technology": 20.0}  # already at 20%
    decision = engine.check_entry("MSFT", quantity=10, price=100.0, sector="Technology", portfolio=portfolio)
    assert decision.approved is False
    assert "sector" in decision.reason.lower()


def test_total_exposure_rejects():
    engine = RiskEngine(position_entry_limit_pct=5.0, sector_concentration_pct=20.0, total_exposure_limit_pct=150.0)
    portfolio = make_portfolio(nav=100_000)
    portfolio.total_exposure_pct = 150.0  # at limit
    decision = engine.check_entry("AAPL", quantity=10, price=100.0, sector="Technology", portfolio=portfolio)
    assert decision.approved is False
    assert "exposure" in decision.reason.lower()
```

**Step 2–5: Implement, test, commit**

```bash
git commit -m "feat: risk engine entry controls - position limit, sector, exposure"
```

---

### Task 23: Risk Engine — Stop-Loss and Drawdown

**Files:**
- Modify: `services/risk_management/engine.py`
- Test: `tests/services/risk_management/test_stop_loss.py`

Test and implement:
- Trailing stop-loss: triggers when price drops X% from highest price since entry
- Portfolio drawdown pause: 10% from peak NAV pauses new buys
- Circuit breaker: 20% from peak NAV triggers full liquidation
- Decision precedence: kill/circuit > margin > stop-loss > hard compliance > soft advisory

```bash
git commit -m "feat: stop-loss, drawdown, circuit breaker with decision precedence"
```

---

### Task 24: Risk Engine — Kill Switch

**Files:**
- Create: `services/risk_management/kill_switch.py`
- Test: `tests/services/risk_management/test_kill_switch.py`

Subscribes to `stream:kill`. When triggered: cancel all open orders, emit market sell for all positions, halt all further trading. Logged in audit trail.

```bash
git commit -m "feat: kill switch with immediate position liquidation"
```

---

### Task 25: Passive Breach Monitoring

**Files:**
- Create: `services/risk_management/passive_monitor.py`
- Test: `tests/services/risk_management/test_passive_monitor.py`

Runs every 30 min during market hours:
- Soft ceiling (7% NAV): notify only
- Hard ceiling (15% NAV): auto-trim to 7% via market order
- Margin utilization warning (70%) and critical (85%)
- All thresholds based on NAV, not gross

```bash
git commit -m "feat: passive breach monitoring with soft/hard ceiling enforcement"
```

---

### Task 26: Correlation Risk Monitor

**Files:**
- Create: `services/risk_management/correlation.py`
- Test: `tests/services/risk_management/test_correlation.py`

Advisory only. Computes portfolio beta vs SPY and pairwise position correlations. Requires 60-day minimum lookback. Publishes alerts when thresholds exceeded.

```bash
git commit -m "feat: correlation risk monitoring - portfolio beta and pairwise correlation"
```

---

### Task 27: Risk Service Runner

**Files:**
- Create: `services/risk_management/runner.py`
- Test: `tests/services/risk_management/test_runner.py`

Subscribes to `stream:recommendations` and `stream:kill`. Runs entry checks synchronously. Runs passive monitoring on timer. Publishes to `stream:approved_orders` and `stream:alerts`. Also monitors prices independently for stop-loss and circuit breaker (not only via recommendation flow — addendum #1). Exposes `/health`.

```bash
git commit -m "feat: risk management service runner with independent price monitoring"
```

---

## Phase 6: Execution Service

### Task 28: IB Order Manager

**Files:**
- Create: `services/execution/order_manager.py`
- Create: `services/execution/ib_executor.py`
- Test: `tests/services/execution/test_order_manager.py`

**Step 1: Write the failing test**

```python
# tests/services/execution/__init__.py (empty)
# tests/services/execution/test_order_manager.py
import pytest
from unittest.mock import AsyncMock, MagicMock
from services.execution.order_manager import OrderManager


class TestOrderManager:
    @pytest.mark.asyncio
    async def test_submit_limit_entry(self):
        mock_executor = AsyncMock()
        mock_executor.submit_limit_order = AsyncMock(return_value="order-001")
        mgr = OrderManager(executor=mock_executor, redis_client=AsyncMock(), db_session=MagicMock())
        order_id = await mgr.submit_entry("AAPL", quantity=50, limit_price=150.75, recommendation_id="rec-001")
        assert order_id == "order-001"
        mock_executor.submit_limit_order.assert_called_once()

    @pytest.mark.asyncio
    async def test_submit_market_exit(self):
        mock_executor = AsyncMock()
        mock_executor.submit_market_order = AsyncMock(return_value="order-002")
        mgr = OrderManager(executor=mock_executor, redis_client=AsyncMock(), db_session=MagicMock())
        order_id = await mgr.submit_exit("AAPL", quantity=50, recommendation_id="rec-001")
        assert order_id == "order-002"
        mock_executor.submit_market_order.assert_called_once()

    @pytest.mark.asyncio
    async def test_idempotency_prevents_duplicate(self):
        mock_executor = AsyncMock()
        mock_executor.submit_limit_order = AsyncMock(return_value="order-001")
        mgr = OrderManager(executor=mock_executor, redis_client=AsyncMock(), db_session=MagicMock())
        await mgr.submit_entry("AAPL", quantity=50, limit_price=150.0, recommendation_id="rec-001")
        await mgr.submit_entry("AAPL", quantity=50, limit_price=150.0, recommendation_id="rec-001")
        # Should only submit once
        assert mock_executor.submit_limit_order.call_count == 1
```

**Step 2–5: Implement, test, commit**

The `IBExecutor` wraps `ib_insync` for order submission. The `OrderManager` handles idempotency, entry/exit logic, and fill reconciliation.

```bash
git commit -m "feat: order manager with IB executor and idempotency"
```

---

### Task 29: Unfilled Order Handling

**Files:**
- Modify: `services/execution/order_manager.py`
- Test: `tests/services/execution/test_unfilled_orders.py`

Re-price after 1 hour, cancel at market close (using `MarketCalendar`), max 3 re-price attempts.

```bash
git commit -m "feat: unfilled order handling with re-price and EOD cancel"
```

---

### Task 30: Partial Fill Handling

**Files:**
- Modify: `services/execution/order_manager.py`
- Test: `tests/services/execution/test_partial_fills.py`

Minimum viable fill threshold (40%). Below minimum: keep as undersized position for operator review unless hard risk rule requires close.

```bash
git commit -m "feat: partial fill handling with minimum viable position size"
```

---

### Task 31: IB Position Reconciliation

**Files:**
- Create: `services/execution/reconciliation.py`
- Test: `tests/services/execution/test_reconciliation.py`

At market open and close, compare IB positions to portfolio DB. Auto-correct minor discrepancies, alert + halt on major ones.

```bash
git commit -m "feat: IB position reconciliation at market open/close"
```

---

### Task 32: Execution Service Runner

**Files:**
- Create: `services/execution/runner.py`
- Test: `tests/services/execution/test_runner.py`

Subscribes to `stream:approved_orders` and `stream:kill`. Manages IB connection with auto-reconnect. Publishes to `stream:fills` and `stream:alerts`. Handles graceful shutdown (no orphaned orders). Exposes `/health`.

Paper mode toggle via config (`ib.paper_port` vs `ib.live_port`).

```bash
git commit -m "feat: execution service runner with IB connection management"
```

---

## Phase 7: API Service

### Task 33: FastAPI Setup with RBAC

**Files:**
- Create: `services/api/app.py`
- Create: `services/api/auth.py`
- Test: `tests/services/api/test_auth.py`

**Step 1: Write the failing test**

```python
# tests/services/api/__init__.py (empty)
# tests/services/api/test_auth.py
import pytest
from fastapi.testclient import TestClient
from services.api.app import create_app


def test_unauthenticated_request_returns_401():
    app = create_app()
    client = TestClient(app)
    response = client.get("/api/v1/portfolio")
    assert response.status_code == 401


def test_authenticated_request_succeeds():
    app = create_app()
    client = TestClient(app)
    response = client.get("/api/v1/portfolio", headers={"X-API-Key": "test-key"})
    assert response.status_code in (200, 404)  # not 401


def test_kill_switch_requires_admin_role():
    app = create_app()
    client = TestClient(app)
    # viewer role cannot trigger kill switch
    response = client.post("/api/v1/kill", headers={"X-API-Key": "viewer-key"})
    assert response.status_code == 403
```

**Step 2–5: Implement FastAPI app with API key auth and role-based access, test, commit**

```bash
git commit -m "feat: FastAPI app with RBAC authentication"
```

---

### Task 34: API Endpoints

**Files:**
- Create: `services/api/routes/portfolio.py`
- Create: `services/api/routes/positions.py`
- Create: `services/api/routes/risk.py`
- Create: `services/api/routes/activity.py`
- Create: `services/api/routes/kill.py`
- Create: `services/api/routes/ml.py`
- Create: `services/api/routes/backtest.py`
- Test: `tests/services/api/test_routes.py`

Each route module implements the endpoints described in the design doc. All read from PostgreSQL. Kill switch publishes to `stream:kill`.

```bash
git commit -m "feat: API endpoints for portfolio, risk, activity, kill switch, ML, backtest"
```

---

## Phase 8: Notifications Service

### Task 35: Notification Dispatcher

**Files:**
- Create: `services/notifications/dispatcher.py`
- Create: `services/notifications/channels.py`
- Test: `tests/services/notifications/test_dispatcher.py`

**Step 1: Write the failing test**

```python
# tests/services/notifications/__init__.py (empty)
# tests/services/notifications/test_dispatcher.py
import pytest
from unittest.mock import AsyncMock
from shared.schemas.messages import AlertMessage
from datetime import datetime, timezone
from services.notifications.dispatcher import NotificationDispatcher


class TestNotificationDispatcher:
    @pytest.mark.asyncio
    async def test_critical_alert_sends_to_all_channels(self):
        slack = AsyncMock()
        email = AsyncMock()
        sms = AsyncMock()
        dispatcher = NotificationDispatcher(slack=slack, email=email, sms=sms)
        alert = AlertMessage(
            timestamp=datetime.now(timezone.utc),
            event_type="circuit_breaker",
            priority="critical",
            message="Portfolio drawdown exceeded 20%",
        )
        await dispatcher.dispatch(alert)
        slack.send.assert_called_once()
        email.send.assert_called_once()
        sms.send.assert_called_once()

    @pytest.mark.asyncio
    async def test_low_priority_only_sends_slack(self):
        slack = AsyncMock()
        email = AsyncMock()
        sms = AsyncMock()
        dispatcher = NotificationDispatcher(slack=slack, email=email, sms=sms)
        alert = AlertMessage(
            timestamp=datetime.now(timezone.utc),
            event_type="trade_executed",
            priority="low",
            message="Bought 50 AAPL",
        )
        await dispatcher.dispatch(alert)
        slack.send.assert_called_once()
        email.send.assert_not_called()
        sms.send.assert_not_called()
```

**Step 2–5: Implement priority-based routing per design doc table, test, commit**

```bash
git commit -m "feat: notification dispatcher with priority-based channel routing"
```

---

### Task 36: Notifications Service Runner

**Files:**
- Create: `services/notifications/runner.py`
- Test: `tests/services/notifications/test_runner.py`

Subscribes to `stream:alerts`. Routes to appropriate channels based on priority. Exposes `/health`.

```bash
git commit -m "feat: notifications service runner"
```

---

## Phase 9: Backtesting Engine

### Task 37: Simulated Execution

**Files:**
- Create: `backtest/simulator.py`
- Test: `tests/backtest/test_simulator.py`

**Step 1: Write the failing test**

```python
# tests/backtest/__init__.py (empty)
# tests/backtest/test_simulator.py
import pytest
from datetime import date
from backtest.simulator import SimulatedExecutor


def test_limit_entry_fills_when_low_below_price():
    executor = SimulatedExecutor(slippage_pct=0.1, commission_per_share=0.005)
    bar = {"date": date(2025, 1, 6), "open": 150.0, "high": 155.0, "low": 148.0, "close": 153.0}
    fill = executor.try_fill_limit_entry(limit_price=149.0, quantity=100, bar=bar)
    assert fill is not None
    assert fill["filled"] is True
    assert fill["fill_price"] == pytest.approx(149.0 * 1.001)  # with slippage


def test_limit_entry_does_not_fill_when_low_above_price():
    executor = SimulatedExecutor(slippage_pct=0.1, commission_per_share=0.005)
    bar = {"date": date(2025, 1, 6), "open": 150.0, "high": 155.0, "low": 151.0, "close": 153.0}
    fill = executor.try_fill_limit_entry(limit_price=149.0, quantity=100, bar=bar)
    assert fill is None


def test_market_exit_fills_at_next_open():
    executor = SimulatedExecutor(slippage_pct=0.1, commission_per_share=0.005)
    bar = {"date": date(2025, 1, 7), "open": 152.0, "high": 155.0, "low": 150.0, "close": 153.0}
    fill = executor.fill_market_exit(quantity=100, bar=bar)
    assert fill["filled"] is True
    assert fill["fill_price"] == pytest.approx(152.0 * 0.999)  # slippage on sell


def test_commission_calculated():
    executor = SimulatedExecutor(slippage_pct=0.0, commission_per_share=0.005)
    bar = {"date": date(2025, 1, 6), "open": 150.0, "high": 155.0, "low": 148.0, "close": 153.0}
    fill = executor.try_fill_limit_entry(limit_price=149.0, quantity=100, bar=bar)
    assert fill["commission"] == pytest.approx(0.50)  # 100 * 0.005
```

**Step 2–5: Implement, test, commit**

```bash
git commit -m "feat: simulated execution with slippage, commission, and margin interest"
```

---

### Task 38: Backtest Runner

**Files:**
- Create: `backtest/runner.py`
- Create: `backtest/metrics.py`
- Test: `tests/backtest/test_runner.py`
- Test: `tests/backtest/test_metrics.py`

The runner:
- Loads 10 years of historical data from PostgreSQL
- Replays day-by-day through the pipeline: data → signals → ML → risk → simulated execution
- Uses the same service code with `MODE=backtest`
- Implements walk-forward: train on N months, test on next 6 months, slide forward
- Applies order submission lag assumptions
- Tracks portfolio state, positions, trades throughout

The metrics module computes:
- Total return vs SPY
- Sharpe ratio
- Max drawdown
- Win rate
- Avg holding period
- Margin utilization over time
- Per-signal feature importance

```bash
git commit -m "feat: backtest runner with walk-forward validation and metrics"
```

---

## Phase 10: Infrastructure

### Task 39: Dockerfiles

**Files:**
- Create: `Dockerfile` (shared base)
- Create: `services/data_ingestion/Dockerfile`
- Create: `services/signal_generation/Dockerfile`
- Create: `services/ml_model/Dockerfile`
- Create: `services/risk_management/Dockerfile`
- Create: `services/execution/Dockerfile`
- Create: `services/api/Dockerfile`
- Create: `services/notifications/Dockerfile`

Each service Dockerfile:
- Uses Python 3.12 slim base
- Installs dependencies from `pyproject.toml`
- Copies shared/ and the specific service code
- Sets entrypoint to `python -m services.<service_name>.runner`

```bash
git commit -m "feat: Dockerfiles for all services"
```

---

### Task 40: Docker Compose

**Files:**
- Create: `docker-compose.yml`

**Step 1: Write docker-compose.yml**

```yaml
services:
  postgres:
    image: postgres:16
    environment:
      POSTGRES_DB: algo_poc
      POSTGRES_USER: algo
      POSTGRES_PASSWORD: algo
    ports:
      - "5432:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U algo"]
      interval: 5s
      retries: 5

  redis:
    image: redis:7
    ports:
      - "6379:6379"
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      retries: 5

  data-ingestion:
    build:
      context: .
      dockerfile: services/data_ingestion/Dockerfile
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
    environment:
      - ALGO_MODE=paper
      - ALGO_DATABASE_URL=postgresql://algo:algo@postgres:5432/algo_poc
      - ALGO_REDIS_URL=redis://redis:6379/0

  signal-generation:
    build:
      context: .
      dockerfile: services/signal_generation/Dockerfile
    depends_on:
      data-ingestion:
        condition: service_healthy
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy

  ml-model:
    build:
      context: .
      dockerfile: services/ml_model/Dockerfile
    depends_on:
      signal-generation:
        condition: service_healthy

  risk-management:
    build:
      context: .
      dockerfile: services/risk_management/Dockerfile
    depends_on:
      ml-model:
        condition: service_healthy

  execution:
    build:
      context: .
      dockerfile: services/execution/Dockerfile
    depends_on:
      risk-management:
        condition: service_healthy

  api:
    build:
      context: .
      dockerfile: services/api/Dockerfile
    ports:
      - "8000:8000"
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy

  notifications:
    build:
      context: .
      dockerfile: services/notifications/Dockerfile
    depends_on:
      redis:
        condition: service_healthy

volumes:
  pgdata:
```

**Step 2: Test**

Run: `docker compose config` (validates YAML)
Run: `docker compose build` (builds all images)

**Step 3: Commit**

```bash
git add docker-compose.yml
git commit -m "feat: Docker Compose with health checks and startup ordering"
```

---

### Task 41: Observability Setup

**Files:**
- Create: `shared/observability.py`
- Create: `docker-compose.observability.yml` (Prometheus + Grafana sidecar)
- Test: `tests/shared/test_observability.py`

Each service exposes Prometheus metrics. OpenTelemetry tracing wired through all services. Trace IDs propagated via Redis stream message metadata.

```bash
git commit -m "feat: observability with Prometheus metrics and OpenTelemetry tracing"
```

---

### Task 42: CLAUDE.md

**Files:**
- Create: `CLAUDE.md`

After all services are built, create `CLAUDE.md` documenting:
- Build commands: `pip install -e ".[dev]"`, `docker compose up`, `docker compose build`
- Test commands: `pytest`, `pytest tests/services/risk_management/ -v`, `pytest tests/backtest/ -v`
- Lint: configured linter commands
- Architecture: brief pointer to design doc, service map
- Config: how to edit `config/default.yaml`, environment variable overrides

```bash
git add CLAUDE.md
git commit -m "docs: add CLAUDE.md for development guidance"
```

---

## Phase 11: Go-Live Readiness

### Task 43: Paper-to-Live Promotion Gates and Rollback Playbook

**Files:**
- Create: `docs/operations/go-live-checklist.md`
- Create: `docs/operations/rollback-playbook.md`
- Create: `scripts/ops/go_live_gate.py`
- Test: `tests/operations/test_go_live_gate.py`

Define objective gates for promotion from paper to live trading. Live mode is blocked unless all gates pass.

**Gate policy (all required):**
- **Paper trading duration:** minimum 60 calendar days in paper mode.
- **Risk stability:** no circuit-breaker events in the last 30 days.
- **Drawdown bound:** paper max drawdown <= configured threshold (default 12%).
- **Execution quality:** median slippage within tolerance (default <= 20 bps) and failed-order rate <= 1%.
- **Reliability:** no unresolved critical alerts for Redis/PostgreSQL/IB connectivity in the last 14 days.
- **Data integrity:** latest reconciliation checks pass with no unresolved major discrepancies.
- **Model governance:** current model version approved and not in rollback/caution state.
- **Backtest regression:** latest backtest run with current model passes all metric thresholds (Sharpe, drawdown, win rate within tolerance of baseline).

**Promotion workflow:**
1. Run `scripts/ops/go_live_gate.py --from paper --to live`.
2. Script validates metrics from PostgreSQL/audit logs and writes a signed report to `docs/operations/reports/`.
3. Require two-person approval recorded in `go-live-checklist.md` (operator + reviewer).
4. Apply `ALGO_MODE=live` only after checklist sign-off.

**Rollback triggers (any trigger causes immediate rollback to paper):**
- Kill switch activation or circuit-breaker event in live mode.
- IB reconciliation major discrepancy not resolved within SLA.
- Critical observability outage impacting execution/risk services.
- Slippage/fill quality breach sustained for configurable window (e.g., 3 consecutive sessions).

**Rollback procedure (time-bound):**
1. Set trading state to `HALTED`; publish kill event.
2. Switch mode to paper (`ALGO_MODE=paper`) and redeploy affected services.
3. Verify execution service is disconnected from live IB port and connected to paper port.
4. Run reconciliation and incident triage; open incident report.
5. Resume paper trading only after incident action items are documented.

```bash
git commit -m "docs: add go-live promotion gates and rollback playbook"
```

---

## Execution Order Summary

| Phase | Tasks | Est. Commits |
|-------|-------|-------------|
| 1. Foundation | Tasks 1–7 | 7 |
| 2. Data Ingestion | Tasks 8–10 | 3 |
| 3. Signal Generation | Tasks 11–16 | 6 |
| 4. ML Model | Tasks 17–21 | 5 |
| 5. Risk Management | Tasks 22–27 | 6 |
| 6. Execution | Tasks 28–32 | 5 |
| 7. API | Tasks 33–34 | 2 |
| 8. Notifications | Tasks 35–36 | 2 |
| 9. Backtesting | Tasks 37–38 | 2 |
| 10. Infrastructure | Tasks 39–42 | 4 |
| 11. Go-Live Readiness | Task 43 | 1 |
| **Total** | **43 tasks** | **43 commits** |

Each task follows TDD: write failing test → verify failure → implement → verify pass → commit.
