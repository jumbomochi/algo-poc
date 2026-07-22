from __future__ import annotations

from datetime import date, timedelta

import pytest

from scripts.validate_replacement_policy import (
    annualized_turnover,
    evaluate_replacement_policies,
    promotion_decision,
    walk_forward_summary,
)


def test_annualized_turnover_uses_average_bought_and_sold_notional():
    trades = [{"entry_price": 10, "exit_price": 11, "quantity": 10}]

    turnover = annualized_turnover(
        trades,
        portfolio_values=[1_000, 1_000],
        dates=[date(2024, 1, 1), date(2025, 1, 1)],
    )

    assert turnover == pytest.approx(0.105, rel=0.01)


def test_walk_forward_summary_reports_non_overlapping_windows():
    dates = [date(2024, 1, 1) + timedelta(days=day) for day in range(6)]
    summary = walk_forward_summary(
        portfolio_values=[100, 101, 102, 101, 103, 104, 105],
        dates=dates,
        trades=[],
        window_days=3,
    )

    assert len(summary["windows"]) == 2
    assert summary["windows"][0]["start_date"] == "2024-01-01"
    assert summary["windows"][1]["end_date"] == "2024-01-06"
    assert "mean_sharpe_ratio" in summary
    assert "max_drawdown" in summary


def test_evaluate_replacement_policies_produces_comparable_report():
    start = date(2024, 1, 1)
    bars = {}
    for index, ticker in enumerate(["AAPL", "MSFT", "AMZN", "ARKK", "LIT"]):
        price = 100.0
        ticker_bars = []
        for day in range(90):
            price *= 1 + (index + 1) * 0.0005
            ticker_bars.append(
                {
                    "date": start + timedelta(days=day),
                    "open": price,
                    "high": price * 1.01,
                    "low": price * 0.99,
                    "close": price,
                    "volume": 100_000,
                }
            )
        bars[ticker] = ticker_bars

    fundamentals = {
        "AAPL": {"roe": 0.25, "debt_equity": 0.5, "profit_margin": 0.30},
        "MSFT": {"roe": 0.15, "debt_equity": 1.0, "profit_margin": 0.20},
        "AMZN": {"roe": 0.08, "debt_equity": 2.0, "profit_margin": 0.05},
    }
    report = evaluate_replacement_policies(
        bars,
        fundamentals_lookup=lambda ticker, as_of: fundamentals.get(ticker),
        sector_map={ticker: "Technology" for ticker in fundamentals},
        quality_universe=list(fundamentals),
        thematic_universe=["ARKK", "LIT"],
        capital=100_000,
        score_margin=0.25,
        walk_forward_days=30,
    )

    assert set(report["policies"]) == {
        "technical_only",
        "weakest",
        "score_margin",
    }
    for result in report["policies"].values():
        assert set(result["strategies"]) == {"quality_value", "thematic_momentum"}
        assert {
            "total_return",
            "sharpe_ratio",
            "max_drawdown",
            "trade_count",
            "annual_turnover",
            "walk_forward",
        } <= set(result)
    assert report["promotion"]["recommended_policy"] in report["policies"]


def test_promotion_requires_sharpe_drawdown_and_turnover_gates():
    policies = {
        "technical_only": {
            "walk_forward": {"mean_sharpe_ratio": 1.0, "max_drawdown": 0.10},
            "annual_turnover": 0.25,
        },
        "weakest": {
            "walk_forward": {"mean_sharpe_ratio": 1.2, "max_drawdown": 0.10},
            "annual_turnover": 1.9,
        },
        "score_margin": {
            "walk_forward": {"mean_sharpe_ratio": 1.3, "max_drawdown": 0.11},
            "annual_turnover": 1.5,
        },
    }

    decision = promotion_decision(policies, turnover_ceiling=2.0)

    assert decision["recommended_policy"] == "weakest"
    assert decision["candidates"]["weakest"]["eligible"] is True
    assert decision["candidates"]["score_margin"]["eligible"] is False
    assert decision["candidates"]["score_margin"]["checks"]["max_drawdown"] is False


def test_promotion_stays_technical_only_when_no_candidate_passes():
    policies = {
        "technical_only": {
            "walk_forward": {"mean_sharpe_ratio": 1.0, "max_drawdown": 0.10},
            "annual_turnover": 1.0,
        },
        "weakest": {
            "walk_forward": {"mean_sharpe_ratio": 0.9, "max_drawdown": 0.08},
            "annual_turnover": 1.0,
        },
        "score_margin": {
            "walk_forward": {"mean_sharpe_ratio": 1.1, "max_drawdown": 0.09},
            "annual_turnover": 2.1,
        },
    }

    assert promotion_decision(policies, turnover_ceiling=2.0)[
        "recommended_policy"
    ] == "technical_only"
