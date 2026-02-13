# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Build Commands

```bash
# Install project in editable mode with dev dependencies
pip install -e ".[dev]"

# Build and start all services via Docker
docker compose up

# Rebuild Docker images after code changes
docker compose build

# Start with observability stack (Prometheus + Grafana)
docker compose -f docker-compose.yml -f docker-compose.observability.yml up
```

## Test Commands

```bash
# Run the full test suite
pytest

# Run tests for a specific service
pytest tests/services/risk_management/ -v
pytest tests/services/data_ingestion/ -v

# Run backtest tests
pytest tests/backtest/ -v

# Run shared module tests
pytest tests/shared/ -v

# Run with coverage
pytest --cov=shared --cov=services
```

## Architecture

algo-poc is an automated US equities trading bot built as a set of Python microservices connected by Redis Streams.

### Data flow

```
data_ingestion -> signal_generation -> ml_model -> risk_management -> execution
                                                                         |
                                                                    notifications
                                                                         |
                                                                        api
```

### Infrastructure

- **PostgreSQL 16** — persistent storage for market data, positions, orders, ML models
- **Redis 7** — message bus (Redis Streams) for inter-service communication
- **Alembic** — database schema migrations (run via `alembic upgrade head`)

### Services

| Service | Description | Subscribes | Publishes |
|---|---|---|---|
| `data_ingestion` | Fetches market data, fundamentals, and events from IB/external sources | — | `stream:market_data`, `stream:fundamentals`, `stream:events` |
| `signal_generation` | Computes technical, fundamental, and event signals; detects staleness | `stream:market_data`, `stream:fundamentals`, `stream:events` | `stream:signals` |
| `ml_model` | Assembles features, trains LightGBM model, generates buy/hold/sell recommendations | `stream:signals` | `stream:recommendations` |
| `risk_management` | Entry controls, stop-loss, drawdown, kill switch, correlation monitoring | `stream:recommendations`, `stream:kill` | `stream:approved_orders`, `stream:alerts` |
| `execution` | Manages IB orders, handles fills, repricing, and kill liquidation | `stream:approved_orders`, `stream:kill` | `stream:fills`, `stream:alerts` |
| `notifications` | Routes alerts to Slack, email, and SMS channels by priority | `stream:alerts` | — |
| `api` | FastAPI REST API for monitoring, control, and backtest triggering | — (reads DB directly) | `stream:kill` (via kill endpoint) |

### Shared modules (`shared/`)

- `config.py` — YAML + env-var configuration loading
- `models/` — SQLAlchemy ORM models
- `schemas/` — Pydantic schemas for stream messages and API payloads
- `redis_client.py` — Redis Streams client with consumer groups and dead-letter queues
- `logging.py` — Structured JSON logging via structlog
- `market_calendar.py` — NYSE trading calendar helpers
- `observability.py` — Prometheus metrics helpers (counters, histograms, gauges)

## Configuration

### Config file

Edit `config/default.yaml` to change default settings. Key sections:

- `mode` — `paper`, `live`, or `backtest`
- `universe` — watchlist source and custom tickers
- `data_ingestion` — polling intervals, rate limits, backfill years
- `signals` — staleness thresholds
- `ml_model` — retraining cadence, target buckets, regime detection
- `risk` — position limits, stop-loss, drawdown thresholds, margin alerts
- `execution` — limit order buffers, reprice settings
- `ib` — Interactive Brokers connection settings
- `notifications` — channel enable/disable flags
- `database` / `redis` — connection URLs
- `observability` — Prometheus port, tracing toggle

### Environment variable overrides

Environment variables take precedence over `config/default.yaml`:

| Variable | Config path | Example |
|---|---|---|
| `ALGO_MODE` | `mode` | `paper`, `live`, `backtest` |
| `ALGO_DATABASE_URL` | `database.url` | `postgresql://algo:algo@localhost:5432/algo_poc` |
| `ALGO_REDIS_URL` | `redis.url` | `redis://localhost:6379/0` |

## Code Conventions

- All modules use `from __future__ import annotations`
- Tests use pytest with `asyncio_mode = "auto"`
- Services are structured as `services/<name>/runner.py` with a main runner class
- Stream message schemas live in `shared/schemas/messages.py`
- Each service Dockerfile builds from Python 3.12-slim and sets `ENTRYPOINT ["python", "-m", "services.<name>.runner"]`
