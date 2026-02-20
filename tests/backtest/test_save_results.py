from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from scripts.run_backtest import save_results


def test_save_results_creates_json(tmp_path):
    """save_results writes a valid JSON file with expected keys."""
    config = {
        "tickers": ["AAPL"],
        "years": 1,
        "initial_capital": 100_000,
        "slippage_bps": 10,
        "commission_per_share": 0.005,
    }
    trades = [
        {
            "ticker": "AAPL",
            "entry_date": date(2024, 1, 10),
            "exit_date": date(2024, 2, 15),
            "entry_price": 150.0,
            "exit_price": 162.0,
            "quantity": 33,
            "pnl": 395.67,
            "entry_commission": 0.17,
            "exit_commission": 0.17,
            "entry_signals": {"proximity": {"value": 0.6, "confidence": 0.8}},
            "exit_reason": "profit_target",
        }
    ]
    portfolio_values = [100_000, 100_050, 100_100]
    dates = [date(2024, 1, 2), date(2024, 1, 3)]
    metrics = {"total_return": 0.001, "sharpe_ratio": 1.5}
    bars = {"AAPL": [{"date": date(2024, 1, 2), "open": 150, "high": 151, "low": 149, "close": 150, "volume": 1000}]}

    path = save_results(
        config=config,
        trades=trades,
        portfolio_values=portfolio_values,
        dates=dates,
        metrics=metrics,
        bars=bars,
        output_dir=str(tmp_path),
    )

    assert Path(path).exists()
    with open(path) as f:
        data = json.load(f)

    assert "config" in data
    assert "trades" in data
    assert "portfolio_values" in data
    assert "dates" in data
    assert "metrics" in data
    assert "bars" in data
    assert data["trades"][0]["ticker"] == "AAPL"
    assert data["trades"][0]["entry_date"] == "2024-01-10"
    assert data["bars"]["AAPL"][0]["date"] == "2024-01-02"
