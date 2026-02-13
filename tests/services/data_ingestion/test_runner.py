import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from services.data_ingestion.runner import DataIngestionRunner
from shared.config import AppConfig


def _make_config() -> AppConfig:
    return AppConfig()


def _make_runner(
    config=None,
    ib_client=None,
    redis_client=None,
    db_session=None,
    events_source=None,
):
    config = config or _make_config()
    ib_client = ib_client or MagicMock()
    redis_client = redis_client or AsyncMock()
    db_session = db_session or MagicMock()
    events_source = events_source or MagicMock()
    return DataIngestionRunner(
        config=config,
        ib_client=ib_client,
        redis_client=redis_client,
        db_session=db_session,
        events_source=events_source,
    )


class TestDataIngestionRunner:
    @pytest.mark.asyncio
    async def test_run_cycle_calls_all_pipelines_for_each_ticker(self):
        """run_cycle should call market data, fundamentals, events for each ticker."""
        runner = _make_runner()
        # Mock the internal pipelines
        runner._market_data = MagicMock()
        runner._market_data.ingest = AsyncMock()
        runner._fundamentals = MagicMock()
        runner._fundamentals.ingest = AsyncMock()
        runner._events = MagicMock()
        runner._events.ingest = AsyncMock()

        tickers = ["AAPL", "MSFT", "GOOG"]
        await runner.run_cycle(tickers)

        # Each pipeline should be called once per ticker
        assert runner._market_data.ingest.call_count == 3
        assert runner._fundamentals.ingest.call_count == 3
        assert runner._events.ingest.call_count == 3

        # Check that the correct tickers were passed
        market_tickers = [call[0][0] for call in runner._market_data.ingest.call_args_list]
        assert set(market_tickers) == {"AAPL", "MSFT", "GOOG"}

        fund_tickers = [call[0][0] for call in runner._fundamentals.ingest.call_args_list]
        assert set(fund_tickers) == {"AAPL", "MSFT", "GOOG"}

        events_tickers = [call[0][0] for call in runner._events.ingest.call_args_list]
        assert set(events_tickers) == {"AAPL", "MSFT", "GOOG"}

    @pytest.mark.asyncio
    async def test_run_cycle_with_empty_tickers(self):
        """run_cycle should handle an empty ticker list gracefully."""
        runner = _make_runner()
        runner._market_data = MagicMock()
        runner._market_data.ingest = AsyncMock()
        runner._fundamentals = MagicMock()
        runner._fundamentals.ingest = AsyncMock()
        runner._events = MagicMock()
        runner._events.ingest = AsyncMock()

        await runner.run_cycle([])

        runner._market_data.ingest.assert_not_called()
        runner._fundamentals.ingest.assert_not_called()
        runner._events.ingest.assert_not_called()

    @pytest.mark.asyncio
    async def test_run_cycle_continues_on_pipeline_error(self):
        """run_cycle should log errors but continue processing other tickers."""
        runner = _make_runner()
        runner._market_data = MagicMock()
        runner._market_data.ingest = AsyncMock(side_effect=[
            Exception("IB timeout"),
            None,
        ])
        runner._fundamentals = MagicMock()
        runner._fundamentals.ingest = AsyncMock()
        runner._events = MagicMock()
        runner._events.ingest = AsyncMock()

        # Should not raise even though first market_data.ingest fails
        await runner.run_cycle(["AAPL", "MSFT"])

        # MSFT should still have been processed after AAPL error
        assert runner._market_data.ingest.call_count == 2
        assert runner._fundamentals.ingest.call_count == 2
        assert runner._events.ingest.call_count == 2

    def test_is_market_active_delegates_to_calendar(self):
        """is_market_active should use MarketCalendar.is_market_open."""
        runner = _make_runner()
        mock_calendar = MagicMock()
        runner._calendar = mock_calendar

        # Market open
        mock_calendar.is_market_open.return_value = True
        assert runner.is_market_active() is True

        # Market closed
        mock_calendar.is_market_open.return_value = False
        assert runner.is_market_active() is False

    @pytest.mark.asyncio
    async def test_run_cycle_single_ticker(self):
        """run_cycle with a single ticker should call each pipeline exactly once."""
        runner = _make_runner()
        runner._market_data = MagicMock()
        runner._market_data.ingest = AsyncMock()
        runner._fundamentals = MagicMock()
        runner._fundamentals.ingest = AsyncMock()
        runner._events = MagicMock()
        runner._events.ingest = AsyncMock()

        await runner.run_cycle(["AAPL"])

        runner._market_data.ingest.assert_called_once()
        runner._fundamentals.ingest.assert_called_once_with("AAPL")
        runner._events.ingest.assert_called_once_with("AAPL")

    def test_runner_initializes_all_pipelines(self):
        """Runner should initialize market_data, fundamentals, and events pipelines."""
        runner = _make_runner()

        assert runner._market_data is not None
        assert runner._fundamentals is not None
        assert runner._events is not None

    @pytest.mark.asyncio
    async def test_shutdown_sets_running_flag(self):
        """shutdown() should set _running to False for graceful stop."""
        runner = _make_runner()
        assert runner._running is True

        await runner.shutdown()

        assert runner._running is False
