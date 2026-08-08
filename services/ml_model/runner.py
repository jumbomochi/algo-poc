from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from shared.config import AppConfig
from shared.logging import get_logger
from shared.schemas.messages import RecommendationMessage, SignalMessage

from services.ml_model.feature_assembly import FeatureAssembler
from services.ml_model.predictor import ModelPredictor
from services.ml_model.regime import RegimeDetector
from services.ml_model.registry import ModelRegistry

logger = get_logger("ml_model")

RECOMMENDATIONS_STREAM = "stream:recommendations"
SIGNALS_STREAM = "stream:signals"
CONSUMER_GROUP = "ml_model"
CONSUMER_NAME = "ml_worker_1"


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
        # Signals buffer per ticker until a full feature vector is assembled.
        # Their message ids are held so they can be acked ONLY once the buffer
        # is durably consumed (a recommendation published) — not on arrival.
        self._signal_buffer: dict[str, list[SignalMessage]] = {}
        self._buffered_message_ids: dict[str, list[str]] = {}

    async def setup(self) -> None:
        """Create the consumer group and replay delivered-but-unacked signals.

        Without the replay, signals in flight (or buffered but not yet
        aggregated) at a crash are lost — the normal ``">"`` read never
        re-delivers them.
        """
        await self._redis.create_consumer_group(SIGNALS_STREAM, CONSUMER_GROUP)
        pending = await self._redis.drain_pending(
            SIGNALS_STREAM, CONSUMER_GROUP, CONSUMER_NAME
        )
        for msg in pending:
            await self._handle_signal(msg)

    async def consume_once(self, *, count: int = 10, block_ms: int = 2000) -> None:
        """Read one batch of signals and process each."""
        messages = await self._redis.read_group(
            SIGNALS_STREAM, CONSUMER_GROUP, CONSUMER_NAME, count=count, block_ms=block_ms
        )
        for msg in messages:
            await self._handle_signal(msg)

    async def _handle_signal(self, msg: Any) -> None:
        """Buffer a signal and, when it completes a feature vector, publish the
        recommendation and ack the whole buffer. Incomplete signals are left
        pending (un-acked) so a restart's drain rebuilds the buffer. Poison
        messages are dead-lettered + acked."""
        try:
            signal = SignalMessage.from_stream_dict(msg.data)
        except Exception as exc:
            await self._dead_letter(msg, exc)
            return

        ticker = signal.ticker
        self._signal_buffer.setdefault(ticker, []).append(signal)
        self._buffered_message_ids.setdefault(ticker, []).append(msg.message_id)
        try:
            result = await self.process_signals(ticker, self._signal_buffer[ticker])
        except Exception:
            # A processing failure (e.g. no active model yet) is transient, not
            # poison — keep the signal buffered and leave it pending so a later
            # signal (or a restart's drain) retries the whole buffer rather than
            # dead-lettering a perfectly valid signal.
            logger.warning(
                "process_signals failed; leaving signal buffered + pending",
                ticker=ticker,
                message_id=msg.message_id,
            )
            return

        if result is not None:
            # The buffer was durably consumed (recommendation published) — only
            # now is it safe to ack every message that contributed to it.
            for mid in self._buffered_message_ids[ticker]:
                await self._redis.ack(SIGNALS_STREAM, CONSUMER_GROUP, mid)
            self._signal_buffer[ticker] = []
            self._buffered_message_ids[ticker] = []
        # else: incomplete — leave pending so a restart replays it.

    async def _dead_letter(self, msg: Any, exc: Exception) -> None:
        await self._redis.send_to_dead_letter(SIGNALS_STREAM, msg, str(exc))
        await self._redis.ack(SIGNALS_STREAM, CONSUMER_GROUP, msg.message_id)

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

        redis_conn = aioredis.from_url(config.redis.url)
        redis_client = RedisStreamClient(redis_conn)
        runner = MLServiceRunner(
            config=config, redis_client=redis_client, db_session=None
        )

        await runner.setup()  # create group + replay pending signals

        logger.info("ML model service started", mode=config.mode)

        while True:
            await runner.consume_once(count=10, block_ms=2000)

    asyncio.run(main())
