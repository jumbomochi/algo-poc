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
    capture_universe=None,
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
        capture_universe=capture_universe,
    )


def _stub_pipelines(runner, *, bars_per_ticker: int = 1):
    """Replace the three pipelines with mocks, market data returning a count."""
    runner._market_data = MagicMock()
    runner._market_data.ingest = AsyncMock(return_value=bars_per_ticker)
    runner._market_data.capture = AsyncMock(return_value=bars_per_ticker)
    runner._fundamentals = MagicMock()
    runner._fundamentals.ingest = AsyncMock()
    runner._events = MagicMock()
    runner._events.ingest = AsyncMock()
    return runner


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


class TestCaptureUniverse:
    """KAN-58 — the capture universe is wider than the trading universe.

    Bars for the whole index have to accumulate from today so a future
    baseline is point-in-time by construction, but widening what
    signal_generation scores is a separate decision. So the extra names are
    fetched and kept without being published.
    """

    @pytest.mark.asyncio
    async def test_capture_only_tickers_are_persisted_not_published(self):
        runner = _stub_pipelines(_make_runner(capture_universe=["AAPL", "ABNB"]))

        await runner.run_cycle(["AAPL"])

        assert [c[0][0] for c in runner._market_data.ingest.call_args_list] == ["AAPL"]
        assert [c[0][0] for c in runner._market_data.capture.call_args_list] == ["ABNB"]

    @pytest.mark.asyncio
    async def test_capture_only_tickers_skip_fundamentals_and_events(self):
        """Capture is about price history. Running the other two pipelines over
        the extra 363 names would triple the IB load for data nothing reads."""
        runner = _stub_pipelines(_make_runner(capture_universe=["AAPL", "ABNB"]))

        await runner.run_cycle(["AAPL"])

        assert [c[0][0] for c in runner._fundamentals.ingest.call_args_list] == ["AAPL"]
        assert [c[0][0] for c in runner._events.ingest.call_args_list] == ["AAPL"]

    @pytest.mark.asyncio
    async def test_a_ticker_in_both_universes_is_fetched_once(self):
        runner = _stub_pipelines(_make_runner(capture_universe=["AAPL", "MSFT"]))

        await runner.run_cycle(["AAPL", "MSFT"])

        assert runner._market_data.ingest.call_count == 2
        runner._market_data.capture.assert_not_called()

    @pytest.mark.asyncio
    async def test_run_cycle_reports_capture_health(self):
        runner = _stub_pipelines(_make_runner(capture_universe=["AAPL", "ABNB", "NVDA"]))

        health = await runner.run_cycle(["AAPL"])

        assert health.tickers_expected == 3
        assert health.tickers_written == 3
        assert health.shortfall == 0

    @pytest.mark.asyncio
    async def test_a_failing_ticker_shows_up_as_a_shortfall(self):
        """Silent partial capture is the failure this figure exists to catch:
        a series with holes looks complete years from now and is not."""
        runner = _stub_pipelines(_make_runner(capture_universe=["AAPL", "ABNB", "NVDA"]))
        runner._market_data.capture = AsyncMock(side_effect=[Exception("IB timeout"), 1])

        health = await runner.run_cycle(["AAPL"])

        assert health.tickers_expected == 3
        assert health.tickers_written == 2
        assert health.shortfall == 1
        # The failure must not stop the remaining names.
        assert runner._market_data.capture.call_count == 2

    @pytest.mark.asyncio
    async def test_a_ticker_with_no_bars_is_not_counted_as_written(self):
        runner = _stub_pipelines(_make_runner(capture_universe=["AAPL", "ABNB"]))
        runner._market_data.capture = AsyncMock(return_value=0)

        health = await runner.run_cycle(["AAPL"])

        assert health.tickers_expected == 2
        assert health.tickers_written == 1

    @pytest.mark.asyncio
    async def test_capture_runs_even_with_an_empty_trading_watchlist(self):
        runner = _stub_pipelines(_make_runner(capture_universe=["ABNB"]))

        health = await runner.run_cycle([])

        runner._market_data.capture.assert_called_once()
        runner._fundamentals.ingest.assert_not_called()
        assert health.tickers_expected == 1


class TestIBConnection:
    """The service constructed an IBClient and never dialled it.

    `IBClient._ib` stayed None, so every `get_daily_bars` raised
    AttributeError and the service published nothing and captured nothing —
    which is what the '~21 error lines against one run_cycle_complete' in the
    logs actually was. Connecting is what makes capture do anything at all.
    """

    @pytest.mark.asyncio
    async def test_it_connects_when_not_yet_connected(self):
        ib = MagicMock()
        ib.is_connected = MagicMock(return_value=False)
        ib.connect = AsyncMock()
        runner = _make_runner(ib_client=ib)

        assert await runner.ensure_ib_connected() is True
        ib.connect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_it_does_not_reconnect_when_already_connected(self):
        """A second connectAsync on the same client id makes IB drop the older
        session — reconnecting every cycle would fight itself."""
        ib = MagicMock()
        ib.is_connected = MagicMock(return_value=True)
        ib.connect = AsyncMock()
        runner = _make_runner(ib_client=ib)

        assert await runner.ensure_ib_connected() is True
        ib.connect.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_failed_connect_is_reported_not_raised(self):
        """The gateway restarts nightly and drops the API on a 1100. A connect
        failure has to leave the service alive to retry, not kill the process
        — under restart: unless-stopped that would be a crash loop."""
        ib = MagicMock()
        ib.is_connected = MagicMock(return_value=False)
        ib.connect = AsyncMock(side_effect=OSError("connection refused"))
        runner = _make_runner(ib_client=ib)

        assert await runner.ensure_ib_connected() is False

    @pytest.mark.asyncio
    async def test_it_reconnects_after_the_gateway_drops_the_session(self):
        ib = MagicMock()
        ib.connect = AsyncMock()
        ib.is_connected = MagicMock(side_effect=[False, True, False])
        runner = _make_runner(ib_client=ib)

        await runner.ensure_ib_connected()   # connects
        await runner.ensure_ib_connected()   # already up, no-op
        await runner.ensure_ib_connected()   # dropped, reconnects

        assert ib.connect.await_count == 2

    @pytest.mark.asyncio
    async def test_shutdown_disconnects_the_client(self):
        """Leaving the session open holds data_client_id, so a restart races
        its own previous session for the same id."""
        ib = MagicMock()
        ib.disconnect = AsyncMock()
        ib.is_connected = MagicMock(return_value=True)
        runner = _make_runner(ib_client=ib)

        await runner.shutdown()

        ib.disconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_shutdown_survives_a_disconnect_failure(self):
        ib = MagicMock()
        ib.is_connected = MagicMock(return_value=True)
        ib.disconnect = AsyncMock(side_effect=OSError("already gone"))
        runner = _make_runner(ib_client=ib)

        await runner.shutdown()  # must not raise

        assert runner._running is False


class TestCaptureRequestBudget:
    """Capture must not out-shout the trading path on the shared gateway.

    IB's historical-data ceiling is roughly 60 requests per rolling 10 minutes.
    The capture universe is 503 names on a 15-minute cycle, so fetching all of
    them every cycle would sit an order of magnitude over the limit — earning
    pacing violations whose per-ticker errors are exactly the silent holes this
    feature exists to prevent. The lookback window means a name deferred now is
    picked up by a later cycle with nothing lost.
    """

    @pytest.mark.asyncio
    async def test_capture_stops_at_the_per_cycle_budget(self):
        config = _make_config()
        config.data_ingestion.capture_max_requests_per_cycle = 2
        runner = _stub_pipelines(
            _make_runner(config=config, capture_universe=["A", "B", "C", "D", "E"])
        )

        await runner.run_cycle([])

        assert runner._market_data.capture.await_count == 2

    @pytest.mark.asyncio
    async def test_deferred_names_are_reported_and_not_counted_as_missing(self):
        """A deferred name is scheduled work, not a failure — reporting it as a
        shortfall would cry wolf on every cycle of a healthy catch-up."""
        config = _make_config()
        config.data_ingestion.capture_max_requests_per_cycle = 2
        runner = _stub_pipelines(
            _make_runner(config=config, capture_universe=["A", "B", "C", "D", "E"])
        )

        health = await runner.run_cycle([])

        assert health.tickers_expected == 5
        assert health.tickers_written == 2
        assert health.tickers_deferred == 3
        assert health.shortfall == 0

    @pytest.mark.asyncio
    async def test_a_real_failure_is_still_a_shortfall_while_deferring(self):
        config = _make_config()
        config.data_ingestion.capture_max_requests_per_cycle = 2
        runner = _stub_pipelines(
            _make_runner(config=config, capture_universe=["A", "B", "C", "D"])
        )
        runner._market_data.capture = AsyncMock(side_effect=[Exception("pacing"), 1])

        health = await runner.run_cycle([])

        assert health.tickers_written == 1
        assert health.tickers_deferred == 2
        assert health.shortfall == 1

    @pytest.mark.asyncio
    async def test_successive_cycles_advance_through_the_universe(self):
        """Restarting from the top every cycle would starve the tail forever."""
        config = _make_config()
        config.data_ingestion.capture_max_requests_per_cycle = 2
        runner = _stub_pipelines(
            _make_runner(config=config, capture_universe=["A", "B", "C", "D", "E"])
        )

        await runner.run_cycle([])
        await runner.run_cycle([])

        fetched = [c[0][0] for c in runner._market_data.capture.call_args_list]
        assert fetched == ["A", "B", "C", "D"]

    @pytest.mark.asyncio
    async def test_the_cursor_wraps_around(self):
        config = _make_config()
        config.data_ingestion.capture_max_requests_per_cycle = 2
        runner = _stub_pipelines(_make_runner(config=config, capture_universe=["A", "B", "C"]))

        await runner.run_cycle([])
        await runner.run_cycle([])
        await runner.run_cycle([])

        fetched = [c[0][0] for c in runner._market_data.capture.call_args_list]
        assert fetched == ["A", "B", "C", "A", "B", "C"]

    @pytest.mark.asyncio
    async def test_a_budget_of_zero_disables_the_cap_rather_than_capture(self):
        """A misread of the setting must not silently stop capture entirely."""
        config = _make_config()
        config.data_ingestion.capture_max_requests_per_cycle = 0
        runner = _stub_pipelines(_make_runner(config=config, capture_universe=["A", "B", "C"]))

        health = await runner.run_cycle([])

        assert runner._market_data.capture.await_count == 3
        assert health.tickers_deferred == 0

    @pytest.mark.asyncio
    async def test_the_trading_watchlist_is_never_deferred(self):
        """The budget throttles capture only. What the sleeves trade has to be
        fetched every cycle — signal_generation depends on it."""
        config = _make_config()
        config.data_ingestion.capture_max_requests_per_cycle = 1
        runner = _stub_pipelines(
            _make_runner(config=config, capture_universe=["AAPL", "X", "Y"])
        )

        health = await runner.run_cycle(["AAPL", "MSFT", "NVDA"])

        assert runner._market_data.ingest.await_count == 3
        assert health.tickers_written == 4  # 3 traded + 1 captured
        assert health.tickers_deferred == 1


class TestResolveCaptureUniverse:
    """The service reads its capture universe from config, separately from
    the trading watchlist, so widening capture never widens what trades."""

    def test_defaults_to_the_whole_index(self):
        from shared.universe import resolve_capture_universe

        config = _make_config()
        assert config.universe.capture_source == "membership"
        assert len(resolve_capture_universe(config.universe.capture_source)) == 503

    def test_capture_is_wider_than_the_trading_watchlist(self):
        from shared.universe import resolve_capture_universe, resolve_watchlist

        capture = set(resolve_capture_universe("membership"))
        trading = set(resolve_watchlist("sleeves", []))
        assert capture - trading, "capture must cover names the sleeves never trade"

    def test_none_disables_capture(self):
        from shared.universe import resolve_capture_universe

        assert resolve_capture_universe("none") == []

    def test_an_unknown_source_raises_rather_than_capturing_nothing(self):
        from shared.universe import resolve_capture_universe

        with pytest.raises(ValueError):
            resolve_capture_universe("membershp")
