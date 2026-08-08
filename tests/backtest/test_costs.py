from __future__ import annotations

import pytest

from backtest.costs import (
    DEFAULT_COMMISSION_MINIMUM,
    DEFAULT_COMMISSION_PER_SHARE,
    DEFAULT_SLIPPAGE_BPS,
    CostModel,
    liquidity_tier,
)


class TestCommissionFloor:
    def test_small_order_pays_the_per_order_minimum(self):
        model = CostModel(commission_per_share=0.005, commission_minimum=1.0)
        # 10 shares * $0.005 = $0.05, well below the $1 IB minimum.
        assert model.commission_for(10) == pytest.approx(1.0)

    def test_large_order_pays_per_share(self):
        model = CostModel(commission_per_share=0.005, commission_minimum=1.0)
        assert model.commission_for(1_000) == pytest.approx(5.0)

    def test_minimum_can_be_disabled(self):
        model = CostModel(commission_per_share=0.005, commission_minimum=0.0)
        assert model.commission_for(10) == pytest.approx(0.05)

    def test_fractional_quantity_still_pays_minimum(self):
        model = CostModel(commission_per_share=0.005, commission_minimum=1.0)
        assert model.commission_for(0.4321) == pytest.approx(1.0)

    def test_defaults_include_a_commission_floor(self):
        assert DEFAULT_COMMISSION_MINIMUM > 0
        assert CostModel().commission_for(1) == pytest.approx(
            DEFAULT_COMMISSION_MINIMUM
        )
        assert DEFAULT_COMMISSION_PER_SHARE == pytest.approx(0.005)


class TestPerInstrumentSlippage:
    def test_unknown_ticker_uses_the_base_rate(self):
        model = CostModel(slippage_bps=10.0)
        assert model.slippage_bps_for("AAPL") == pytest.approx(10.0)
        assert model.slippage_bps_for(None) == pytest.approx(10.0)

    def test_explicit_override_wins(self):
        model = CostModel(slippage_bps=10.0, slippage_bps_by_ticker={"ARKK": 33.0})
        assert model.slippage_bps_for("ARKK") == pytest.approx(33.0)
        assert model.slippage_bps_for("AAPL") == pytest.approx(10.0)

    def test_liquidity_tiers_widen_thin_instruments(self):
        model = CostModel.with_liquidity_tiers(slippage_bps=10.0)
        # Mega-cap equities and broad sector ETFs trade at the base rate.
        assert model.slippage_bps_for("AAPL") == pytest.approx(10.0)
        assert model.slippage_bps_for("XLK") == pytest.approx(10.0)
        # Thematic and inverse ETFs are thinner and must cost more.
        assert model.slippage_bps_for("ARKK") > 10.0
        assert model.slippage_bps_for("SH") > 10.0
        assert model.slippage_bps_for("ARKK") > model.slippage_bps_for("SH")

    def test_liquidity_tiers_scale_with_the_base_rate(self):
        tight = CostModel.with_liquidity_tiers(slippage_bps=10.0)
        wide = CostModel.with_liquidity_tiers(slippage_bps=20.0)
        assert wide.slippage_bps_for("ARKK") == pytest.approx(
            2 * tight.slippage_bps_for("ARKK")
        )

    def test_liquidity_tier_classification(self):
        assert liquidity_tier("AAPL") == "liquid"
        assert liquidity_tier("XLE") == "liquid"
        assert liquidity_tier("TLT") == "liquid"
        assert liquidity_tier("ARKK") == "thematic_etf"
        assert liquidity_tier("PSQ") == "inverse_etf"

    def test_default_base_rate(self):
        assert DEFAULT_SLIPPAGE_BPS == pytest.approx(10.0)
