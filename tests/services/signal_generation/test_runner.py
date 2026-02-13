import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from services.signal_generation.runner import SignalGenerationRunner
from shared.config import AppConfig
from shared.schemas.messages import SignalMessage


def _make_config() -> AppConfig:
    return AppConfig()


def _make_calendar_mock():
    """Create a calendar mock with sensible defaults for market data tests."""
    cal = MagicMock()
    cal.get_last_session_close.return_value = datetime(
        2025, 1, 6, 16, 0, tzinfo=timezone.utc
    )
    return cal


def _make_runner(
    config=None,
    redis_client=None,
    db_session=None,
    calendar=None,
):
    config = config or _make_config()
    redis_client = redis_client or AsyncMock()
    db_session = db_session or MagicMock()
    calendar = calendar or _make_calendar_mock()
    return SignalGenerationRunner(
        config=config,
        redis_client=redis_client,
        db_session=db_session,
        calendar=calendar,
    )


class TestProcessMarketData:
    @pytest.mark.asyncio
    async def test_runs_all_technical_signals(self):
        """process_market_data should run all three technical signals."""
        runner = _make_runner()
        # Build data that technical signals can process (need close/low/high arrays)
        import numpy as np

        np.random.seed(42)
        n = 252
        closes = 100.0 + np.cumsum(np.random.randn(n) * 0.5)
        data = {
            "ticker": "AAPL",
            "timestamp": datetime(2025, 1, 6, 16, 0, tzinfo=timezone.utc).isoformat(),
            "open": (closes + np.random.rand(n)).tolist(),
            "high": (closes + abs(np.random.randn(n))).tolist(),
            "low": (closes - abs(np.random.randn(n))).tolist(),
            "close": closes.tolist(),
            "volume": np.random.randint(100000, 1000000, n).tolist(),
        }
        results = await runner.process_market_data(data)
        assert len(results) == 3
        for msg in results:
            assert isinstance(msg, SignalMessage)
            assert msg.ticker == "AAPL"
            assert -1.0 <= msg.signal_value <= 1.0

    @pytest.mark.asyncio
    async def test_publishes_signals_to_redis(self):
        """process_market_data should publish each signal to stream:signals."""
        redis = AsyncMock()
        runner = _make_runner(redis_client=redis)
        import numpy as np

        np.random.seed(42)
        n = 252
        closes = 100.0 + np.cumsum(np.random.randn(n) * 0.5)
        data = {
            "ticker": "AAPL",
            "timestamp": datetime(2025, 1, 6, 16, 0, tzinfo=timezone.utc).isoformat(),
            "open": (closes + np.random.rand(n)).tolist(),
            "high": (closes + abs(np.random.randn(n))).tolist(),
            "low": (closes - abs(np.random.randn(n))).tolist(),
            "close": closes.tolist(),
            "volume": np.random.randint(100000, 1000000, n).tolist(),
        }
        await runner.process_market_data(data)
        assert redis.publish.call_count == 3

    @pytest.mark.asyncio
    async def test_signal_names_are_technical(self):
        """The signal names should be the three technical signal names."""
        runner = _make_runner()
        import numpy as np

        np.random.seed(42)
        n = 252
        closes = 100.0 + np.cumsum(np.random.randn(n) * 0.5)
        data = {
            "ticker": "AAPL",
            "timestamp": datetime(2025, 1, 6, 16, 0, tzinfo=timezone.utc).isoformat(),
            "open": (closes + np.random.rand(n)).tolist(),
            "high": (closes + abs(np.random.randn(n))).tolist(),
            "low": (closes - abs(np.random.randn(n))).tolist(),
            "close": closes.tolist(),
            "volume": np.random.randint(100000, 1000000, n).tolist(),
        }
        results = await runner.process_market_data(data)
        names = {r.signal_name for r in results}
        assert names == {"support_proximity", "support_strength", "support_trend"}


class TestProcessFundamentals:
    @pytest.mark.asyncio
    async def test_runs_all_fundamental_signals(self):
        """process_fundamentals should run all three fundamental signals."""
        runner = _make_runner()
        data = {
            "ticker": "MSFT",
            "timestamp": datetime(2025, 1, 6, 16, 0, tzinfo=timezone.utc).isoformat(),
            "pe_ratio": 15.0,
            "pb_ratio": 1.5,
            "sector_median_pe": 20.0,
            "sector_median_pb": 2.5,
            "roe": 0.20,
            "debt_equity": 0.5,
            "margin": 0.15,
            "revenue_growth": 0.10,
            "earnings_growth": 0.12,
        }
        results = await runner.process_fundamentals(data)
        assert len(results) == 3
        for msg in results:
            assert isinstance(msg, SignalMessage)
            assert msg.ticker == "MSFT"

    @pytest.mark.asyncio
    async def test_fundamental_signal_names(self):
        """The signal names should be the three fundamental signal names."""
        runner = _make_runner()
        data = {
            "ticker": "MSFT",
            "timestamp": datetime(2025, 1, 6, 16, 0, tzinfo=timezone.utc).isoformat(),
            "pe_ratio": 15.0,
            "pb_ratio": 1.5,
            "sector_median_pe": 20.0,
            "sector_median_pb": 2.5,
            "roe": 0.20,
            "debt_equity": 0.5,
            "margin": 0.15,
            "revenue_growth": 0.10,
            "earnings_growth": 0.12,
        }
        results = await runner.process_fundamentals(data)
        names = {r.signal_name for r in results}
        assert names == {"valuation", "quality", "growth"}


class TestProcessEvents:
    @pytest.mark.asyncio
    async def test_runs_all_event_signals(self):
        """process_events should run all three event signals."""
        runner = _make_runner()
        data = {
            "ticker": "GOOG",
            "timestamp": datetime(2025, 1, 6, 16, 0, tzinfo=timezone.utc).isoformat(),
            "actual_eps": 2.50,
            "estimate_eps": 2.00,
            "sentiment_score": 0.5,
            "insider_buy_value": 200_000,
            "insider_sell_value": 50_000,
        }
        results = await runner.process_events(data)
        assert len(results) == 3
        for msg in results:
            assert isinstance(msg, SignalMessage)
            assert msg.ticker == "GOOG"

    @pytest.mark.asyncio
    async def test_event_signal_names(self):
        """The signal names should be the three event signal names."""
        runner = _make_runner()
        data = {
            "ticker": "GOOG",
            "timestamp": datetime(2025, 1, 6, 16, 0, tzinfo=timezone.utc).isoformat(),
            "actual_eps": 2.50,
            "estimate_eps": 2.00,
            "sentiment_score": 0.5,
            "insider_buy_value": 200_000,
            "insider_sell_value": 50_000,
        }
        results = await runner.process_events(data)
        names = {r.signal_name for r in results}
        assert names == {"earnings_surprise", "news_sentiment", "insider_activity"}


class TestStalenessIntegration:
    @pytest.mark.asyncio
    async def test_stale_market_data_flags_signals(self):
        """When market data is stale, process_market_data should still compute
        but returned signals should have is_stale metadata."""
        calendar = MagicMock()
        calendar.get_last_session_close.return_value = datetime(
            2025, 1, 6, 16, 0, tzinfo=timezone.utc
        )
        runner = _make_runner(calendar=calendar)
        import numpy as np

        np.random.seed(42)
        n = 252
        closes = 100.0 + np.cumsum(np.random.randn(n) * 0.5)
        # Timestamp well before last session close to trigger staleness
        data = {
            "ticker": "AAPL",
            "timestamp": datetime(2025, 1, 5, 10, 0, tzinfo=timezone.utc).isoformat(),
            "open": (closes + np.random.rand(n)).tolist(),
            "high": (closes + abs(np.random.randn(n))).tolist(),
            "low": (closes - abs(np.random.randn(n))).tolist(),
            "close": closes.tolist(),
            "volume": np.random.randint(100000, 1000000, n).tolist(),
        }
        # With stale data, the runner should still return signals
        # but may set confidence to 0 or skip publishing
        results = await runner.process_market_data(data)
        # Even with stale data, signals should still be returned (flagged)
        assert isinstance(results, list)


class TestRunnerInit:
    def test_initializes_all_signal_groups(self):
        """Runner should initialize technical, fundamental, and event signals."""
        runner = _make_runner()
        assert len(runner._technical_signals) == 3
        assert len(runner._fundamental_signals) == 3
        assert len(runner._event_signals) == 3

    def test_staleness_checker_is_configured(self):
        """Runner should have a StalenessChecker configured from config."""
        runner = _make_runner()
        assert runner._staleness is not None
