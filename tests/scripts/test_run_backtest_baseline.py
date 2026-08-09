"""The headline backtest's baseline contract: universe, costs, and provenance.

Theme 4 of the 2026-08-06 implementation review. These cover the pieces of
``scripts/run_backtest.py`` that decide *what* is traded and *under which
execution model*, without needing IB data.
"""
from __future__ import annotations

import json
from datetime import date

import pytest

from backtest.costs import CostModel
from backtest.divergence import NEXT_OPEN_FILL_MODEL, execution_model_from_backtest_config
from scripts.run_backtest import (
    ALWAYS_TRADABLE,
    build_base_config,
    build_cost_model,
    load_membership_calendar,
    resolve_backtest_universe,
)
from shared.universe import ACTIVE_SLEEVES, get_union_universe


class TestResolveBacktestUniverse:
    def test_without_membership_falls_back_to_the_static_sleeve_union(self):
        assert resolve_backtest_universe(None) == get_union_universe(ACTIVE_SLEEVES)

    def test_membership_adds_names_that_are_no_longer_in_the_index(self, tmp_path):
        path = tmp_path / "membership.json"
        path.write_text(json.dumps({
            "2015-01-02": ["AAPL", "DELISTED_CO"],
            "2020-01-02": ["AAPL", "NEWCO"],
        }))
        membership = load_membership_calendar(str(path))

        universe = resolve_backtest_universe(membership)

        # Every historical member has to be fetched, including the one that left.
        assert "DELISTED_CO" in universe
        assert "NEWCO" in universe
        assert len(universe) == len(set(universe))

    def test_membership_keeps_the_etf_sleeves_tradable(self, tmp_path):
        path = tmp_path / "membership.json"
        path.write_text(json.dumps({"2015-01-02": ["AAPL"]}))
        membership = load_membership_calendar(str(path))

        # Sector / thematic / inverse ETFs are not index constituents, so they
        # must be exempt from the point-in-time gate or those sleeves go dark.
        assert membership.contains("XLK", date(2016, 6, 1)) is True
        assert membership.contains("ARKK", date(2016, 6, 1)) is True
        assert membership.contains("SH", date(2016, 6, 1)) is True
        assert membership.contains("MSFT", date(2016, 6, 1)) is False

    def test_always_tradable_covers_every_non_equity_sleeve_ticker(self):
        from shared.universe import UNIVERSE_REGISTRY

        for sleeve in ("sector_rotation", "thematic_momentum", "tail_risk_hedge"):
            assert set(UNIVERSE_REGISTRY[sleeve]) <= ALWAYS_TRADABLE


class TestBuildCostModel:
    def test_defaults_carry_a_commission_floor_and_tiered_slippage(self):
        model = build_cost_model(slippage_bps=10, commission_per_share=0.005)

        assert isinstance(model, CostModel)
        assert model.commission_minimum > 0
        assert model.slippage_bps_for("ARKK") > model.slippage_bps_for("AAPL")

    def test_floor_is_configurable(self):
        model = build_cost_model(
            slippage_bps=10, commission_per_share=0.005, commission_minimum=0.0
        )
        assert model.commission_for(1) == pytest.approx(0.005)


class TestBuildBaseConfig:
    def _config(self, **overrides):
        kwargs = dict(
            all_tickers=["AAPL"],
            years=10,
            capital=100_000.0,
            cost_model=build_cost_model(slippage_bps=10, commission_per_share=0.005),
            replacement_policy="technical_only",
            replacement_score_margin=0.25,
            portfolio_capitals={"momentum": 23_080.0},
            point_in_time_universe=True,
        )
        kwargs.update(overrides)
        return build_base_config(**kwargs)

    def test_declares_the_next_open_execution_model(self):
        config = self._config()

        assert config["fill_model"] == NEXT_OPEN_FILL_MODEL
        assert config["commission_minimum"] > 0

    def test_saved_config_is_a_like_for_like_divergence_baseline(self):
        """The divergence monitor must accept results from this backtest."""
        model = execution_model_from_backtest_config(self._config())

        assert model.is_like_for_like is True

    def test_records_whether_the_universe_was_point_in_time(self):
        assert self._config(point_in_time_universe=True)["point_in_time_universe"] is True
        assert (
            self._config(point_in_time_universe=False)["point_in_time_universe"]
            is False
        )

    def test_carries_the_per_instrument_slippage_map(self):
        config = self._config()
        assert config["slippage_bps_by_ticker"]["ARKK"] > config["slippage_bps"]
