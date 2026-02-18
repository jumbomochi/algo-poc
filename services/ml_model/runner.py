from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from shared.config import AppConfig
from shared.schemas.messages import RecommendationMessage, SignalMessage

from services.ml_model.feature_assembly import FeatureAssembler
from services.ml_model.predictor import ModelPredictor
from services.ml_model.regime import RegimeDetector
from services.ml_model.registry import ModelRegistry

RECOMMENDATIONS_STREAM = "stream:recommendations"


class MLServiceRunner:
    """Orchestrates the ML model service pipeline.

    1. Assembles feature vectors from incoming signals.
    2. Loads the active model from the registry (lazy).
    3. Predicts action/confidence/top_features.
    4. Adjusts confidence by regime familiarity (if enabled).
    5. Creates and publishes a RecommendationMessage.
    """

    def __init__(
        self,
        config: AppConfig,
        redis_client: Any,
        db_session: Any,
        model_dir: str = "models/",
    ):
        self._config = config
        self._redis = redis_client
        self._db = db_session
        self._assembler = FeatureAssembler()
        self._registry = ModelRegistry(db_session, model_dir)
        self._regime = RegimeDetector()
        self._predictor: ModelPredictor | None = None

    def _ensure_predictor_loaded(self) -> ModelPredictor:
        """Lazily load the active model and create a predictor."""
        if self._predictor is None:
            model, version = self._registry.load_active()
            self._predictor = ModelPredictor(model)
        return self._predictor

    async def process_signals(
        self,
        ticker: str,
        signals: list[SignalMessage],
    ) -> RecommendationMessage | None:
        """Process a batch of signals for a ticker and produce a recommendation.

        Args:
            ticker: The stock ticker.
            signals: List of SignalMessage instances.

        Returns:
            RecommendationMessage if a complete feature vector could be
            assembled and prediction was made, or None if incomplete.
        """
        # 1. Assemble features
        features = self._assembler.assemble(ticker, signals)
        if features is None:
            return None

        # 2. Load model if not loaded
        predictor = self._ensure_predictor_loaded()

        # 3. Predict
        action, confidence, top_features = predictor.predict(features)

        # 4. Adjust confidence by regime familiarity (if enabled)
        if self._config.ml_model.regime_detection_enabled:
            # Extract returns/volatilities from signal values as proxy
            # In production, these would come from market data
            returns = [
                features.get("support_proximity", 0.0),
                features.get("support_trend", 0.0),
                features.get("growth", 0.0),
            ]
            volatilities = [
                abs(features.get("support_strength", 0.0)),
                abs(features.get("earnings_surprise", 0.0)),
                abs(features.get("news_sentiment", 0.0)),
            ]
            _, familiarity = self._regime.detect(returns, volatilities)
            confidence = confidence * familiarity

        # 5. Create RecommendationMessage
        recommendation = RecommendationMessage(
            ticker=ticker,
            timestamp=datetime.now(timezone.utc),
            action=action,
            confidence=confidence,
            top_features=top_features,
            recommendation_id=str(uuid.uuid4()),
        )

        # 6. Publish to stream:recommendations
        await self._redis.publish(
            RECOMMENDATIONS_STREAM,
            recommendation.to_stream_dict(),
        )

        return recommendation


if __name__ == "__main__":
    import asyncio

    from shared.config import load_config
    from shared.logging import get_logger

    config = load_config("config/default.yaml")
    logger = get_logger("ml_model")

    async def main() -> None:
        import redis.asyncio as aioredis

        from shared.redis_client import RedisStreamClient
        from shared.schemas.messages import SignalMessage

        redis_conn = aioredis.from_url(config.redis.url)
        redis_client = RedisStreamClient(redis_conn)
        runner = MLServiceRunner(
            config=config, redis_client=redis_client, db_session=None
        )

        STREAM = "stream:signals"
        GROUP = "ml_model"
        CONSUMER = "ml_worker_1"
        await redis_client.create_consumer_group(STREAM, GROUP)

        logger.info("ML model service started", mode=config.mode)

        # Buffer signals per ticker
        signal_buffer: dict[str, list[SignalMessage]] = {}

        while True:
            messages = await redis_client.read_group(
                STREAM, GROUP, CONSUMER, count=10, block_ms=2000
            )
            for msg in messages:
                try:
                    signal = SignalMessage.from_stream_dict(msg.data)
                    signal_buffer.setdefault(signal.ticker, []).append(signal)

                    # Try to process when we have enough signals
                    result = await runner.process_signals(
                        signal.ticker, signal_buffer[signal.ticker]
                    )
                    if result is not None:
                        signal_buffer[signal.ticker] = []

                    await redis_client.ack(STREAM, GROUP, msg.message_id)
                except Exception:
                    logger.exception("ml_processing_error")

    asyncio.run(main())
