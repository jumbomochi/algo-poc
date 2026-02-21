from __future__ import annotations

from services.risk_management.engine import RiskEngine, PortfolioState


def _make_portfolio(nav: float = 100_000) -> PortfolioState:
    return PortfolioState(
        nav=nav,
        peak_nav=nav,
        positions={},
        sector_exposure={},
        total_exposure_pct=0.0,
        margin_utilization_pct=0.0,
    )


def test_check_entry_respects_max_lots():
    """check_entry should reject when existing_lots >= max_lots_per_ticker."""
    engine = RiskEngine(
        position_entry_limit_pct=7.0,
        sector_concentration_pct=30.0,
        total_exposure_limit_pct=100.0,
        max_lots_per_ticker=2,
    )
    portfolio = _make_portfolio()
    # First lot: approved
    decision = engine.check_entry("AAPL", 50, 150.0, "Tech", portfolio, existing_lots=0)
    assert decision.approved

    # Second lot: approved
    decision = engine.check_entry("AAPL", 50, 155.0, "Tech", portfolio, existing_lots=1)
    assert decision.approved

    # Third lot: rejected (max 2)
    decision = engine.check_entry("AAPL", 50, 160.0, "Tech", portfolio, existing_lots=2)
    assert not decision.approved
    assert "max lots" in decision.reason.lower()


def test_check_entry_default_no_lot_limit():
    """Without max_lots_per_ticker, existing_lots should be ignored."""
    engine = RiskEngine(
        position_entry_limit_pct=7.0,
        sector_concentration_pct=30.0,
        total_exposure_limit_pct=100.0,
    )
    portfolio = _make_portfolio()
    decision = engine.check_entry("AAPL", 50, 150.0, "Tech", portfolio, existing_lots=5)
    assert decision.approved
