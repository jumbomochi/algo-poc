from __future__ import annotations

from datetime import date

import pytest

from backtest.costs import CostModel
from backtest.simulator import SimulatedExecutor


def _executor(
    slippage_bps: float = 10.0,
    commission_per_share: float = 0.005,
    commission_minimum: float = 0.0,
    slippage_bps_by_ticker: dict[str, float] | None = None,
) -> SimulatedExecutor:
    return SimulatedExecutor(
        CostModel(
            slippage_bps=slippage_bps,
            commission_per_share=commission_per_share,
            commission_minimum=commission_minimum,
            slippage_bps_by_ticker=slippage_bps_by_ticker or {},
        )
    )


class TestLimitEntry:
    def test_limit_entry_fills_at_limit_when_low_reaches_it(self):
        executor = _executor()
        bar = {"date": date(2025, 1, 6), "open": 150.0, "high": 155.0, "low": 148.0, "close": 153.0}
        fill = executor.try_fill_limit_entry(limit_price=149.0, quantity=100, bar=bar)
        assert fill is not None
        assert fill["filled"] is True
        assert fill["fill_price"] == pytest.approx(149.0 * 1.001)  # with slippage

    def test_limit_entry_fills_at_the_open_when_the_bar_gaps_through(self):
        """A limit buy resting before the open fills at the open, not the limit.

        The open is the first price the order can trade against; if the market
        opens below the limit the fill is *better* than the limit price.
        """
        executor = _executor(slippage_bps=0)
        bar = {"date": date(2025, 1, 7), "open": 145.0, "high": 150.0, "low": 144.0, "close": 148.0}
        fill = executor.try_fill_limit_entry(limit_price=149.0, quantity=100, bar=bar)
        assert fill is not None
        assert fill["fill_price"] == pytest.approx(145.0)

    def test_limit_entry_does_not_fill_when_low_above_price(self):
        executor = _executor()
        bar = {"date": date(2025, 1, 6), "open": 150.0, "high": 155.0, "low": 151.0, "close": 153.0}
        fill = executor.try_fill_limit_entry(limit_price=149.0, quantity=100, bar=bar)
        assert fill is None

    def test_limit_entry_fills_when_low_equals_price(self):
        executor = _executor()
        bar = {"date": date(2025, 1, 6), "open": 150.0, "high": 155.0, "low": 149.0, "close": 153.0}
        fill = executor.try_fill_limit_entry(limit_price=149.0, quantity=100, bar=bar)
        assert fill is not None
        assert fill["filled"] is True

    def test_limit_entry_zero_slippage(self):
        executor = _executor(slippage_bps=0)
        bar = {"date": date(2025, 1, 6), "open": 150.0, "high": 155.0, "low": 148.0, "close": 153.0}
        fill = executor.try_fill_limit_entry(limit_price=149.0, quantity=100, bar=bar)
        assert fill is not None
        assert fill["fill_price"] == pytest.approx(149.0)


class TestMarketExit:
    def test_market_exit_fills_at_open(self):
        executor = _executor()
        bar = {"date": date(2025, 1, 7), "open": 152.0, "high": 155.0, "low": 150.0, "close": 153.0}
        fill = executor.fill_market_exit(quantity=100, bar=bar)
        assert fill["filled"] is True
        assert fill["fill_price"] == pytest.approx(152.0 * 0.999)

    def test_market_exit_always_fills(self):
        executor = _executor(slippage_bps=5, commission_per_share=0.01)
        bar = {"date": date(2025, 1, 7), "open": 100.0, "high": 110.0, "low": 90.0, "close": 105.0}
        fill = executor.fill_market_exit(quantity=50, bar=bar)
        assert fill is not None
        assert fill["filled"] is True
        assert fill["date"] == date(2025, 1, 7)

    def test_market_exit_zero_slippage(self):
        executor = _executor(slippage_bps=0)
        bar = {"date": date(2025, 1, 7), "open": 152.0, "high": 155.0, "low": 150.0, "close": 153.0}
        fill = executor.fill_market_exit(quantity=100, bar=bar)
        assert fill["fill_price"] == pytest.approx(152.0)


class TestPerInstrumentSlippage:
    def test_entry_uses_the_ticker_slippage_override(self):
        executor = _executor(slippage_bps=10, slippage_bps_by_ticker={"ARKK": 50.0})
        bar = {"date": date(2025, 1, 6), "open": 100.0, "high": 101.0, "low": 98.0, "close": 100.0}
        fill = executor.try_fill_limit_entry(
            limit_price=99.0, quantity=100, bar=bar, ticker="ARKK"
        )
        assert fill["fill_price"] == pytest.approx(99.0 * 1.005)

    def test_exit_uses_the_ticker_slippage_override(self):
        executor = _executor(slippage_bps=10, slippage_bps_by_ticker={"ARKK": 50.0})
        bar = {"date": date(2025, 1, 7), "open": 100.0, "high": 101.0, "low": 98.0, "close": 100.0}
        fill = executor.fill_market_exit(quantity=100, bar=bar, ticker="ARKK")
        assert fill["fill_price"] == pytest.approx(100.0 * 0.995)


class TestCommission:
    def test_commission_calculated(self):
        executor = _executor(slippage_bps=0, commission_per_share=0.005)
        bar = {"date": date(2025, 1, 6), "open": 150.0, "high": 155.0, "low": 148.0, "close": 153.0}
        fill = executor.try_fill_limit_entry(limit_price=149.0, quantity=100, bar=bar)
        assert fill["commission"] == pytest.approx(0.50)

    def test_commission_on_exit(self):
        executor = _executor(slippage_bps=0, commission_per_share=0.01)
        bar = {"date": date(2025, 1, 7), "open": 152.0, "high": 155.0, "low": 150.0, "close": 153.0}
        fill = executor.fill_market_exit(quantity=200, bar=bar)
        assert fill["commission"] == pytest.approx(2.0)

    def test_entry_commission_respects_the_per_order_minimum(self):
        executor = _executor(slippage_bps=0, commission_per_share=0.005, commission_minimum=1.0)
        bar = {"date": date(2025, 1, 6), "open": 150.0, "high": 155.0, "low": 148.0, "close": 153.0}
        fill = executor.try_fill_limit_entry(limit_price=149.0, quantity=5, bar=bar)
        assert fill["commission"] == pytest.approx(1.0)

    def test_exit_commission_respects_the_per_order_minimum(self):
        executor = _executor(slippage_bps=0, commission_per_share=0.005, commission_minimum=1.0)
        bar = {"date": date(2025, 1, 7), "open": 152.0, "high": 155.0, "low": 150.0, "close": 153.0}
        fill = executor.fill_market_exit(quantity=5, bar=bar)
        assert fill["commission"] == pytest.approx(1.0)

    def test_fill_dict_has_all_keys(self):
        executor = _executor()
        bar = {"date": date(2025, 1, 6), "open": 150.0, "high": 155.0, "low": 148.0, "close": 153.0}
        fill = executor.try_fill_limit_entry(limit_price=149.0, quantity=100, bar=bar)
        assert set(fill.keys()) == {"filled", "fill_price", "quantity", "commission", "date"}
        assert fill["quantity"] == 100
        assert fill["date"] == date(2025, 1, 6)
