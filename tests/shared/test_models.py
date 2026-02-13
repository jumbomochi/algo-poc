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
