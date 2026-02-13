# Automated Trading Bot — Design Document

**Date:** 2026-02-13
**Status:** Approved with conditions
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

Note: Stop-loss and circuit-breaker protections are also enforced by independent price-event monitoring (not only recommendation flow) to guarantee strict risk response.

| Control | Threshold | Action on breach |
|---------|-----------|-----------------|
| Position entry limit | Max 5% of NAV per position | Scale down order to max allowed |
| Sector concentration | Max 20% of NAV per sector | Reject order |
| Total exposure | Max 150% of NAV (margin allowed) | Reject order |
| Stop-loss | Per-position trailing stop (configurable %) | Emit market sell order immediately on trigger |
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
- Auth-protected with RBAC (API key/token + role enforcement for privileged actions)
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
| Entry (limit order) | Filled if the day's low <= limit price, subject to signal timestamp and order submission lag assumptions |
| Exit (market order) | Filled at next bar's open price, subject to signal timestamp and order submission lag assumptions |
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

## Future Work (Post-Production Enhancements)

- Frontend dashboard (design system TBD)
- Additional asset classes
- Multiple concurrent strategies
- Advanced ML models (ensembles, deep learning)

---

## Production Review Addendum (2026-02-13)

This section captures review comments for productionization and clarifies control intent.

### 1) Risk Control Intent: Hard vs Soft

Threshold values in this document are indicative and tuned in configuration. Enforcement strictness is by control type:

| Control Type | Examples | Enforcement |
|--------------|----------|-------------|
| Hard controls (strict, automated) | Stop-loss, drawdown circuit breaker, kill switch, critical margin protection | Immediate automated action; exits use market orders for certainty |
| Soft controls (advisory / discretionary) | Profit-taking ceilings, soft concentration drift, non-critical trim opportunities | Alert-first and/or human-in-the-loop judgment before optional action |

### 2) Decision Precedence (Deterministic)

When multiple controls trigger at once, apply this order:

1. Kill switch / circuit breaker
2. Critical margin protection
3. Stop-loss exits
4. Hard compliance constraints (position, sector, total exposure)
5. Soft/advisory controls (ceiling notifications, discretionary trims)

Hard controls always override soft controls.

### 3) Human-in-the-Loop Policy

- Advisory actions must include recommended order parameters and rationale in API + notifications.
- Manual decisions must be logged with actor, timestamp, reason, and resulting action.
- Define response SLAs for market-hour alerts (e.g., advisory trim/ceiling events) and escalation paths.

### 4) Point-in-Time Data Requirements

To avoid lookahead leakage and ensure auditability, all fundamental/event records must carry:

- `effective_at` (when the market could first know the fact)
- `ingested_at` (when our system ingested it)
- `source_revision` (vendor/document revision identifier)

Backtest and live feature generation must use the same point-in-time filtering semantics.

### 5) Message Bus Reliability Contract

Redis Streams usage must define and implement:

- Consumer groups per stream with explicit ack/retry behavior
- Dead-letter stream for poison messages
- Replay/recovery procedure after service restart
- Idempotency keys for all decision-producing events (not only execution)

### 6) Backtest/Live Parity Controls

- Explicitly timestamp signal creation, recommendation emission, and order submission in simulation.
- Track and report parity metrics between paper/live and simulator assumptions (fill rate, slippage, time-to-fill).
- Gate production model/policy changes behind regression checks on historical + paper-trading scenarios.

### 7) Security and Governance Hardening

- Role-based access control for trade-control endpoints (`/kill`, policy overrides, manual order actions).
- Immutable audit trail for all operator-initiated actions.
- Secrets management and rotation policy for API and broker credentials.
- Network restrictions (allowlist/VPN) for privileged operational endpoints.

### 8) Observability

Structured logging alone is insufficient for a microservices architecture.

- **Metrics** — each service exposes Prometheus metrics (request latency, error rates, queue depth). Grafana dashboards for system health.
- **Distributed tracing** — trace a signal from ingestion through recommendation to order fill. OpenTelemetry instrumentation across all services.
- **System health alerts** (distinct from trading alerts) — service down, Redis unreachable, DB connection pool exhaustion, message consumer lag.

### 9) Database Migration Strategy

Seven services share PostgreSQL. Schema evolution must be coordinated.

- Use Alembic for versioned migrations.
- The `shared/models/` package owns the schema. Migrations live in a dedicated `migrations/` directory.
- Migrations run as a separate step before service startup (not embedded in service boot).
- Backward-compatible migrations only — no breaking changes without a multi-step rollout plan.

### 10) Testing Strategy

- **Unit tests** — per-service, mock Redis and PostgreSQL. Test signal computation, risk rule evaluation, order logic in isolation.
- **Integration tests** — spin up Redis + PostgreSQL in Docker, run multi-service flows end-to-end (signal → recommendation → risk check → simulated execution).
- **IB mock** — a fake IB gateway that implements the `ib_insync` interface for testing execution service without a live/paper IB connection.
- **Backtest as regression suite** — run the full backtest as a CI gate; flag if key metrics (Sharpe, drawdown) deviate beyond tolerance from baseline.

### 11) Service Startup Ordering

Services have dependencies that must be respected.

**Startup order:**
1. PostgreSQL + Redis (infrastructure)
2. Data Ingestion (populates streams)
3. Signal Generation (consumes data streams)
4. ML Model (consumes signal stream)
5. Risk Management (consumes recommendations)
6. Execution (consumes approved orders) — must not start accepting orders until Risk Management is healthy
7. API + Notifications (monitoring, can start anytime after infrastructure)

Docker Compose `depends_on` with health check conditions enforces this. Each service waits for its upstream dependencies to be healthy before processing messages.

### 12) Market Hours Awareness

A shared `market_calendar` module (using the `exchange_calendars` library) provides:

- Market open/close times (including early close days)
- Holiday schedule
- `is_market_open()` helper used by all time-sensitive operations:
  - Periodic risk scans (every 30 min during market hours only)
  - Unfilled order cancellation (at market close, not arbitrary EOD)
  - Data ingestion polling (active during market hours, reduced after hours)
  - Notification urgency (critical alerts outside market hours still send SMS)

### 13) IB Position Reconciliation

The system's portfolio DB and IB's actual positions can drift (missed fills, manual trades in IB, corporate actions).

- **Periodic reconciliation** — at market open and close, query IB for all positions and compare against portfolio DB.
- **Discrepancy handling:**
  - Minor (rounding, timing): auto-correct DB to match IB.
  - Major (unknown position, missing position): alert + halt new trading until manually resolved.
- **Corporate actions** — stock splits, mergers, symbol changes detected via IB events and reflected in DB.

### 14) Universe Rebalancing

The S&P 500 watchlist is not static.

- **Quarterly review** — on S&P rebalance dates, update the YAML watchlist (manual or semi-automated via an index composition data source).
- **Additions** — new tickers begin data ingestion immediately; signals need 1 year of history before the ML model can act on them (backfill required).
- **Removals** — if the system holds a position in a removed ticker, it continues monitoring until the position is closed. The ticker is not abruptly dropped.

### 15) Signal Staleness Detection

If a data source goes down, the ML model must not act on stale signals.

- Every signal carries a `computed_at` timestamp.
- Before assembling a feature vector, check each signal's age against configurable staleness thresholds:
  - Market data: since last expected market session close + configurable grace window (calendar-aware)
  - Fundamentals: 1 week
  - Events/news: 48 hours
- If any signal exceeds its staleness threshold, mark the feature vector as incomplete.
- Incomplete feature vectors: skip the ticker for that evaluation cycle and emit an alert. Do not feed stale data to the model.
- Staleness evaluation must use the shared market calendar (weekends, holidays, early closes) to avoid false positives outside regular trading sessions.

### 16) Partial Fill Handling

Limit entry orders may be partially filled before timeout or cancellation.

- Define a **minimum viable position size** (configurable, e.g., 40% of intended order size).
- If a partial fill meets the minimum: keep the position, log the shortfall, do not re-attempt.
- If a partial fill is below the minimum:
  - Close only when required by a hard risk/compliance rule or when notional is below minimum tradable lot constraints.
  - Otherwise keep as an undersized position, mark for operator review, and suppress immediate liquidation churn.
- All partial fill decisions are logged in the audit trail.

### 17) Correlation Risk

Sector concentration limits alone do not capture correlated exposure across sectors.

- Track **portfolio-level beta** (vs SPY) using rolling 1-year regression.
- Alert when portfolio beta exceeds a configurable threshold (e.g., 1.5).
- Track **pairwise position correlation** — flag when more than 50% of positions have trailing correlation > 0.7.
- Data sufficiency rule: require a minimum lookback window (configurable, e.g., 60 trading days) before computing beta/correlation for a position.
- Fallback behavior: positions with insufficient history are excluded from hard counts and surfaced with low-confidence flags in dashboard/alerts.
- These are advisory controls (soft) — alerts and dashboard visibility, not automated blocking. The operator decides whether to reduce correlated exposure.

### 18) Data Backup and Recovery

PostgreSQL holds critical state: trade history, audit logs, model training data, portfolio positions.

- **Automated daily backups** — pg_dump to a separate storage location (local disk + off-site/cloud).
- **Point-in-time recovery** — enable WAL archiving for continuous backup, allowing recovery to any point in time.
- **Backup verification** — weekly automated restore test to a scratch database to confirm backup integrity.
- **Retention** — daily backups retained for 30 days, monthly snapshots retained for 1 year.
- **Recovery objectives** — define and track RPO/RTO targets (e.g., RPO <= 15 min with WAL, RTO <= 2 hours for primary restore).
- **Backup security** — encrypt backups at rest and in transit; document key ownership/rotation and restore access controls.
- **Recovery playbook** — documented procedure for restoring from backup, including IB position reconciliation after recovery.
