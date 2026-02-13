from __future__ import annotations

import os
import tempfile
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from services.ml_model.runner import MLServiceRunner
from services.ml_model.trainer import ModelTrainer
from shared.config import AppConfig, MLModelConfig
from shared.schemas.messages import SignalMessage, RecommendationMessage


SIGNAL_NAMES = [
    "support_proximity",
    "support_strength",
    "support_trend",
    "valuation",
    "quality",
    "growth",
    "earnings_surprise",
    "news_sentiment",
    "insider_activity",
]


def _make_signal(
    ticker: str,
    name: str,
    value: float = 0.5,
    confidence: float = 0.9,
) -> SignalMessage:
    now = datetime.now(timezone.utc)
    return SignalMessage(
        ticker=ticker,
        timestamp=now,
        signal_name=name,
        signal_value=value,
        confidence=confidence,
        computed_at=now,
    )


def _make_complete_signals(ticker: str = "AAPL") -> list[SignalMessage]:
    return [_make_signal(ticker, name, value=0.1 * i) for i, name in enumerate(SIGNAL_NAMES)]


@pytest.fixture()
def trained_model():
    np.random.seed(42)
    n = 300
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
    targets = pd.Series(np.random.choice(["sell", "hold", "buy"], n))
    trainer = ModelTrainer()
    model, _ = trainer.train(features, targets)
    return model


@pytest.fixture()
def model_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture()
def mock_config():
    config = MagicMock(spec=AppConfig)
    config.ml_model = MLModelConfig()
    return config


@pytest.fixture()
def mock_redis():
    redis = AsyncMock()
    redis.publish = AsyncMock()
    return redis


@pytest.fixture()
def mock_db(trained_model, model_dir):
    """Set up a mock DB with a saved active model."""
    import joblib

    session = MagicMock()
    model_path = os.path.join(model_dir, "v1.0.0.joblib")
    joblib.dump(trained_model, model_path)

    active_record = MagicMock()
    active_record.version = "v1.0.0"
    active_record.model_path = model_path
    active_record.is_active = True

    def query_side_effect(model_class):
        q = MagicMock()
        def filter_by_side_effect(**kwargs):
            f = MagicMock()
            if kwargs.get("is_active"):
                f.first.return_value = active_record
            else:
                f.first.return_value = active_record
            return f
        q.filter_by = filter_by_side_effect
        q.all.return_value = [active_record]
        return q

    session.query.side_effect = query_side_effect
    return session


class TestMLServiceRunner:
    @pytest.mark.asyncio
    async def test_process_signals_returns_recommendation(
        self, mock_config, mock_redis, mock_db, model_dir
    ):
        runner = MLServiceRunner(
            config=mock_config,
            redis_client=mock_redis,
            db_session=mock_db,
            model_dir=model_dir,
        )
        signals = _make_complete_signals("AAPL")
        result = await runner.process_signals("AAPL", signals)

        assert result is not None
        assert isinstance(result, RecommendationMessage)
        assert result.ticker == "AAPL"
        assert result.action in ("buy", "sell", "hold")
        assert 0.0 <= result.confidence <= 1.0

    @pytest.mark.asyncio
    async def test_process_signals_publishes_to_redis(
        self, mock_config, mock_redis, mock_db, model_dir
    ):
        runner = MLServiceRunner(
            config=mock_config,
            redis_client=mock_redis,
            db_session=mock_db,
            model_dir=model_dir,
        )
        signals = _make_complete_signals("AAPL")
        await runner.process_signals("AAPL", signals)

        mock_redis.publish.assert_called_once()
        call_args = mock_redis.publish.call_args
        assert call_args[0][0] == "stream:recommendations"

    @pytest.mark.asyncio
    async def test_process_signals_incomplete_returns_none(
        self, mock_config, mock_redis, mock_db, model_dir
    ):
        runner = MLServiceRunner(
            config=mock_config,
            redis_client=mock_redis,
            db_session=mock_db,
            model_dir=model_dir,
        )
        # Only 5 signals
        signals = [_make_signal("AAPL", name) for name in SIGNAL_NAMES[:5]]
        result = await runner.process_signals("AAPL", signals)

        assert result is None
        mock_redis.publish.assert_not_called()

    @pytest.mark.asyncio
    async def test_recommendation_has_top_features(
        self, mock_config, mock_redis, mock_db, model_dir
    ):
        runner = MLServiceRunner(
            config=mock_config,
            redis_client=mock_redis,
            db_session=mock_db,
            model_dir=model_dir,
        )
        signals = _make_complete_signals("AAPL")
        result = await runner.process_signals("AAPL", signals)

        assert result is not None
        assert isinstance(result.top_features, dict)
        assert len(result.top_features) > 0

    @pytest.mark.asyncio
    async def test_recommendation_has_unique_id(
        self, mock_config, mock_redis, mock_db, model_dir
    ):
        runner = MLServiceRunner(
            config=mock_config,
            redis_client=mock_redis,
            db_session=mock_db,
            model_dir=model_dir,
        )
        signals = _make_complete_signals("AAPL")
        r1 = await runner.process_signals("AAPL", signals)
        r2 = await runner.process_signals("AAPL", signals)

        assert r1 is not None and r2 is not None
        assert r1.recommendation_id != r2.recommendation_id

    @pytest.mark.asyncio
    async def test_regime_adjusts_confidence(
        self, mock_config, mock_redis, mock_db, model_dir
    ):
        """When regime detection is enabled, confidence should be adjusted
        by the regime familiarity score."""
        mock_config.ml_model.regime_detection_enabled = True
        runner = MLServiceRunner(
            config=mock_config,
            redis_client=mock_redis,
            db_session=mock_db,
            model_dir=model_dir,
        )
        signals = _make_complete_signals("AAPL")

        # Patch regime detector to return low familiarity
        with patch.object(
            runner._regime, "detect", return_value=("sideways", 0.5)
        ):
            result = await runner.process_signals("AAPL", signals)

        assert result is not None
        # Confidence should be adjusted (reduced) by low familiarity
        assert result.confidence <= 1.0

    @pytest.mark.asyncio
    async def test_regime_disabled_no_adjustment(
        self, mock_config, mock_redis, mock_db, model_dir
    ):
        """When regime detection is disabled, confidence should not be
        adjusted by familiarity."""
        mock_config.ml_model.regime_detection_enabled = False
        runner = MLServiceRunner(
            config=mock_config,
            redis_client=mock_redis,
            db_session=mock_db,
            model_dir=model_dir,
        )
        signals = _make_complete_signals("AAPL")
        result = await runner.process_signals("AAPL", signals)

        assert result is not None
        assert 0.0 <= result.confidence <= 1.0
