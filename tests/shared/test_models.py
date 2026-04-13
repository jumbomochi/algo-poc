from datetime import datetime, timezone
from sqlalchemy import Float
from shared.models.base import Base
from shared.models.market_data import OHLCVDaily
from shared.models.portfolio import Position, Trade
from shared.models.equity_snapshot import EquitySnapshot
from shared.models.portfolio_config import PortfolioConfig
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
    assert cols >= {
        "id", "ticker", "quantity", "avg_entry_price", "sector",
        "opened_at", "status", "portfolio", "peak_price", "entry_signals",
    }


def test_position_quantity_is_float():
    col = Position.__table__.columns["quantity"]
    assert isinstance(col.type, Float)


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
    assert issubclass(EquitySnapshot, Base)
    assert issubclass(PortfolioConfig, Base)


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


def test_portfolio_config_has_required_fields():
    cols = {c.name for c in PortfolioConfig.__table__.columns}
    assert cols >= {
        "id", "portfolio", "capital", "cash", "created_at", "updated_at",
    }


def test_portfolio_config_portfolio_is_unique():
    col = PortfolioConfig.__table__.columns["portfolio"]
    assert col.unique is True
