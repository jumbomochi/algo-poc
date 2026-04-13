# Design: DB Persistence for Paper Trading + Automated Model Retraining

**Date:** 2026-04-13
**Status:** Approved

## Overview

Two related features that prepare the paper trading system for production:

1. **DB Persistence** — migrate paper trading state from JSON file to PostgreSQL, extending the existing `Position` and `Trade` SQLAlchemy models
2. **Automated Retraining** — weekly ML signal quality model retraining with a promotion gate, reading training data from the DB

Feature 2 depends on Feature 1 (retraining reads trades from Postgres).

---

## Feature 1: Paper Trading State in PostgreSQL

### Goal

Replace `data/paper_state.json` with PostgreSQL as the single source of truth for paper trading state. This gives queryable trade history, crash recovery via transactions, and alignment with the live trading path.

### Schema Changes

#### `positions` table (existing — extend)

| Column | Type | Change | Notes |
|--------|------|--------|-------|
| `portfolio` | `String(50)` | **Add**, not null | Strategy name (e.g. "momentum") |
| `peak_price` | `Float` | **Add**, not null | For trailing stop tracking |
| `entry_signals` | `JSON` | **Add**, nullable | Signal dict that triggered entry |
| `sector` | `String(50)` | **Make nullable** | Paper trading may not know sector |
| `recommendation_id` | — | **Not on model** | Not present, no change needed |

Note: `quantity` changes from `Integer` to `Float` to support fractional shares (already in use since commit `166cc2c`).

#### `trades` table (existing — extend)

| Column | Type | Change | Notes |
|--------|------|--------|-------|
| `portfolio` | `String(50)` | **Add**, not null | Strategy name |
| `entry_price` | `Float` | **Add**, not null | Averaged entry price |
| `entry_date` | `Date` | **Add**, not null | Date position was opened (copied from position at sell time) |
| `exit_reason` | `String(50)` | **Add**, nullable | e.g. "trailing_stop", "signal", "max_hold" |
| `pnl` | `Float` | **Add**, not null | Realized P&L |
| `entry_signals` | `JSON` | **Add**, nullable | Signal dict at entry |
| `bar_features` | `JSON` | **Add**, nullable | Bar-derived features for ML training |
| `recommendation_id` | `String(50)` | **Make nullable** | Not used in paper trading |
| `order_type` | `String(20)` | **Make nullable** | Not used in paper trading |
| `quantity` | — | **Change to Float** | Fractional shares |

#### `equity_snapshots` table (new)

| Column | Type | Notes |
|--------|------|-------|
| `id` | `Integer` PK | Auto-increment |
| `portfolio` | `String(50)`, not null | Strategy name, or `_aggregate` for total |
| `date` | `Date`, not null | Trading day |
| `equity` | `Float`, not null | Total equity (cash + market value) |
| `cash` | `Float`, not null | Cash available |
| `market_value` | `Float`, not null | Sum of position market values |
| `created_at` | `DateTime(tz)`, not null | Row creation time |

Index: `(portfolio, date)` unique.

#### `portfolio_config` table (new)

| Column | Type | Notes |
|--------|------|-------|
| `id` | `Integer` PK | Auto-increment |
| `portfolio` | `String(50)`, not null, unique | Strategy name |
| `capital` | `Float`, not null | Initial allocation (immutable) |
| `cash` | `Float`, not null | Current cash available |
| `created_at` | `DateTime(tz)`, not null | Row creation time |
| `updated_at` | `DateTime(tz)`, not null | Last modification |

### Migration

Single Alembic migration covering all changes:
- ALTER `positions`: add `portfolio`, `peak_price`, `entry_signals`; make `sector` nullable; change `quantity` to Float
- ALTER `trades`: add `portfolio`, `entry_price`, `entry_date`, `exit_reason`, `pnl`, `entry_signals`, `bar_features`; make `recommendation_id` and `order_type` nullable; change `quantity` to Float
- CREATE `equity_snapshots`
- CREATE `portfolio_config`

### `PaperTradingState` Refactor

The class switches from JSON file I/O to SQLAlchemy session operations:

```
class PaperTradingState:
    def __init__(self, session_factory):
        self._session_factory = session_factory

    @classmethod
    def create_new(cls, portfolio_capitals, session_factory) -> PaperTradingState:
        # INSERT into portfolio_config for each portfolio

    @classmethod
    def load(cls, session_factory) -> PaperTradingState:
        # SELECT from portfolio_config (validates state exists)

    def save(self):
        # No-op or commit — writes happen transactionally during record_fill

    def record_fill(self, portfolio, ticker, action, quantity, price, fill_date,
                    entry_signals=None, bar_features=None, exit_reason=None):
        # BUY: INSERT or UPDATE positions row, UPDATE portfolio_config.cash
        #      Store entry_signals on position row
        # SELL: INSERT into trades (copy entry_date, entry_signals from position,
        #       store bar_features, exit_reason, pnl), DELETE position,
        #       UPDATE portfolio_config.cash

    def update_peak_prices(self, portfolio, current_prices):
        # UPDATE positions SET peak_price = max(peak_price, ?) WHERE portfolio = ?

    def compute_equity(self, portfolio, current_prices):
        # SELECT cash FROM portfolio_config + SUM(quantity * price) FROM positions

    def record_equity_snapshot(self, portfolio, date, equity, cash, market_value):
        # UPSERT into equity_snapshots

    @property
    def portfolios(self):
        # Returns a dict-like view for backward compatibility with print_status
```

**Method signatures stay the same** so `run_paper.py` changes are minimal — primarily swapping the constructor from file path to DB session, and adding equity snapshot recording after each daily run.

### Transaction Boundaries

Each daily run wraps all fill recording in a single transaction:
1. Begin transaction
2. For each signal: `record_fill()`
3. For each portfolio: `update_peak_prices()`, `record_equity_snapshot()`
4. Commit

If any step fails, the entire day rolls back — no partial state corruption.

### CLI Changes

- `--init` creates `portfolio_config` rows (errors if rows already exist)
- `--status` queries positions, trades, equity_snapshots from DB
- `--state-file` flag removed (no longer needed)
- `--db-url` flag added (defaults to config `database.url`)
- `--reset` flag added to wipe all paper state tables and re-init (with confirmation prompt)

### What Gets Deleted

- `data/paper_state.json` — no longer used
- JSON file I/O code in `PaperTradingState`

---

## Feature 2: Automated Model Retraining

### Goal

Weekly automated retraining of the LightGBM signal quality model, with a promotion gate that only deploys better models.

### Pipeline: `scripts/retrain_model.py`

Steps executed on each run:

1. **Query training data** — SELECT completed trades from `trades` table where `entry_signals` is not null. Include `bar_features`, `pnl`, `portfolio`, `entry_date`. The `entry_date` is stored directly on the trade row (copied from position at sell time).

2. **Sample check** — if trade count < `min_training_samples` (200, from config), log warning and exit. No model trained.

3. **Extract features** — call existing `extract_features()` from `backtest/feature_extractor.py`. The function already handles the trade dict format.

4. **Walk-forward evaluation** — call existing `walk_forward_evaluate()` with `n_splits=3`. Collect per-fold metrics.

5. **Train final model** — call existing `train_final_model()` on all data.

6. **Promotion gate** — load current active `ModelVersion` from DB. Compare:
   - New avg walk-forward accuracy >= current accuracy
   - New avg filtered win rate >= current filtered win rate
   - If no active model exists, promote unconditionally
   - If either metric regresses, skip promotion and log reason

7. **Save artifacts**:
   - Model file: `data/models/signal_quality_v{version}.txt`
   - Version string: `v{YYYY-MM-DD}` (date of training)

8. **Record in DB**:
   - INSERT into `model_versions` with metrics JSON, training window dates, model path
   - If promoted: SET `is_active = True`, deactivate previous active model
   - If skipped: SET `is_active = False` (kept for audit trail)

9. **Print summary** — promoted/skipped, key metrics comparison

### CLI Interface

```
python scripts/retrain_model.py                    # Run retraining pipeline
python scripts/retrain_model.py --force            # Skip promotion gate, always promote
python scripts/retrain_model.py --dry-run          # Evaluate but don't save/promote
python scripts/retrain_model.py --db-url <url>     # Override database URL
```

### `ModelVersion` Table

Already exists in `shared/models/ml_models.py` — no schema changes needed. Fields:
- `version`: `String(50)`, unique — e.g. "v2026-04-13"
- `training_window_start` / `training_window_end`: `Date`
- `metrics`: `JSON` — stores walk-forward fold results, accuracy, win rates, feature importance
- `model_path`: `String(500)` — path to saved model file
- `is_active`: `Boolean` — only one active at a time
- `created_at`: `DateTime(tz)`

### Reuse of Existing Code

The retraining script reuses these existing functions without modification:
- `backtest.feature_extractor.extract_features()`
- `scripts.train_signal_model.walk_forward_evaluate()`
- `scripts.train_signal_model.train_final_model()`
- `scripts.train_signal_model._prepare_for_lgb()`

The only new code is the DB query layer, promotion gate comparison, and the CLI wrapper.

### Scheduling

The script is a standalone CLI, designed to be called by launchd (same pattern as the paper trading runner). Recommended schedule: weekly, Saturday morning SGT (after Friday US market close, after the daily paper run completes).

---

## Out of Scope

- Live trading changes
- Redis Streams / microservice pipeline wiring
- Notification integration (Slack/email/SMS)
- Backtest runner changes (continues using JSON results)
- Changes to signal generation logic
- Multi-brokerage support

## Build Order

1. Alembic migration (schema changes + new tables)
2. Refactor `PaperTradingState` to use SQLAlchemy
3. Update `run_paper.py` CLI and daily run flow
4. Tests for DB-backed state
5. `retrain_model.py` script
6. Tests for retraining pipeline
