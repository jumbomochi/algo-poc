from __future__ import annotations

from datetime import date

import pytest

from backtest.simulator import SimulatedExecutor


class TestLimitEntry:
    def test_limit_entry_fills_when_low_below_price(self):
        executor = SimulatedExecutor(slippage_bps=10, commission_per_share=0.005)
        bar = {"date": date(2025, 1, 6), "open": 150.0, "high": 155.0, "low": 148.0, "close": 153.0}
        fill = executor.try_fill_limit_entry(limit_price=149.0, quantity=100, bar=bar)
        assert fill is not None
        assert fill["filled"] is True
        assert fill["fill_price"] == pytest.approx(149.0 * 1.001)  # with slippage

    def test_limit_entry_does_not_fill_when_low_above_price(self):
        executor = SimulatedExecutor(slippage_bps=10, commission_per_share=0.005)
        bar = {"date": date(2025, 1, 6), "open": 150.0, "high": 155.0, "low": 151.0, "close": 153.0}
        fill = executor.try_fill_limit_entry(limit_price=149.0, quantity=100, bar=bar)
        assert fill is None

    def test_limit_entry_fills_when_low_equals_price(self):
        executor = SimulatedExecutor(slippage_bps=10, commission_per_share=0.005)
        bar = {"date": date(2025, 1, 6), "open": 150.0, "high": 155.0, "low": 149.0, "close": 153.0}
        fill = executor.try_fill_limit_entry(limit_price=149.0, quantity=100, bar=bar)
        assert fill is not None
        assert fill["filled"] is True

    def test_limit_entry_zero_slippage(self):
        executor = SimulatedExecutor(slippage_bps=0, commission_per_share=0.005)
        bar = {"date": date(2025, 1, 6), "open": 150.0, "high": 155.0, "low": 148.0, "close": 153.0}
        fill = executor.try_fill_limit_entry(limit_price=149.0, quantity=100, bar=bar)
        assert fill is not None
        assert fill["fill_price"] == pytest.approx(149.0)


class TestMarketExit:
    def test_market_exit_fills_at_next_open(self):
        executor = SimulatedExecutor(slippage_bps=10, commission_per_share=0.005)
        bar = {"date": date(2025, 1, 7), "open": 152.0, "high": 155.0, "low": 150.0, "close": 153.0}
        fill = executor.fill_market_exit(quantity=100, bar=bar)
        assert fill["filled"] is True
        assert fill["fill_price"] == pytest.approx(152.0 * 0.999)

    def test_market_exit_always_fills(self):
        executor = SimulatedExecutor(slippage_bps=5, commission_per_share=0.01)
        bar = {"date": date(2025, 1, 7), "open": 100.0, "high": 110.0, "low": 90.0, "close": 105.0}
        fill = executor.fill_market_exit(quantity=50, bar=bar)
        assert fill is not None
        assert fill["filled"] is True
        assert fill["date"] == date(2025, 1, 7)

    def test_market_exit_zero_slippage(self):
        executor = SimulatedExecutor(slippage_bps=0, commission_per_share=0.005)
        bar = {"date": date(2025, 1, 7), "open": 152.0, "high": 155.0, "low": 150.0, "close": 153.0}
        fill = executor.fill_market_exit(quantity=100, bar=bar)
        assert fill["fill_price"] == pytest.approx(152.0)


class TestCommission:
    def test_commission_calculated(self):
        executor = SimulatedExecutor(slippage_bps=0, commission_per_share=0.005)
        bar = {"date": date(2025, 1, 6), "open": 150.0, "high": 155.0, "low": 148.0, "close": 153.0}
        fill = executor.try_fill_limit_entry(limit_price=149.0, quantity=100, bar=bar)
        assert fill["commission"] == pytest.approx(0.50)

    def test_commission_on_exit(self):
        executor = SimulatedExecutor(slippage_bps=0, commission_per_share=0.01)
        bar = {"date": date(2025, 1, 7), "open": 152.0, "high": 155.0, "low": 150.0, "close": 153.0}
        fill = executor.fill_market_exit(quantity=200, bar=bar)
        assert fill["commission"] == pytest.approx(2.0)

    def test_fill_dict_has_all_keys(self):
        executor = SimulatedExecutor(slippage_bps=10, commission_per_share=0.005)
        bar = {"date": date(2025, 1, 6), "open": 150.0, "high": 155.0, "low": 148.0, "close": 153.0}
        fill = executor.try_fill_limit_entry(limit_price=149.0, quantity=100, bar=bar)
        assert set(fill.keys()) == {"filled", "fill_price", "quantity", "commission", "date"}
        assert fill["quantity"] == 100
        assert fill["date"] == date(2025, 1, 6)
