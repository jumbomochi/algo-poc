# DB Persistence & Automated Retraining Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate paper trading state from JSON to PostgreSQL and add weekly automated model retraining with a promotion gate.

**Architecture:** Extend existing `Position` and `Trade` SQLAlchemy models with paper-trading fields, add `EquitySnapshot` and `PortfolioConfig` models, refactor `PaperTradingState` to use DB sessions. Then build a `retrain_model.py` script that queries trades from DB and only promotes models that beat the current active one.

**Tech Stack:** SQLAlchemy 2.0 (mapped_column), Alembic, PostgreSQL, LightGBM, pytest with in-memory SQLite for tests

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `shared/models/portfolio.py` | Modify | Add `portfolio`, `peak_price`, `entry_signals` to Position; add `portfolio`, `entry_price`, `entry_date`, `exit_reason`, `pnl`, `entry_signals`, `bar_features` to Trade; change quantity to Float; make some fields nullable |
| `shared/models/equity_snapshot.py` | Create | New `EquitySnapshot` model |
| `shared/models/portfolio_config.py` | Create | New `PortfolioConfig` model |
| `shared/models/__init__.py` | Modify | Export new models |
| `migrations/versions/001_paper_trading_schema.py` | Create | Alembic migration for all schema changes |
| `scripts/paper_state.py` | Rewrite | SQLAlchemy-backed `PaperTradingState` |
| `scripts/run_paper.py` | Modify | Use DB session instead of file path; add equity snapshot recording |
| `scripts/retrain_model.py` | Rewrite | DB-backed retraining pipeline with promotion gate |
| `tests/backtest/test_paper_state.py` | Rewrite | Test against in-memory SQLite |
| `tests/shared/test_models.py` | Modify | Update column assertions for new fields |
| `tests/backtest/test_retrain_model.py` | Create | Tests for DB-backed retraining pipeline |

---

### Task 1: Extend Position and Trade Models

**Files:**
- Modify: `shared/models/portfolio.py`
- Modify: `tests/shared/test_models.py`

- [ ] **Step 1: Write failing tests for new Position columns**

In `tests/shared/test_models.py`, update the existing `test_position_model_has_nav_fields` test and add a new one:

```python
def test_position_model_has_nav_fields():
    cols = {c.name for c in Position.__table__.columns}
    assert cols >= {
        "id", "ticker", "quantity", "avg_entry_price", "sector",
        "opened_at", "status", "portfolio", "peak_price", "entry_signals",
    }


def test_position_quantity_is_float():
    col = Position.__table__.columns["quantity"]
    assert isinstance(col.type, Float)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/shared/test_models.py::test_position_model_has_nav_fields tests/shared/test_models.py::test_position_quantity_is_float -v`
Expected: FAIL — missing columns `portfolio`, `peak_price`, `entry_signals`; quantity is Integer not Float

- [ ] **Step 3: Write failing tests for new Trade columns**

In `tests/shared/test_models.py`, update the existing test and add a new one:

```python
def test_trade_model_has_audit_fields():
    cols = {c.name for c in Trade.__table__.columns}
    assert cols >= {
        "id", "ticker", "side", "quantity", "price", "order_type",
        "recommendation_id", "executed_at", "portfolio", "entry_price",
        "entry_date", "exit_reason", "pnl", "entry_signals", "bar_features",
    }


def test_trade_quantity_is_float():
    col = Trade.__table__.columns["quantity"]
    assert isinstance(col.type, Float)


def test_trade_recommendation_id_is_nullable():
    col = Trade.__table__.columns["recommendation_id"]
    assert col.nullable is True


def test_trade_order_type_is_nullable():
    col = Trade.__table__.columns["order_type"]
    assert col.nullable is True
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `pytest tests/shared/test_models.py -v`
Expected: FAIL — missing new columns, quantity not Float, recommendation_id/order_type not nullable

- [ ] **Step 5: Update Position model**

In `shared/models/portfolio.py`, update the `Position` class. Add import for `JSON` at the top:

```python
from sqlalchemy import DateTime, Float, Index, Integer, JSON, String
```

Update `Position`:

```python
class Position(Base):
    __tablename__ = "positions"
    __table_args__ = (
        Index("ix_positions_ticker_status", "ticker", "status"),
        Index("ix_positions_portfolio", "portfolio"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker: Mapped[str] = mapped_column(String(10), nullable=False)
    portfolio: Mapped[str] = mapped_column(String(50), nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    avg_entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    current_price: Mapped[float] = mapped_column(Float, nullable=False)
    peak_price: Mapped[float] = mapped_column(Float, nullable=False)
    sector: Mapped[str | None] = mapped_column(String(50), nullable=True)
    highest_price_since_entry: Mapped[float] = mapped_column(Float, nullable=False)
    entry_signals: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="open"
    )
```

- [ ] **Step 6: Update Trade model**

In `shared/models/portfolio.py`, update the `Trade` class. Add import for `Date`:

```python
from datetime import date, datetime
```

Update `Trade`:

```python
class Trade(Base):
    __tablename__ = "trades"
    __table_args__ = (
        Index("ix_trade_ticker_executed", "ticker", "executed_at"),
        Index("ix_trade_recommendation", "recommendation_id"),
        Index("ix_trade_portfolio", "portfolio"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker: Mapped[str] = mapped_column(String(10), nullable=False)
    portfolio: Mapped[str] = mapped_column(String(50), nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    entry_date: Mapped[date] = mapped_column(Date, nullable=False)
    order_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    recommendation_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(50), nullable=True)
    pnl: Mapped[float] = mapped_column(Float, nullable=False)
    entry_signals: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    bar_features: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    commission: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    slippage: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    executed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `pytest tests/shared/test_models.py -v`
Expected: All PASS

- [ ] **Step 8: Commit**

```bash
git add shared/models/portfolio.py tests/shared/test_models.py
git commit -m "feat: extend Position and Trade models for paper trading"
```

---

### Task 2: Create EquitySnapshot and PortfolioConfig Models

**Files:**
- Create: `shared/models/equity_snapshot.py`
- Create: `shared/models/portfolio_config.py`
- Modify: `shared/models/__init__.py`
- Modify: `tests/shared/test_models.py`

- [ ] **Step 1: Write failing tests for EquitySnapshot**

Add to `tests/shared/test_models.py`:

```python
from shared.models.equity_snapshot import EquitySnapshot
from shared.models.portfolio_config import PortfolioConfig


def test_equity_snapshot_has_required_fields():
    cols = {c.name for c in EquitySnapshot.__table__.columns}
    assert cols >= {
        "id", "portfolio", "date", "equity", "cash", "market_value", "created_at",
    }


def test_equity_snapshot_unique_portfolio_date():
    """Unique constraint on (portfolio, date)."""
    indexes = EquitySnapshot.__table__.indexes
    idx_cols = set()
    for idx in indexes:
        if idx.unique:
            idx_cols = {c.name for c in idx.columns}
    assert idx_cols >= {"portfolio", "date"}
```

- [ ] **Step 2: Write failing tests for PortfolioConfig**

Add to `tests/shared/test_models.py`:

```python
def test_portfolio_config_has_required_fields():
    cols = {c.name for c in PortfolioConfig.__table__.columns}
    assert cols >= {
        "id", "portfolio", "capital", "cash", "created_at", "updated_at",
    }


def test_portfolio_config_portfolio_is_unique():
    col = PortfolioConfig.__table__.columns["portfolio"]
    assert col.unique is True


def test_new_models_share_base():
    assert issubclass(EquitySnapshot, Base)
    assert issubclass(PortfolioConfig, Base)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/shared/test_models.py::test_equity_snapshot_has_required_fields tests/shared/test_models.py::test_portfolio_config_has_required_fields -v`
Expected: FAIL — ImportError, modules don't exist yet

- [ ] **Step 4: Create EquitySnapshot model**

Create `shared/models/equity_snapshot.py`:

```python
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, DateTime, Float, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base


class EquitySnapshot(Base):
    __tablename__ = "equity_snapshots"
    __table_args__ = (
        Index("ix_equity_portfolio_date", "portfolio", "date", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    portfolio: Mapped[str] = mapped_column(String(50), nullable=False)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    equity: Mapped[float] = mapped_column(Float, nullable=False)
    cash: Mapped[float] = mapped_column(Float, nullable=False)
    market_value: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
```

- [ ] **Step 5: Create PortfolioConfig model**

Create `shared/models/portfolio_config.py`:

```python
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, String
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base


class PortfolioConfig(Base):
    __tablename__ = "portfolio_config"

    id: Mapped[int] = mapped_column(primary_key=True)
    portfolio: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    capital: Mapped[float] = mapped_column(Float, nullable=False)
    cash: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
```

- [ ] **Step 6: Update `__init__.py` exports**

In `shared/models/__init__.py`:

```python
from shared.models.base import Base
from shared.models.market_data import OHLCVDaily
from shared.models.fundamentals import FundamentalRecord
from shared.models.events import EventRecord
from shared.models.signals import SignalRecord
from shared.models.portfolio import Position, Trade
from shared.models.equity_snapshot import EquitySnapshot
from shared.models.portfolio_config import PortfolioConfig
from shared.models.audit import AuditLog
from shared.models.ml_models import ModelVersion

__all__ = [
    "Base",
    "OHLCVDaily",
    "FundamentalRecord",
    "EventRecord",
    "SignalRecord",
    "Position",
    "Trade",
    "EquitySnapshot",
    "PortfolioConfig",
    "AuditLog",
    "ModelVersion",
]
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `pytest tests/shared/test_models.py -v`
Expected: All PASS

- [ ] **Step 8: Commit**

```bash
git add shared/models/equity_snapshot.py shared/models/portfolio_config.py shared/models/__init__.py tests/shared/test_models.py
git commit -m "feat: add EquitySnapshot and PortfolioConfig models"
```

---

### Task 3: Create Alembic Migration

**Files:**
- Create: `migrations/versions/001_paper_trading_schema.py`

- [ ] **Step 1: Write the migration**

Create `migrations/versions/001_paper_trading_schema.py`:

```python
"""Add paper trading schema: extend positions/trades, add equity_snapshots and portfolio_config.

Revision ID: 001
Revises:
Create Date: 2026-04-13
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- positions table ---
    # Add new columns
    op.add_column("positions", sa.Column("portfolio", sa.String(50), nullable=False, server_default="unknown"))
    op.add_column("positions", sa.Column("peak_price", sa.Float(), nullable=False, server_default="0"))
    op.add_column("positions", sa.Column("entry_signals", sa.JSON(), nullable=True))
    # Make sector nullable
    op.alter_column("positions", "sector", existing_type=sa.String(50), nullable=True)
    # Change quantity from Integer to Float
    op.alter_column("positions", "quantity", existing_type=sa.Integer(), type_=sa.Float())
    # Remove server defaults (only needed for migration of existing rows)
    op.alter_column("positions", "portfolio", server_default=None)
    op.alter_column("positions", "peak_price", server_default=None)
    # Add index
    op.create_index("ix_positions_portfolio", "positions", ["portfolio"])

    # --- trades table ---
    # Add new columns
    op.add_column("trades", sa.Column("portfolio", sa.String(50), nullable=False, server_default="unknown"))
    op.add_column("trades", sa.Column("entry_price", sa.Float(), nullable=False, server_default="0"))
    op.add_column("trades", sa.Column("entry_date", sa.Date(), nullable=False, server_default="2020-01-01"))
    op.add_column("trades", sa.Column("exit_reason", sa.String(50), nullable=True))
    op.add_column("trades", sa.Column("pnl", sa.Float(), nullable=False, server_default="0"))
    op.add_column("trades", sa.Column("entry_signals", sa.JSON(), nullable=True))
    op.add_column("trades", sa.Column("bar_features", sa.JSON(), nullable=True))
    # Make recommendation_id and order_type nullable
    op.alter_column("trades", "recommendation_id", existing_type=sa.String(50), nullable=True)
    op.alter_column("trades", "order_type", existing_type=sa.String(20), nullable=True)
    # Change quantity from Integer to Float
    op.alter_column("trades", "quantity", existing_type=sa.Integer(), type_=sa.Float())
    # Remove server defaults
    op.alter_column("trades", "portfolio", server_default=None)
    op.alter_column("trades", "entry_price", server_default=None)
    op.alter_column("trades", "entry_date", server_default=None)
    op.alter_column("trades", "pnl", server_default=None)
    # Add index
    op.create_index("ix_trade_portfolio", "trades", ["portfolio"])

    # --- equity_snapshots table (new) ---
    op.create_table(
        "equity_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("portfolio", sa.String(50), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("equity", sa.Float(), nullable=False),
        sa.Column("cash", sa.Float(), nullable=False),
        sa.Column("market_value", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_equity_portfolio_date", "equity_snapshots",
        ["portfolio", "date"], unique=True,
    )

    # --- portfolio_config table (new) ---
    op.create_table(
        "portfolio_config",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("portfolio", sa.String(50), nullable=False, unique=True),
        sa.Column("capital", sa.Float(), nullable=False),
        sa.Column("cash", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("portfolio_config")
    op.drop_index("ix_equity_portfolio_date", table_name="equity_snapshots")
    op.drop_table("equity_snapshots")

    # trades: drop new columns, restore non-nullable, restore Integer
    op.drop_index("ix_trade_portfolio", table_name="trades")
    op.drop_column("trades", "bar_features")
    op.drop_column("trades", "entry_signals")
    op.drop_column("trades", "pnl")
    op.drop_column("trades", "exit_reason")
    op.drop_column("trades", "entry_date")
    op.drop_column("trades", "entry_price")
    op.drop_column("trades", "portfolio")
    op.alter_column("trades", "recommendation_id", existing_type=sa.String(50), nullable=False)
    op.alter_column("trades", "order_type", existing_type=sa.String(20), nullable=False)
    op.alter_column("trades", "quantity", existing_type=sa.Float(), type_=sa.Integer())

    # positions: drop new columns, restore non-nullable sector, restore Integer
    op.drop_index("ix_positions_portfolio", table_name="positions")
    op.drop_column("positions", "entry_signals")
    op.drop_column("positions", "peak_price")
    op.drop_column("positions", "portfolio")
    op.alter_column("positions", "sector", existing_type=sa.String(50), nullable=False)
    op.alter_column("positions", "quantity", existing_type=sa.Float(), type_=sa.Integer())
```

- [ ] **Step 2: Verify migration file is syntactically valid**

Run: `python -c "import migrations.versions.001_paper_trading_schema as m; print('upgrade:', hasattr(m, 'upgrade'), 'downgrade:', hasattr(m, 'downgrade'))"`
Expected: `upgrade: True downgrade: True`

- [ ] **Step 3: Commit**

```bash
git add migrations/versions/001_paper_trading_schema.py
git commit -m "feat: add Alembic migration for paper trading schema"
```

---

### Task 4: Rewrite PaperTradingState with SQLAlchemy

**Files:**
- Rewrite: `scripts/paper_state.py`
- Rewrite: `tests/backtest/test_paper_state.py`

- [ ] **Step 1: Write failing tests for DB-backed PaperTradingState**

Rewrite `tests/backtest/test_paper_state.py`. Tests use an in-memory SQLite database:

```python
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from shared.models.base import Base
from scripts.paper_state import PaperTradingState


@pytest.fixture
def db_session():
    """Create an in-memory SQLite database with all tables."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    session = factory()
    yield session
    session.close()


def test_create_new_initializes_portfolio_configs(db_session: Session):
    """create_new should insert portfolio_config rows."""
    state = PaperTradingState.create_new(
        portfolio_capitals={"momentum": 20_000, "mr": 14_000},
        session=db_session,
    )
    portfolios = state.get_portfolio_names()
    assert set(portfolios) == {"momentum", "mr"}
    assert state.get_cash("momentum") == 20_000
    assert state.get_cash("mr") == 14_000
    assert state.get_capital("momentum") == 20_000


def test_load_reads_existing_state(db_session: Session):
    """load should read portfolio_config rows created by create_new."""
    PaperTradingState.create_new(
        portfolio_capitals={"momentum": 20_000},
        session=db_session,
    )
    loaded = PaperTradingState.load(db_session)
    assert "momentum" in loaded.get_portfolio_names()
    assert loaded.get_cash("momentum") == 20_000


def test_load_raises_if_no_state(db_session: Session):
    """load should raise if no portfolio_config rows exist."""
    with pytest.raises(ValueError, match="No paper trading state"):
        PaperTradingState.load(db_session)


def test_record_buy_creates_position(db_session: Session):
    """Recording a buy should create an open position."""
    state = PaperTradingState.create_new(
        portfolio_capitals={"mr": 10_000},
        session=db_session,
    )
    state.record_fill(
        portfolio="mr", ticker="AAPL", action="buy",
        quantity=10, price=150.0, fill_date=date(2024, 1, 15),
    )
    positions = state.get_positions("mr")
    assert "AAPL" in positions
    assert positions["AAPL"]["quantity"] == 10
    assert positions["AAPL"]["avg_entry_price"] == 150.0
    assert positions["AAPL"]["peak_price"] == 150.0
    assert state.get_cash("mr") == 10_000 - (150.0 * 10)


def test_record_buy_averages_into_existing(db_session: Session):
    """Buying more of same ticker should average the entry price."""
    state = PaperTradingState.create_new(
        portfolio_capitals={"mr": 50_000},
        session=db_session,
    )
    state.record_fill("mr", "AAPL", "buy", 10, 150.0, date(2024, 1, 1))
    state.record_fill("mr", "AAPL", "buy", 10, 160.0, date(2024, 1, 2))

    positions = state.get_positions("mr")
    assert positions["AAPL"]["quantity"] == 20
    assert positions["AAPL"]["avg_entry_price"] == 155.0  # (150*10 + 160*10) / 20


def test_record_sell_creates_trade(db_session: Session):
    """Selling should remove position and create a trade record."""
    state = PaperTradingState.create_new(
        portfolio_capitals={"mr": 10_000},
        session=db_session,
    )
    state.record_fill("mr", "AAPL", "buy", 10, 150.0, date(2024, 1, 1))
    state.record_fill(
        "mr", "AAPL", "sell", 10, 160.0, date(2024, 1, 15),
        exit_reason="trailing_stop",
    )

    positions = state.get_positions("mr")
    assert "AAPL" not in positions

    trades = state.get_trades("mr")
    assert len(trades) == 1
    assert trades[0]["ticker"] == "AAPL"
    assert trades[0]["pnl"] == 100.0  # (160 - 150) * 10
    assert trades[0]["exit_reason"] == "trailing_stop"
    assert state.get_cash("mr") == 10_000  # bought 1500, sold 1600, net +100


def test_update_peak_prices(db_session: Session):
    """update_peak_prices should update peak for held positions."""
    state = PaperTradingState.create_new(
        portfolio_capitals={"mr": 10_000},
        session=db_session,
    )
    state.record_fill("mr", "AAPL", "buy", 10, 150.0, date(2024, 1, 1))
    state.update_peak_prices("mr", {"AAPL": 170.0})

    positions = state.get_positions("mr")
    assert positions["AAPL"]["peak_price"] == 170.0


def test_compute_equity(db_session: Session):
    """compute_equity should return cash + market value."""
    state = PaperTradingState.create_new(
        portfolio_capitals={"mr": 10_000},
        session=db_session,
    )
    state.record_fill("mr", "AAPL", "buy", 10, 150.0, date(2024, 1, 1))
    # cash = 10000 - 1500 = 8500
    # market value at 160 = 1600
    equity = state.compute_equity("mr", {"AAPL": 160.0})
    assert equity == 8500.0 + 1600.0


def test_record_equity_snapshot(db_session: Session):
    """record_equity_snapshot should create a snapshot row."""
    state = PaperTradingState.create_new(
        portfolio_capitals={"mr": 10_000},
        session=db_session,
    )
    state.record_equity_snapshot(
        portfolio="mr", snap_date=date(2024, 1, 15),
        equity=10_500.0, cash=8_500.0, market_value=2_000.0,
    )
    snapshots = state.get_equity_history("mr")
    assert len(snapshots) == 1
    assert snapshots[0]["equity"] == 10_500.0


def test_record_equity_snapshot_upserts(db_session: Session):
    """Recording snapshot for same portfolio+date should update, not duplicate."""
    state = PaperTradingState.create_new(
        portfolio_capitals={"mr": 10_000},
        session=db_session,
    )
    state.record_equity_snapshot("mr", date(2024, 1, 15), 10_500.0, 8_500.0, 2_000.0)
    state.record_equity_snapshot("mr", date(2024, 1, 15), 10_600.0, 8_600.0, 2_000.0)

    snapshots = state.get_equity_history("mr")
    assert len(snapshots) == 1
    assert snapshots[0]["equity"] == 10_600.0


def test_record_fill_with_entry_signals(db_session: Session):
    """entry_signals should be stored on position and carried to trade."""
    state = PaperTradingState.create_new(
        portfolio_capitals={"mr": 10_000},
        session=db_session,
    )
    signals = {"rsi": 28.5, "support_proximity": 0.02}
    state.record_fill(
        "mr", "AAPL", "buy", 10, 150.0, date(2024, 1, 1),
        entry_signals=signals,
    )
    positions = state.get_positions("mr")
    assert positions["AAPL"]["entry_signals"] == signals

    state.record_fill("mr", "AAPL", "sell", 10, 160.0, date(2024, 1, 15))
    trades = state.get_trades("mr")
    assert trades[0]["entry_signals"] == signals


def test_get_all_trades_for_training(db_session: Session):
    """get_all_trades should return trades across all portfolios as dicts."""
    state = PaperTradingState.create_new(
        portfolio_capitals={"mr": 10_000, "mom": 20_000},
        session=db_session,
    )
    state.record_fill("mr", "AAPL", "buy", 10, 150.0, date(2024, 1, 1),
                       entry_signals={"rsi": 28})
    state.record_fill("mr", "AAPL", "sell", 10, 160.0, date(2024, 1, 15))
    state.record_fill("mom", "MSFT", "buy", 5, 300.0, date(2024, 1, 1))
    state.record_fill("mom", "MSFT", "sell", 5, 320.0, date(2024, 1, 15))

    all_trades = state.get_all_trades()
    assert len(all_trades) == 2
    tickers = {t["ticker"] for t in all_trades}
    assert tickers == {"AAPL", "MSFT"}
    # Each trade has the fields needed for ML training
    for t in all_trades:
        assert "pnl" in t
        assert "portfolio" in t
        assert "entry_date" in t
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/backtest/test_paper_state.py -v`
Expected: FAIL — `PaperTradingState` constructor doesn't accept `session`

- [ ] **Step 3: Implement PaperTradingState with SQLAlchemy**

Rewrite `scripts/paper_state.py`:

```python
#!/usr/bin/env python3
"""Paper trading state persistence backed by PostgreSQL.

Manages position tracking, trade history, equity snapshots, and
per-portfolio capital/cash via SQLAlchemy models.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from shared.models.portfolio import Position, Trade
from shared.models.equity_snapshot import EquitySnapshot
from shared.models.portfolio_config import PortfolioConfig


class PaperTradingState:
    """Manages paper trading state across multiple portfolios in the DB."""

    def __init__(self, session: Session) -> None:
        self._session = session

    @classmethod
    def create_new(
        cls,
        portfolio_capitals: dict[str, float],
        session: Session,
    ) -> PaperTradingState:
        """Create fresh state with initial capital per portfolio."""
        now = datetime.now(timezone.utc)
        for name, capital in portfolio_capitals.items():
            config = PortfolioConfig(
                portfolio=name,
                capital=capital,
                cash=capital,
                created_at=now,
                updated_at=now,
            )
            session.add(config)
        session.flush()
        return cls(session)

    @classmethod
    def load(cls, session: Session) -> PaperTradingState:
        """Load state from DB. Raises ValueError if no state exists."""
        count = session.execute(
            select(PortfolioConfig.id).limit(1)
        ).scalar()
        if count is None:
            raise ValueError("No paper trading state found. Run with --init first.")
        return cls(session)

    def get_portfolio_names(self) -> list[str]:
        """Return list of portfolio names."""
        rows = self._session.execute(
            select(PortfolioConfig.portfolio).order_by(PortfolioConfig.portfolio)
        ).scalars().all()
        return list(rows)

    def get_cash(self, portfolio: str) -> float:
        """Return current cash for a portfolio."""
        row = self._session.execute(
            select(PortfolioConfig.cash).where(PortfolioConfig.portfolio == portfolio)
        ).scalar_one()
        return float(row)

    def get_capital(self, portfolio: str) -> float:
        """Return initial capital for a portfolio."""
        row = self._session.execute(
            select(PortfolioConfig.capital).where(PortfolioConfig.portfolio == portfolio)
        ).scalar_one()
        return float(row)

    def _update_cash(self, portfolio: str, delta: float) -> None:
        """Adjust cash for a portfolio by delta amount."""
        self._session.execute(
            update(PortfolioConfig)
            .where(PortfolioConfig.portfolio == portfolio)
            .values(
                cash=PortfolioConfig.cash + delta,
                updated_at=datetime.now(timezone.utc),
            )
        )
        self._session.flush()

    def record_fill(
        self,
        portfolio: str,
        ticker: str,
        action: str,
        quantity: float,
        price: float,
        fill_date: date,
        entry_signals: dict | None = None,
        bar_features: dict | None = None,
        exit_reason: str | None = None,
    ) -> None:
        """Record a fill (buy or sell) for a portfolio."""
        now = datetime.now(timezone.utc)

        if action == "buy":
            existing = self._session.execute(
                select(Position).where(
                    Position.portfolio == portfolio,
                    Position.ticker == ticker,
                    Position.status == "open",
                )
            ).scalar_one_or_none()

            if existing:
                old_qty = existing.quantity
                old_price = existing.avg_entry_price
                new_qty = old_qty + quantity
                existing.avg_entry_price = (old_price * old_qty + price * quantity) / new_qty
                existing.quantity = new_qty
                existing.current_price = price
                existing.peak_price = max(existing.peak_price, price)
                existing.highest_price_since_entry = max(existing.highest_price_since_entry, price)
                if entry_signals:
                    existing.entry_signals = entry_signals
            else:
                pos = Position(
                    ticker=ticker,
                    portfolio=portfolio,
                    quantity=quantity,
                    avg_entry_price=price,
                    current_price=price,
                    peak_price=price,
                    highest_price_since_entry=price,
                    entry_signals=entry_signals,
                    opened_at=datetime(fill_date.year, fill_date.month, fill_date.day, tzinfo=timezone.utc),
                    status="open",
                )
                self._session.add(pos)

            self._update_cash(portfolio, -(price * quantity))

        elif action == "sell":
            pos = self._session.execute(
                select(Position).where(
                    Position.portfolio == portfolio,
                    Position.ticker == ticker,
                    Position.status == "open",
                )
            ).scalar_one_or_none()

            if pos:
                pnl = (price - pos.avg_entry_price) * quantity
                trade = Trade(
                    ticker=ticker,
                    portfolio=portfolio,
                    side="sell",
                    quantity=quantity,
                    price=price,
                    entry_price=pos.avg_entry_price,
                    entry_date=pos.opened_at.date(),
                    exit_reason=exit_reason,
                    pnl=pnl,
                    entry_signals=pos.entry_signals,
                    bar_features=bar_features,
                    commission=0.0,
                    slippage=0.0,
                    executed_at=datetime(fill_date.year, fill_date.month, fill_date.day, tzinfo=timezone.utc),
                )
                self._session.add(trade)
                self._session.delete(pos)
                self._update_cash(portfolio, price * quantity)

        self._session.flush()

    def update_peak_prices(
        self, portfolio: str, current_prices: dict[str, float]
    ) -> None:
        """Update peak prices for all held positions in a portfolio."""
        positions = self._session.execute(
            select(Position).where(
                Position.portfolio == portfolio,
                Position.status == "open",
            )
        ).scalars().all()

        for pos in positions:
            if pos.ticker in current_prices:
                new_price = current_prices[pos.ticker]
                pos.peak_price = max(pos.peak_price, new_price)
                pos.highest_price_since_entry = max(pos.highest_price_since_entry, new_price)
                pos.current_price = new_price

        self._session.flush()

    def compute_equity(
        self, portfolio: str, current_prices: dict[str, float]
    ) -> float:
        """Compute current equity (cash + market value of positions)."""
        cash = self.get_cash(portfolio)
        positions = self._session.execute(
            select(Position).where(
                Position.portfolio == portfolio,
                Position.status == "open",
            )
        ).scalars().all()

        market_value = sum(
            pos.quantity * current_prices.get(pos.ticker, pos.avg_entry_price)
            for pos in positions
        )
        return cash + market_value

    def record_equity_snapshot(
        self,
        portfolio: str,
        snap_date: date,
        equity: float,
        cash: float,
        market_value: float,
    ) -> None:
        """Record (or update) an equity snapshot for a portfolio on a date."""
        now = datetime.now(timezone.utc)
        existing = self._session.execute(
            select(EquitySnapshot).where(
                EquitySnapshot.portfolio == portfolio,
                EquitySnapshot.date == snap_date,
            )
        ).scalar_one_or_none()

        if existing:
            existing.equity = equity
            existing.cash = cash
            existing.market_value = market_value
            existing.created_at = now
        else:
            snap = EquitySnapshot(
                portfolio=portfolio,
                date=snap_date,
                equity=equity,
                cash=cash,
                market_value=market_value,
                created_at=now,
            )
            self._session.add(snap)

        self._session.flush()

    def get_positions(self, portfolio: str) -> dict[str, dict]:
        """Return open positions for a portfolio as {ticker: {...}}."""
        rows = self._session.execute(
            select(Position).where(
                Position.portfolio == portfolio,
                Position.status == "open",
            )
        ).scalars().all()

        return {
            pos.ticker: {
                "quantity": pos.quantity,
                "avg_entry_price": pos.avg_entry_price,
                "entry_price": pos.avg_entry_price,
                "peak_price": pos.peak_price,
                "entry_date": str(pos.opened_at.date()),
                "entry_signals": pos.entry_signals,
            }
            for pos in rows
        }

    def get_trades(self, portfolio: str) -> list[dict]:
        """Return completed trades for a portfolio."""
        rows = self._session.execute(
            select(Trade)
            .where(Trade.portfolio == portfolio)
            .order_by(Trade.executed_at)
        ).scalars().all()

        return [
            {
                "ticker": t.ticker,
                "portfolio": t.portfolio,
                "entry_price": t.entry_price,
                "exit_price": t.price,
                "quantity": t.quantity,
                "entry_date": str(t.entry_date),
                "exit_date": str(t.executed_at.date()),
                "pnl": t.pnl,
                "exit_reason": t.exit_reason,
                "entry_signals": t.entry_signals,
                "bar_features": t.bar_features,
            }
            for t in rows
        ]

    def get_all_trades(self) -> list[dict]:
        """Return all completed trades across all portfolios (for ML training)."""
        rows = self._session.execute(
            select(Trade).order_by(Trade.executed_at)
        ).scalars().all()

        return [
            {
                "ticker": t.ticker,
                "portfolio": t.portfolio,
                "entry_price": t.entry_price,
                "exit_price": t.price,
                "quantity": t.quantity,
                "entry_date": str(t.entry_date),
                "exit_date": str(t.executed_at.date()),
                "pnl": t.pnl,
                "exit_reason": t.exit_reason,
                "entry_signals": t.entry_signals,
                "bar_features": t.bar_features,
            }
            for t in rows
        ]

    def get_equity_history(self, portfolio: str) -> list[dict]:
        """Return equity snapshots for a portfolio."""
        rows = self._session.execute(
            select(EquitySnapshot)
            .where(EquitySnapshot.portfolio == portfolio)
            .order_by(EquitySnapshot.date)
        ).scalars().all()

        return [
            {
                "date": str(s.date),
                "equity": s.equity,
                "cash": s.cash,
                "market_value": s.market_value,
            }
            for s in rows
        ]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/backtest/test_paper_state.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/paper_state.py tests/backtest/test_paper_state.py
git commit -m "feat: rewrite PaperTradingState with SQLAlchemy backend"
```

---

### Task 5: Update run_paper.py to Use DB Session

**Files:**
- Modify: `scripts/run_paper.py`

- [ ] **Step 1: Update imports and remove JSON state file references**

At the top of `scripts/run_paper.py`, replace the old imports and add DB session setup:

```python
#!/usr/bin/env python3
"""Daily paper trading runner.

Reuses the exact same signal functions from the backtest system.
Fetches latest bars from IB Gateway, runs all 8 signal functions,
and executes signals against DB-backed state.

Usage:
    python scripts/run_paper.py --init            # Initialize fresh state
    python scripts/run_paper.py --status           # Print current positions
    python scripts/run_paper.py                    # Daily signal run (requires IB)
    python scripts/run_paper.py --reset            # Wipe state and re-init
"""
from __future__ import annotations

import argparse
import sys
from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from shared.config import load_config
from scripts.paper_state import PaperTradingState
from scripts.run_backtest import (
    BEAR_TICKERS,
    PortfolioConfig,
    compute_regime_by_date,
    fetch_bars_from_ib,
    get_union_universe,
    make_crash_freeze_signals_fn,
    make_earnings_drift_signals_fn,
    make_momentum_signals_fn,
    make_quality_value_signals_fn,
    make_sector_rotation_signals_fn,
    make_short_term_mr_signals_fn,
    make_signals_fn,
    make_tail_risk_hedge_signals_fn,
    make_thematic_momentum_signals_fn,
)
from scripts.fetch_fundamentals import load_fundamentals_cache, build_fundamentals_lookup, SECTOR_MAP
from scripts.fetch_earnings import load_earnings_cache, build_earnings_lookup
from backtest.aggregate_risk import AggregateRiskMonitor
from services.risk_management.engine import RiskEngine
from shared.models.base import Base
from shared.models.portfolio_config import PortfolioConfig as PortfolioConfigModel
```

- [ ] **Step 2: Update print_status to use DB-backed state**

Replace the existing `print_status` function:

```python
def print_status(state: PaperTradingState) -> None:
    """Print current paper trading status."""
    print("\n" + "=" * 60)
    print("  PAPER TRADING STATUS")
    print("=" * 60)

    total_equity = 0.0
    total_capital = 0.0
    total_positions = 0

    for name in state.get_portfolio_names():
        capital = state.get_capital(name)
        cash = state.get_cash(name)
        positions = state.get_positions(name)
        trades = state.get_trades(name)
        n_pos = len(positions)
        total_positions += n_pos
        total_capital += capital

        market_value = sum(
            pos["quantity"] * pos["avg_entry_price"]
            for pos in positions.values()
        )
        equity = cash + market_value
        total_equity += equity
        pnl = equity - capital

        print(f"\n  --- {name} ---")
        print(f"    Capital:    ${capital:>12,.2f}")
        print(f"    Cash:       ${cash:>12,.2f}")
        print(f"    Equity:     ${equity:>12,.2f}")
        print(f"    P&L:        ${pnl:>+12,.2f}")
        print(f"    Positions:  {n_pos}")
        print(f"    Trades:     {len(trades)}")

        if positions:
            for ticker, pos in positions.items():
                print(f"      {ticker:>6s}  {pos['quantity']:>8.4f} shares @ ${pos['avg_entry_price']:.2f}")

    print(f"\n  --- TOTAL ---")
    print(f"    Capital:    ${total_capital:>12,.2f}")
    print(f"    Equity:     ${total_equity:>12,.2f}")
    print(f"    P&L:        ${total_equity - total_capital:>+12,.2f}")
    print(f"    Positions:  {total_positions}")

    # Risk monitoring
    risk_monitor = AggregateRiskMonitor(
        alert_drawdown_pct=15.0,
        circuit_breaker_pct=22.0,
    )
    aggregate_values = [total_capital, total_equity]
    risk_alerts = risk_monitor.check_aggregate_drawdown(aggregate_values)
    if risk_alerts:
        print(f"\n  RISK ALERTS:")
        for alert in risk_alerts:
            icon = "!!" if alert["level"] == "critical" else " >"
            print(f"    {icon} [{alert['level'].upper()}] {alert['message']}")

    print("=" * 60)
```

- [ ] **Step 3: Update run_daily to record fills and equity snapshots**

Replace the existing `run_daily` function:

```python
def run_daily(
    state: PaperTradingState,
    portfolios: dict[str, PortfolioConfig],
    bars_by_ticker: dict[str, list[dict]],
) -> list[dict]:
    """Run one daily cycle: generate signals, record fills, snapshot equity."""
    signals_generated: list[dict] = []
    today = date.today()

    # Get current prices from latest bar
    current_prices = {}
    for ticker, bars in bars_by_ticker.items():
        if bars:
            current_prices[ticker] = bars[-1]["close"]

    for name, pc in portfolios.items():
        universe = list(bars_by_ticker.keys())

        for ticker in universe:
            bars = bars_by_ticker.get(ticker, [])
            if not bars:
                continue

            signal = pc.signals_fn(ticker, bars)
            if signal is not None:
                signal["portfolio"] = name
                signal["date"] = str(today)
                signals_generated.append(signal)

                action = signal["action"]
                price = signal["limit_price"]
                qty = signal.get("quantity", 0)

                state.record_fill(
                    portfolio=name,
                    ticker=ticker,
                    action=action,
                    quantity=qty,
                    price=price,
                    fill_date=today,
                    entry_signals=signal.get("entry_signals"),
                    exit_reason=signal.get("exit_reason"),
                )

                if action == "buy":
                    print(f"  BUY  {ticker:>6s}  {qty:>8.4f} @ ${price:>8.2f}  [{name}]")
                elif action == "sell":
                    reason = signal.get("exit_reason", "signal")
                    print(f"  SELL {ticker:>6s}             @ ${price:>8.2f}  [{name}] ({reason})")

        # Update peak prices and record equity snapshot
        state.update_peak_prices(name, current_prices)
        equity = state.compute_equity(name, current_prices)
        cash = state.get_cash(name)
        market_value = equity - cash
        state.record_equity_snapshot(name, today, equity, cash, market_value)

    # Record aggregate equity snapshot
    total_equity = sum(
        state.compute_equity(name, current_prices) for name in portfolios
    )
    total_cash = sum(state.get_cash(name) for name in portfolios)
    total_mv = total_equity - total_cash
    state.record_equity_snapshot("_aggregate", today, total_equity, total_cash, total_mv)

    return signals_generated
```

- [ ] **Step 4: Update main() to use DB session**

Replace the existing `main()` function:

```python
def make_db_session(db_url: str) -> Session:
    """Create a SQLAlchemy session."""
    engine = create_engine(db_url)
    factory = sessionmaker(bind=engine)
    return factory()


def main():
    parser = argparse.ArgumentParser(description="Daily paper trading runner")
    parser.add_argument("--capital", type=float, default=100_000,
                        help="Total capital (default: 100000)")
    parser.add_argument("--db-url", default=None,
                        help="Database URL (default: from config)")
    parser.add_argument("--years", type=int, default=1,
                        help="Years of historical bars for signal warmup (default: 1)")
    parser.add_argument("--init", action="store_true",
                        help="Initialize fresh paper trading state")
    parser.add_argument("--status", action="store_true",
                        help="Print current status and exit")
    parser.add_argument("--reset", action="store_true",
                        help="Wipe all paper state and re-initialize")
    parser.add_argument("--ib-host", default="127.0.0.1")
    parser.add_argument("--ib-port", type=int, default=7497)
    args = parser.parse_args()

    # Resolve DB URL
    db_url = args.db_url
    if not db_url:
        config = load_config("config/default.yaml")
        db_url = config.database.url
    session = make_db_session(db_url)

    # --reset: wipe and re-init
    if args.reset:
        confirm = input("This will delete ALL paper trading data. Type 'yes' to confirm: ")
        if confirm.strip().lower() != "yes":
            print("Aborted.")
            return
        from shared.models.portfolio import Position, Trade
        from shared.models.equity_snapshot import EquitySnapshot
        session.query(EquitySnapshot).delete()
        session.query(Trade).delete()
        session.query(Position).delete()
        session.query(PortfolioConfigModel).delete()
        session.commit()
        print("Paper trading state wiped.")
        # Fall through to --init if both flags set
        if not args.init:
            return

    # --init: create fresh state
    if args.init:
        capitals = {name: args.capital * pct for name, pct in CAPITAL_ALLOCATIONS.items()}
        state = PaperTradingState.create_new(capitals, session)
        session.commit()
        print(f"Initialized paper trading state in database")
        print(f"Total capital: ${args.capital:,.0f}")
        for name, cap in capitals.items():
            print(f"  {name}: ${cap:,.0f}")
        return

    # --status: print current state
    if args.status:
        try:
            state = PaperTradingState.load(session)
        except ValueError as e:
            print(str(e))
            sys.exit(1)
        print_status(state)
        return

    # Daily run
    try:
        state = PaperTradingState.load(session)
    except ValueError as e:
        print(str(e))
        sys.exit(1)

    print(f"Paper Trading Daily Run - {date.today()}")
    print(f"State loaded from database")

    # Fetch bars from IB
    all_tickers = get_union_universe(list(CAPITAL_ALLOCATIONS.keys()))
    print(f"\nFetching bars for {len(all_tickers)} tickers ({args.years} year)...")
    bars_by_ticker = fetch_bars_from_ib(
        tickers=all_tickers,
        years=args.years,
        host=args.ib_host,
        port=args.ib_port,
    )

    if not bars_by_ticker:
        print("ERROR: No data fetched. Is IB Gateway running?")
        sys.exit(1)

    # Load caches
    fundamentals_cache = load_fundamentals_cache("data/cache/fundamentals.json")
    earnings_cache = load_earnings_cache("data/cache/earnings.json")
    fundamentals_lookup = build_fundamentals_lookup(fundamentals_cache)
    earnings_lookup = build_earnings_lookup(earnings_cache, window_days=2)

    # Compute regime
    regime_by_date = compute_regime_by_date(bars_by_ticker)

    # Build portfolios
    portfolios = build_portfolios(
        capital=args.capital,
        bars_by_ticker=bars_by_ticker,
        regime_by_date=regime_by_date,
        fundamentals_lookup=fundamentals_lookup,
        earnings_lookup=earnings_lookup,
    )

    # Run signals
    print(f"\nRunning signals across {len(portfolios)} portfolios...")
    signals = run_daily(state, portfolios, bars_by_ticker)

    if signals:
        print(f"\n{len(signals)} signals generated")
    else:
        print("\nNo signals generated today")

    # Commit all changes
    session.commit()
    print(f"\nState committed to database")


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Run the full test suite to verify nothing is broken**

Run: `pytest tests/ -v --timeout=60`
Expected: All tests pass. Some tests may need adjustment if they imported `PaperTradingState` with old constructor.

- [ ] **Step 6: Commit**

```bash
git add scripts/run_paper.py
git commit -m "feat: update paper trading runner to use DB-backed state"
```

---

### Task 6: Build Automated Retraining Script

**Files:**
- Rewrite: `scripts/retrain_model.py`
- Create: `tests/backtest/test_retrain_model.py`

- [ ] **Step 1: Write failing tests for the retraining pipeline**

Create `tests/backtest/test_retrain_model.py`:

```python
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from shared.models.base import Base
from shared.models.ml_models import ModelVersion
from scripts.paper_state import PaperTradingState
from scripts.retrain_model import (
    load_trades_from_db,
    compare_models,
    run_retraining_pipeline,
)


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    session = factory()
    yield session
    session.close()


def _seed_trades(session: Session, n: int = 250) -> None:
    """Seed DB with n trades across two portfolios."""
    state = PaperTradingState.create_new(
        portfolio_capitals={"mr": 50_000, "mom": 50_000},
        session=session,
    )
    import random
    random.seed(42)
    tickers = ["AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA", "JPM"]

    for i in range(n):
        portfolio = "mr" if i % 2 == 0 else "mom"
        ticker = tickers[i % len(tickers)]
        entry_price = 100 + random.uniform(-20, 20)
        exit_price = entry_price + random.uniform(-10, 15)
        entry_d = date(2023, 1, 1 + (i % 28))
        exit_d = date(2023, 2, 1 + (i % 28))

        state.record_fill(
            portfolio=portfolio, ticker=ticker, action="buy",
            quantity=10, price=entry_price, fill_date=entry_d,
            entry_signals={"rsi": random.uniform(20, 80), "rank": random.randint(1, 10)},
        )
        state.record_fill(
            portfolio=portfolio, ticker=ticker, action="sell",
            quantity=10, price=exit_price, fill_date=exit_d,
            exit_reason="trailing_stop",
            bar_features={"return_5d": random.uniform(-0.1, 0.1), "vol_20d": random.uniform(0.01, 0.05)},
        )

    session.flush()


def test_load_trades_from_db(db_session: Session):
    """load_trades_from_db should return trade dicts with ML-ready fields."""
    _seed_trades(db_session, n=50)
    trades = load_trades_from_db(db_session)
    assert len(trades) == 50
    for t in trades:
        assert "pnl" in t
        assert "entry_date" in t
        assert "portfolio" in t
        assert "entry_signals" in t


def test_load_trades_filters_null_signals(db_session: Session):
    """Trades without entry_signals should be excluded."""
    state = PaperTradingState.create_new(
        portfolio_capitals={"mr": 10_000},
        session=db_session,
    )
    # Buy without entry_signals
    state.record_fill("mr", "AAPL", "buy", 10, 150.0, date(2024, 1, 1))
    state.record_fill("mr", "AAPL", "sell", 10, 160.0, date(2024, 1, 15))
    # Buy with entry_signals
    state.record_fill("mr", "MSFT", "buy", 10, 300.0, date(2024, 1, 1),
                       entry_signals={"rsi": 30})
    state.record_fill("mr", "MSFT", "sell", 10, 310.0, date(2024, 1, 15))
    db_session.flush()

    trades = load_trades_from_db(db_session)
    assert len(trades) == 1
    assert trades[0]["ticker"] == "MSFT"


def test_compare_models_promote_when_better():
    """New model should be promoted when metrics improve."""
    current = {"accuracy": 0.55, "filtered_win_rate": 0.60}
    new = {"accuracy": 0.58, "filtered_win_rate": 0.62}
    assert compare_models(current_metrics=current, new_metrics=new) is True


def test_compare_models_skip_when_worse():
    """New model should be skipped when accuracy regresses."""
    current = {"accuracy": 0.60, "filtered_win_rate": 0.65}
    new = {"accuracy": 0.55, "filtered_win_rate": 0.63}
    assert compare_models(current_metrics=current, new_metrics=new) is False


def test_compare_models_promote_when_no_current():
    """First model should always be promoted."""
    assert compare_models(current_metrics=None, new_metrics={"accuracy": 0.5, "filtered_win_rate": 0.5}) is True


def test_pipeline_skips_when_too_few_trades(db_session: Session):
    """Pipeline should skip if trade count < min_training_samples."""
    _seed_trades(db_session, n=10)
    result = run_retraining_pipeline(
        session=db_session, min_samples=200, output_dir="/tmp/test_models",
        dry_run=True,
    )
    assert result["status"] == "skipped"
    assert "insufficient" in result["reason"].lower()


def test_pipeline_trains_and_evaluates(db_session: Session):
    """Pipeline should train model and return metrics when enough trades."""
    _seed_trades(db_session, n=250)
    result = run_retraining_pipeline(
        session=db_session, min_samples=50, output_dir="/tmp/test_models",
        dry_run=True,
    )
    assert result["status"] in ("promoted", "skipped", "dry_run")
    assert "accuracy" in result["metrics"]
    assert "filtered_win_rate" in result["metrics"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/backtest/test_retrain_model.py -v`
Expected: FAIL — `ImportError: cannot import name 'load_trades_from_db'`

- [ ] **Step 3: Implement retrain_model.py**

Rewrite `scripts/retrain_model.py`:

```python
#!/usr/bin/env python3
"""Automated model retraining pipeline.

Queries completed trades from PostgreSQL, trains a LightGBM signal
quality model, and promotes it only if it outperforms the current
active model.

Usage:
    python scripts/retrain_model.py                    # Run pipeline
    python scripts/retrain_model.py --force            # Always promote
    python scripts/retrain_model.py --dry-run          # Evaluate only
    python scripts/retrain_model.py --db-url <url>     # Override DB URL
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from backtest.feature_extractor import extract_features
from scripts.train_signal_model import (
    _prepare_for_lgb,
    train_final_model,
    walk_forward_evaluate,
)
from shared.models.base import Base
from shared.models.ml_models import ModelVersion
from shared.models.portfolio import Trade


def load_trades_from_db(session: Session) -> list[dict]:
    """Load completed trades with entry_signals from the database."""
    rows = session.execute(
        select(Trade)
        .where(Trade.entry_signals.isnot(None))
        .order_by(Trade.executed_at)
    ).scalars().all()

    return [
        {
            "ticker": t.ticker,
            "portfolio": t.portfolio,
            "entry_price": t.entry_price,
            "exit_price": t.price,
            "quantity": t.quantity,
            "entry_date": str(t.entry_date),
            "exit_date": str(t.executed_at.date()),
            "pnl": t.pnl,
            "exit_reason": t.exit_reason,
            "entry_signals": t.entry_signals or {},
            "bar_features": t.bar_features or {},
        }
        for t in rows
    ]


def get_active_model_metrics(session: Session) -> dict | None:
    """Load metrics from the currently active model version."""
    row = session.execute(
        select(ModelVersion).where(ModelVersion.is_active == True)
    ).scalar_one_or_none()

    if row is None:
        return None
    return row.metrics


def compare_models(
    current_metrics: dict | None,
    new_metrics: dict,
) -> bool:
    """Return True if new model should replace current model.

    Promotes if:
    - No current model exists, OR
    - New accuracy >= current accuracy AND new filtered_win_rate >= current
    """
    if current_metrics is None:
        return True

    return (
        new_metrics["accuracy"] >= current_metrics.get("accuracy", 0)
        and new_metrics["filtered_win_rate"] >= current_metrics.get("filtered_win_rate", 0)
    )


def run_retraining_pipeline(
    session: Session,
    min_samples: int = 200,
    output_dir: str = "data/models",
    n_splits: int = 3,
    dry_run: bool = False,
    force: bool = False,
) -> dict:
    """Run the full retraining pipeline. Returns a result dict."""
    # Step 1: Load trades
    trades = load_trades_from_db(session)
    print(f"Loaded {len(trades)} trades with entry_signals from DB")

    # Step 2: Sample check
    if len(trades) < min_samples:
        reason = f"Insufficient trades: {len(trades)} < {min_samples} min_training_samples"
        print(f"SKIP: {reason}")
        return {"status": "skipped", "reason": reason}

    # Step 3: Extract features
    features, labels = extract_features(trades)
    print(f"Feature matrix: {features.shape[0]} x {features.shape[1]}")
    print(f"Baseline win rate: {labels.mean():.1%}")

    # Step 4: Walk-forward evaluation
    dates = pd.to_datetime(pd.Series([t["entry_date"] for t in trades]))
    fold_results = walk_forward_evaluate(features, labels, dates, n_splits)

    if not fold_results:
        reason = "Walk-forward produced no valid folds"
        print(f"SKIP: {reason}")
        return {"status": "skipped", "reason": reason}

    avg_accuracy = float(np.mean([r["accuracy"] for r in fold_results]))
    avg_filtered_wr = float(np.mean([r["filtered_win_rate"] for r in fold_results]))
    avg_baseline_wr = float(np.mean([r["baseline_win_rate"] for r in fold_results]))

    print(f"\nWalk-forward results ({len(fold_results)} folds):")
    for r in fold_results:
        print(f"  Fold {r['fold']}: acc={r['accuracy']:.1%}, "
              f"filtered_wr={r['filtered_win_rate']:.1%}, "
              f"baseline_wr={r['baseline_win_rate']:.1%}")
    print(f"  Avg accuracy: {avg_accuracy:.1%}")
    print(f"  Avg filtered WR: {avg_filtered_wr:.1%}")
    print(f"  Avg baseline WR: {avg_baseline_wr:.1%}")

    new_metrics = {
        "accuracy": avg_accuracy,
        "filtered_win_rate": avg_filtered_wr,
        "baseline_win_rate": avg_baseline_wr,
        "walk_forward_folds": fold_results,
        "total_trades": len(trades),
    }

    if dry_run:
        print("\nDRY RUN — no model saved or promoted")
        return {"status": "dry_run", "metrics": new_metrics}

    # Step 5: Train final model
    print("\nTraining final model on all data...")
    model = train_final_model(features, labels)

    # Feature importance
    importance = model.feature_importance(importance_type="gain")
    feature_names = features.columns.tolist()
    imp_sorted = sorted(zip(feature_names, importance), key=lambda x: x[1], reverse=True)
    new_metrics["feature_importance"] = {n: float(i) for n, i in imp_sorted}

    print("Top 5 features:")
    for name, imp in imp_sorted[:5]:
        print(f"  {name}: {imp:.1f}")

    # Step 6: Promotion gate
    current_metrics = get_active_model_metrics(session)
    should_promote = force or compare_models(current_metrics, new_metrics)

    # Step 7: Save model file
    version = f"v{date.today().isoformat()}"
    os.makedirs(output_dir, exist_ok=True)
    model_path = os.path.join(output_dir, f"signal_quality_{version}.txt")
    model.save_model(model_path)

    # Determine training window
    entry_dates = [t["entry_date"] for t in trades]
    window_start = min(entry_dates)
    window_end = max(entry_dates)

    # Step 8: Record in DB
    now = datetime.now(timezone.utc)
    if should_promote:
        # Deactivate current active model
        current_active = session.execute(
            select(ModelVersion).where(ModelVersion.is_active == True)
        ).scalar_one_or_none()
        if current_active:
            current_active.is_active = False

    model_version = ModelVersion(
        version=version,
        training_window_start=date.fromisoformat(window_start) if isinstance(window_start, str) else window_start,
        training_window_end=date.fromisoformat(window_end) if isinstance(window_end, str) else window_end,
        metrics=new_metrics,
        model_path=model_path,
        is_active=should_promote,
        created_at=now,
    )
    session.add(model_version)
    session.flush()

    # Step 9: Summary
    if should_promote:
        status = "promoted"
        # Also save as the "latest" symlink-like path for the paper trader
        latest_path = os.path.join(output_dir, "signal_quality_model.txt")
        model.save_model(latest_path)
        print(f"\nMODEL PROMOTED: {version}")
        print(f"  Saved to: {model_path}")
        print(f"  Active model updated: {latest_path}")
    else:
        status = "skipped"
        print(f"\nMODEL NOT PROMOTED: {version}")
        if current_metrics:
            print(f"  Current: acc={current_metrics.get('accuracy', 0):.1%}, "
                  f"wr={current_metrics.get('filtered_win_rate', 0):.1%}")
        print(f"  New:     acc={avg_accuracy:.1%}, wr={avg_filtered_wr:.1%}")

    return {"status": status, "version": version, "metrics": new_metrics}


def main():
    parser = argparse.ArgumentParser(
        description="Automated signal quality model retraining"
    )
    parser.add_argument("--db-url", default=None,
                        help="Database URL (default: from config)")
    parser.add_argument("--output-dir", default="data/models",
                        help="Directory to save model (default: data/models)")
    parser.add_argument("--min-samples", type=int, default=200,
                        help="Minimum trades required (default: 200)")
    parser.add_argument("--n-splits", type=int, default=3,
                        help="Walk-forward splits (default: 3)")
    parser.add_argument("--force", action="store_true",
                        help="Skip promotion gate, always promote")
    parser.add_argument("--dry-run", action="store_true",
                        help="Evaluate but don't save or promote")
    args = parser.parse_args()

    from shared.config import load_config

    db_url = args.db_url
    if not db_url:
        config = load_config("config/default.yaml")
        db_url = config.database.url

    engine = create_engine(db_url)
    session = sessionmaker(bind=engine)()

    try:
        result = run_retraining_pipeline(
            session=session,
            min_samples=args.min_samples,
            output_dir=args.output_dir,
            n_splits=args.n_splits,
            dry_run=args.dry_run,
            force=args.force,
        )
        session.commit()
        print(f"\nPipeline complete: {result['status']}")
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/backtest/test_retrain_model.py -v`
Expected: All PASS

- [ ] **Step 5: Run the full test suite**

Run: `pytest tests/ -v --timeout=60`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add scripts/retrain_model.py tests/backtest/test_retrain_model.py
git commit -m "feat: add automated model retraining with promotion gate"
```

---

### Task 7: Final Integration Verification

**Files:** None (verification only)

- [ ] **Step 1: Run full test suite**

Run: `pytest tests/ -v`
Expected: All tests pass

- [ ] **Step 2: Verify migration file imports cleanly**

Run: `python -c "from shared.models import Position, Trade, EquitySnapshot, PortfolioConfig, ModelVersion; print('All models import OK')"`
Expected: `All models import OK`

- [ ] **Step 3: Verify paper trading CLI help**

Run: `python scripts/run_paper.py --help`
Expected: Shows `--db-url`, `--init`, `--status`, `--reset` flags (no `--state-file`)

- [ ] **Step 4: Verify retraining CLI help**

Run: `python scripts/retrain_model.py --help`
Expected: Shows `--db-url`, `--force`, `--dry-run`, `--min-samples` flags

- [ ] **Step 5: Commit any final fixes if needed**

```bash
git add -u
git commit -m "fix: integration cleanup for DB persistence and retraining"
```
