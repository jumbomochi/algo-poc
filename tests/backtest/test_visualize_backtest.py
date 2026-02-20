from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path


def _generate_sample_bars(ticker: str, start_date: date, num_days: int, base_price: float) -> list[dict]:
    """Generate realistic OHLCV bar data for testing."""
    import random

    random.seed(hash(ticker))
    bars = []
    price = base_price
    d = start_date

    for _ in range(num_days):
        # Skip weekends
        while d.weekday() >= 5:
            d += timedelta(days=1)

        change_pct = random.gauss(0.0005, 0.015)
        open_price = price
        close_price = price * (1 + change_pct)
        high_price = max(open_price, close_price) * (1 + abs(random.gauss(0, 0.005)))
        low_price = min(open_price, close_price) * (1 - abs(random.gauss(0, 0.005)))
        volume = random.randint(500_000, 5_000_000)

        bars.append({
            "date": d.isoformat(),
            "open": round(open_price, 2),
            "high": round(high_price, 2),
            "low": round(low_price, 2),
            "close": round(close_price, 2),
            "volume": volume,
        })

        price = close_price
        d += timedelta(days=1)

    return bars


def _build_sample_json(tmp_path: Path) -> Path:
    """Build a sample backtest JSON file with realistic data."""
    start_date = date(2023, 1, 3)
    num_days = 252

    aapl_bars = _generate_sample_bars("AAPL", start_date, num_days, 150.0)
    msft_bars = _generate_sample_bars("MSFT", start_date, num_days, 250.0)

    # Use the AAPL bar dates as the canonical date list
    dates = [b["date"] for b in aapl_bars]

    # Portfolio values: one more entry than dates (initial capital first)
    initial_capital = 100_000.0
    portfolio_values = [initial_capital]
    nav = initial_capital
    for i in range(len(dates)):
        nav += (i - 126) * 2  # gentle uptrend from midpoint
        portfolio_values.append(round(nav, 2))

    trades = [
        {
            "ticker": "AAPL",
            "entry_date": dates[60],
            "exit_date": dates[90],
            "entry_price": float(aapl_bars[60]["close"]),
            "exit_price": float(aapl_bars[90]["close"]),
            "quantity": 33,
            "pnl": 245.50,
            "entry_commission": 0.17,
            "exit_commission": 0.17,
            "entry_signals": {
                "proximity": {"value": 0.65, "confidence": 0.80},
                "strength": {"value": 0.30, "confidence": 0.55},
                "trend": {"value": 0.20, "confidence": 0.40},
            },
            "exit_reason": "profit_target",
        },
        {
            "ticker": "MSFT",
            "entry_date": dates[100],
            "exit_date": dates[140],
            "entry_price": float(msft_bars[100]["close"]),
            "exit_price": float(msft_bars[140]["close"]),
            "quantity": 20,
            "pnl": -180.30,
            "entry_commission": 0.10,
            "exit_commission": 0.10,
            "entry_signals": {
                "proximity": {"value": 0.55, "confidence": 0.70},
                "strength": {"value": 0.10, "confidence": 0.35},
                "trend": {"value": -0.05, "confidence": 0.25},
            },
            "exit_reason": "stop_loss",
        },
    ]

    metrics = {
        "total_return": 0.0652,
        "sharpe_ratio": 1.12,
        "max_drawdown": -0.045,
        "win_rate": 0.50,
        "avg_holding_period_days": 32.5,
        "total_trades": 2,
    }

    data = {
        "config": {
            "tickers": ["AAPL", "MSFT"],
            "years": 1,
            "initial_capital": initial_capital,
            "slippage_bps": 10,
            "commission_per_share": 0.005,
        },
        "metrics": metrics,
        "trades": trades,
        "portfolio_values": portfolio_values,
        "dates": dates,
        "bars": {
            "AAPL": aapl_bars,
            "MSFT": msft_bars,
        },
    }

    json_path = tmp_path / "test_backtest.json"
    with open(json_path, "w") as f:
        json.dump(data, f, indent=2)

    return json_path


def test_generate_report_creates_html(tmp_path):
    """generate_report reads a JSON file and produces an HTML report."""
    from scripts.visualize_backtest import generate_report

    json_path = _build_sample_json(tmp_path)
    output_path = str(tmp_path / "report.html")

    result_path = generate_report(str(json_path), output_path)

    assert Path(result_path).exists()
    html = Path(result_path).read_text()

    # Must contain Plotly
    assert "plotly" in html.lower()

    # Must contain key chart sections
    assert "Equity Curve" in html
    assert "Drawdown" in html

    # Must contain ticker names
    assert "AAPL" in html
    assert "MSFT" in html


def test_generate_report_default_output_path(tmp_path):
    """When output_path is empty, generate_report creates .html next to .json."""
    from scripts.visualize_backtest import generate_report

    json_path = _build_sample_json(tmp_path)

    result_path = generate_report(str(json_path))

    assert Path(result_path).exists()
    assert result_path.endswith(".html")
