# Automated Trading Bot — Design Document

**Date:** 2026-02-13
**Status:** Approved
**Project:** algo-poc

## Overview

An automated trading bot for US Equities that encodes investment know-how as features/signals and uses ML to learn optimal combinations. The system supports the full pipeline from backtesting against 10 years of historical data through to live margin trading via Interactive Brokers.

**Key characteristics:**
- Position trading (weeks to months holding period)
- Three signal families: technical (support levels), fundamental, event-driven
- ML-assisted signal combination (LightGBM)
- Microservices architecture with Redis Streams message bus
- Margin trading up to 150% of NAV
- Full risk framework with kill switch

## Architecture

### System Diagram

```
┌─────────────────┐   ┌─────────────────┐   ┌─────────────────┐
│ Data Ingestion   │   │ Signal Generation│   │ ML Model        │
│                  │──▶│                  │──▶│                 │
│ - Market data    │   │ - Technical      │   │ - Feature       │
│ - Fundamentals   │   │ - Fundamental    │   │   assembly      │
│ - Events/News    │   │ - Event          │   │ - Prediction    │
└────────┬─────────┘   └──────────────────┘   └────────┬────────┘
         │                                             │
         ▼                                             ▼
┌──────────────────────────────────────────────────────────────────┐
│                   Message Bus (Redis Streams)                    │
└────────────┬──────────────────┬──────────────┬──────────────────┘
             │                  │              │
             ▼                  ▼              ▼
      ┌─────────────┐   ┌─────────────┐  ┌──────────────┐
      │ Risk Mgmt    │   │ API (FastAPI)│  │ Notifications│
      │              │──▶│             │  │              │
      │ - Pos limits │   │ - REST API  │  │ - Email      │
      │ - Stops      │   │ - Kill switch│ │ - Slack      │
      │ - Drawdown   │   │ - Monitoring│  │ - SMS        │
      │ - Kill switch│   └─────────────┘  └──────────────┘
      └──────┬───────┘
             │
             ▼
      ┌─────────────┐
      │ Execution    │
      │              │
      │ - IB Gateway │
      │ - Order mgmt │
      │ - Paper mode │
      └──────┬───────┘
             │
             ▼
      ┌─────────────┐
      │ PostgreSQL   │
      │              │
      │ - Positions  │
      │ - Trades     │
      │ - Historical │
      └─────────────┘
```

### Services

| # | Service | Responsibility |
|---|---------|---------------|
| 1 | Data Ingestion | Pull market data, fundamentals, events; publish to bus |
| 2 | Signal Generation | Compute technical/fundamental/event signals from raw data |
| 3 | ML Model | Assemble features, run predictions, emit trade recommendations |
| 4 | Risk Management | Gate every recommendation against risk rules before execution |
| 5 | Execution | Send orders to IB, manage order lifecycle |
| 6 | API | FastAPI REST backend for monitoring and kill switch |
| 7 | Notifications | Subscribe to alerts, dispatch to email/Slack/SMS |

### Message Bus

**Redis Streams** — chosen over RabbitMQ/Kafka for simplicity at this scale.

| Stream | Publisher | Consumer(s) |
|--------|-----------|-------------|
| `stream:market_data` | Data Ingestion | Signal Generation |
| `stream:fundamentals` | Data Ingestion | Signal Generation |
| `stream:events` | Data Ingestion | Signal Generation |
| `stream:signals` | Signal Generation | ML Model |
| `stream:recommendations` | ML Model | Risk Management |
| `stream:approved_orders` | Risk Management | Execution |
| `stream:fills` | Execution | Risk Management, API |
| `stream:alerts` | Risk Management, Execution | Notifications, API |
| `stream:kill` | API, Risk Management | Execution |

---

## Service Designs

### 1. Data Ingestion Service

**Three data pipelines:**

| Pipeline | Source | Frequency |
|----------|--------|-----------|
| Market Data | IB TWS API (`ib_insync`) | Daily bars at market close + on-demand historical |
| Fundamentals | IB fundamental data + SEC EDGAR | Weekly refresh + on earnings events |
| Events/News | News API (e.g., Benzinga, Alpha Vantage) | Polling every 15-30 min during market hours |

**Design decisions:**
- **Universe management** — configurable watchlist in YAML (starting with S&P 500)
- **Data normalization** — canonical schema: `(ticker, timestamp, payload_type, data)` for all sources
- **Historical backfill** — CLI command to backfill 10 years of data into PostgreSQL for backtesting
- **Rate limiting** — IB's 50 msg/sec limit managed internally with request pacing
- **Storage** — PostgreSQL for historical data; Redis for real-time streams with TTL-based caching

### 2. Signal Generation Service

**Three signal families, each as a pluggable module:**

#### Technical Signals (1-Year Support Levels)

| Signal | Description | Inputs |
|--------|-------------|--------|
| Support proximity | How close current price is to 1-year support levels | 1yr OHLCV |
| Support strength | Number of times a support level has been tested and held over past year | 1yr OHLCV |
| Support trend | Whether support levels are rising, flat, or declining over past year | 1yr OHLCV |

Note: Charts visualize 5 years of price history with 1-year support levels overlaid. Support levels are computed from the trailing 1-year window only.

#### Fundamental Signals

| Signal | Description | Inputs |
|--------|-------------|--------|
| Valuation | P/E, P/B, EV/EBITDA relative to sector | Fundamentals |
| Quality | ROE, debt/equity, margin trends | Fundamentals |
| Growth | Revenue/earnings growth rate, guidance vs estimates | Fundamentals |

#### Event Signals

| Signal | Description | Inputs |
|--------|-------------|--------|
| Earnings surprise | Actual vs estimate, magnitude + direction | Events + Fundamentals |
| News sentiment | NLP-based sentiment score from headlines | Events |
| Insider activity | Insider buy/sell clusters | Events |

**Design decisions:**
- **Plugin architecture** — each signal implements a `Signal` base class with `compute(data) -> SignalResult`
- **Output normalization** — all signals emit values on a common scale (-1.0 to +1.0 or z-scores)
- **Published to** `stream:signals` as `(ticker, timestamp, signal_name, signal_value, confidence)`
- **Stateless** — reads from data streams/DB, computes, publishes

### 3. ML Model Service

**Feature assembly:**
- Consumes from `stream:signals`, collects all signal values per ticker into a feature vector
- Feature vector: `(ticker, timestamp, support_proximity, support_strength, support_trend, valuation, quality, growth, earnings_surprise, news_sentiment, insider_activity)`
- Training data accumulated in PostgreSQL

**Model specification:**

| Aspect | Choice | Rationale |
|--------|--------|-----------|
| Model type | LightGBM (Gradient Boosted Trees) | Handles mixed features, interpretable feature importance, fast training |
| Target variable | Forward N-week return bucketed: sell / hold / buy | Matches position trading horizon |
| Training cadence | 6-month rolling window retrain | Avoids overfitting — accumulates meaningful sample of completed trades between retrains |
| Validation | Walk-forward (no lookahead bias) | Honest backtest results |

**Output:** Publishes to `stream:recommendations` as `(ticker, timestamp, action, confidence, top_features)` where `top_features` explains which signals drove the recommendation.

**Additional features:**
- **Regime detection** — detects market regime (bull/bear/sideways); triggers caution mode in unfamiliar regimes between retrains
- **Model registry** — trained models versioned with metrics and training window metadata; supports rollback

### 4. Risk Management Service

**All position sizing and thresholds are based on NAV (Net Asset Value), not gross exposure.**

#### Risk Controls (evaluated in order on every recommendation)

| Control | Threshold | Action on breach |
|---------|-----------|-----------------|
| Position entry limit | Max 5% of NAV per position | Scale down order to max allowed |
| Sector concentration | Max 20% of NAV per sector | Reject order |
| Total exposure | Max 150% of NAV (margin allowed) | Reject order |
| Stop-loss | Per-position trailing stop (configurable %) | Emit market sell order |
| Portfolio drawdown | 10% from peak NAV | Pause all new buys; keep existing stops active |
| Circuit breaker | 20% from peak NAV | Liquidate all positions, halt trading |
| Kill switch | Manual trigger (CLI or API) | Cancel all open orders, close all positions, halt |

#### Passive Breach Monitoring (periodic scan every 30 min during market hours)

| Control | Threshold | Action |
|---------|-----------|--------|
| Soft ceiling | Position drifts above 7% of NAV | **Notify only** (no auto-trade) |
| Hard ceiling | Position drifts above 15% of NAV | Auto-trim to 7% of NAV via market order |

#### Margin-Specific Controls

| Control | Threshold | Action |
|---------|-----------|--------|
| Margin utilization warning | 70% of IB maintenance margin | Alert, pause new leveraged entries |
| Margin utilization critical | 85% of IB maintenance margin | Auto-trim most leveraged positions |

**Design decisions:**
- **Risk checks are synchronous** — recommendation evaluated against all rules before forwarding to execution
- **All thresholds configurable** in YAML
- **Audit log** — every decision (pass, adjust, reject, halt) logged with full context to PostgreSQL
- **Double-down support** — when model confidence is high + support strength is strong, position can enter at up to 10% of NAV initial allocation

### 5. Execution Service

**Connection:** `ib_insync` to IB TWS or IB Gateway. Persistent connection during market hours with auto-reconnect. Paper mode via IB's paper account (same API, different port).

#### Order Types

| Scenario | Order Type | Price Logic |
|----------|-----------|-------------|
| Standard entry | Limit order | Ask + buffer (0.1-0.5%, scaled by spread/volume) |
| Double-down entry | Limit order | Ask + wider buffer (0.5-1.0%) |
| All exits (signal, stop, trim, circuit breaker, kill) | Market order | Best available |

#### Unfilled Entry Handling
- Re-price to current market + buffer after 1 hour
- Cancel if unfilled by end of day
- Max 3 re-price attempts per order before cancelling

**Design decisions:**
- **Market orders for all exits** — certainty of execution over price optimization
- **Idempotency** — each recommendation has a unique ID to prevent double-orders from retries
- **Fill reconciliation** — after each fill, update portfolio DB and publish to `stream:fills`
- **Graceful shutdown** — flush pending work on SIGTERM, no orphaned orders

### 6. API Service (FastAPI)

**REST API** exposing all system data for monitoring. No frontend — API-only for now.

| Endpoint Area | Description |
|--------------|-------------|
| Portfolio | Current positions, NAV, exposure %, margin utilization, PnL |
| Positions | Per-stock detail: entry price, current price, holding period, signal attribution |
| Charts | 5-year price history with 1-year support levels overlaid |
| ML | Model version, feature importance, confidence distribution |
| Risk | All thresholds with current values |
| Backtest | Return curves, metrics, walk-forward results |
| Activity | Chronological log of all system actions |
| Kill switch | `POST /kill` — authenticated endpoint |

**Design decisions:**
- OpenAPI/Swagger auto-generated by FastAPI
- Auth-protected (API key or token)
- Stateless — all state in PostgreSQL and Redis
- Frontend is a future concern — will consume this same API

### 7. Notifications Service

| Event | Channel | Priority |
|-------|---------|----------|
| Soft ceiling breach (7% NAV) | Email + Slack | Medium |
| Trade executed | Slack | Low |
| Stop-loss triggered | Email + Slack | High |
| Drawdown warning (10%) | Email + Slack + SMS | Critical |
| Circuit breaker triggered (20%) | Email + Slack + SMS | Critical |
| Kill switch activated | Email + Slack + SMS | Critical |
| IB connection lost | Email + Slack | High |
| Model retrain completed | Slack | Low |

---

## Backtesting Engine

**Approach:** Replay historical data through the identical service pipeline. Each service reads `MODE=backtest` and switches from live data/IB to database reads and simulated fills. Minimal code divergence between backtest and live.

**Backtest window:** 10 years of historical data.

**Simulated execution:**

| Aspect | Simulation Rule |
|--------|----------------|
| Entry (limit order) | Filled if the day's low <= limit price |
| Exit (market order) | Filled at next bar's open price |
| Slippage | Configurable estimate (default 0.1%) |
| Commission | IB's tiered commission schedule |
| Margin interest | IB's margin rates applied to leveraged positions |

**Walk-forward validation:**
- Train ML on first N months, test on next 6 months, slide forward
- Matches the 6-month retrain cadence of the live system
- ~18-19 test windows across 10 years

**Backtest metrics:**

| Metric | Description |
|--------|-------------|
| Total return vs benchmark (SPY) | Strategy vs buy-and-hold |
| Sharpe ratio | Risk-adjusted return |
| Max drawdown | Worst peak-to-trough |
| Win rate | % of profitable trades |
| Avg holding period | Validates position trading intent |
| Margin utilization over time | Leverage usage patterns |
| Per-signal feature importance | Which signals drove returns |

---

## Infrastructure & Operations

### Project Structure

```
algo-poc/
├── services/
│   ├── data_ingestion/
│   ├── signal_generation/
│   ├── ml_model/
│   ├── risk_management/
│   ├── execution/
│   ├── api/
│   └── notifications/
├── shared/
│   ├── models/          # SQLAlchemy models (shared DB schema)
│   ├── schemas/         # Pydantic schemas (shared message formats)
│   ├── redis_client.py  # Redis Streams helper
│   └── config.py        # Shared configuration loading
├── backtest/
│   ├── runner.py        # Backtest orchestrator
│   └── simulator.py     # Simulated execution
├── config/
│   ├── default.yaml     # Default thresholds, universe, etc.
│   └── secrets.yaml     # IB credentials, API keys (gitignored)
├── docker-compose.yml
├── pyproject.toml       # Single Python project, uv/poetry for deps
└── tests/
```

### Running the System

- **Docker Compose** — all 7 services + PostgreSQL + Redis
- `docker compose up` — start everything
- `docker compose up <service-name>` — start specific services
- Each service has its own `Dockerfile`

### Configuration

- All thresholds in `config/default.yaml` — no code changes to tune
- `config/secrets.yaml` gitignored, loaded via environment variables in Docker
- `MODE=live|backtest|paper` switches behavior across all services

### Operational Features

- **Health checks** — each service exposes `/health`, Docker restarts on failure
- **Structured logging** — JSON logs from all services
- **Graceful shutdown** — services flush pending work on SIGTERM

---

## Technology Stack

| Component | Technology |
|-----------|-----------|
| Language | Python |
| Message bus | Redis Streams |
| Database | PostgreSQL |
| IB connection | `ib_insync` |
| ML framework | LightGBM |
| API framework | FastAPI |
| Data processing | pandas, numpy |
| ORM | SQLAlchemy |
| Schemas | Pydantic |
| Containerization | Docker + Docker Compose |
| Dependency management | uv or poetry (via pyproject.toml) |

---

## Future Work (Out of Scope for POC)

- Frontend dashboard (design system TBD)
- Production deployment (cloud infrastructure, Kubernetes)
- Additional asset classes
- Multiple concurrent strategies
- Advanced ML models (ensembles, deep learning)
