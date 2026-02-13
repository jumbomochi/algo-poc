from __future__ import annotations

import pytest

from services.risk_management.engine import PortfolioState, RiskDecision, RiskEngine


def make_portfolio(nav: float = 100_000, positions: dict | None = None) -> PortfolioState:
    return PortfolioState(
        nav=nav,
        peak_nav=nav,
        positions=positions or {},
        sector_exposure={},
        total_exposure_pct=0.0,
        margin_utilization_pct=0.0,
    )


class TestPositionEntryLimit:
    def test_position_entry_limit_scales_down(self):
        engine = RiskEngine(
            position_entry_limit_pct=5.0,
            sector_concentration_pct=20.0,
            total_exposure_limit_pct=150.0,
        )
        portfolio = make_portfolio(nav=100_000)
        decision = engine.check_entry(
            "AAPL", quantity=100, price=100.0, sector="Technology", portfolio=portfolio
        )
        assert decision.approved is True
        # 5% of $100k = $5000 at $100/share = 50 shares max
        assert decision.adjusted_quantity < 100
        assert decision.adjusted_quantity == 50

    def test_position_within_limit_not_scaled(self):
        engine = RiskEngine(
            position_entry_limit_pct=5.0,
            sector_concentration_pct=20.0,
            total_exposure_limit_pct=150.0,
        )
        portfolio = make_portfolio(nav=100_000)
        decision = engine.check_entry(
            "AAPL", quantity=10, price=100.0, sector="Technology", portfolio=portfolio
        )
        assert decision.approved is True
        assert decision.adjusted_quantity == 10

    def test_position_exactly_at_limit(self):
        engine = RiskEngine(
            position_entry_limit_pct=5.0,
            sector_concentration_pct=20.0,
            total_exposure_limit_pct=150.0,
        )
        portfolio = make_portfolio(nav=100_000)
        # 50 shares * $100 = $5000 = exactly 5%
        decision = engine.check_entry(
            "AAPL", quantity=50, price=100.0, sector="Technology", portfolio=portfolio
        )
        assert decision.approved is True
        assert decision.adjusted_quantity == 50


class TestSectorConcentration:
    def test_sector_concentration_rejects(self):
        engine = RiskEngine(
            position_entry_limit_pct=5.0,
            sector_concentration_pct=20.0,
            total_exposure_limit_pct=150.0,
        )
        portfolio = make_portfolio(nav=100_000)
        portfolio.sector_exposure = {"Technology": 20.0}
        decision = engine.check_entry(
            "MSFT", quantity=10, price=100.0, sector="Technology", portfolio=portfolio
        )
        assert decision.approved is False
        assert "sector" in decision.reason.lower()

    def test_sector_below_limit_allows(self):
        engine = RiskEngine(
            position_entry_limit_pct=5.0,
            sector_concentration_pct=20.0,
            total_exposure_limit_pct=150.0,
        )
        portfolio = make_portfolio(nav=100_000)
        portfolio.sector_exposure = {"Technology": 15.0}
        decision = engine.check_entry(
            "MSFT", quantity=10, price=100.0, sector="Technology", portfolio=portfolio
        )
        assert decision.approved is True


class TestTotalExposure:
    def test_total_exposure_rejects(self):
        engine = RiskEngine(
            position_entry_limit_pct=5.0,
            sector_concentration_pct=20.0,
            total_exposure_limit_pct=150.0,
        )
        portfolio = make_portfolio(nav=100_000)
        portfolio.total_exposure_pct = 150.0
        decision = engine.check_entry(
            "AAPL", quantity=10, price=100.0, sector="Technology", portfolio=portfolio
        )
        assert decision.approved is False
        assert "exposure" in decision.reason.lower()

    def test_total_exposure_below_limit_allows(self):
        engine = RiskEngine(
            position_entry_limit_pct=5.0,
            sector_concentration_pct=20.0,
            total_exposure_limit_pct=150.0,
        )
        portfolio = make_portfolio(nav=100_000)
        portfolio.total_exposure_pct = 100.0
        decision = engine.check_entry(
            "AAPL", quantity=10, price=100.0, sector="Technology", portfolio=portfolio
        )
        assert decision.approved is True


class TestRiskDecisionDataclass:
    def test_risk_decision_fields(self):
        d = RiskDecision(approved=True, reason="ok", adjusted_quantity=10)
        assert d.approved is True
        assert d.reason == "ok"
        assert d.adjusted_quantity == 10

    def test_risk_decision_rejected(self):
        d = RiskDecision(approved=False, reason="too risky", adjusted_quantity=0)
        assert d.approved is False
        assert d.adjusted_quantity == 0
