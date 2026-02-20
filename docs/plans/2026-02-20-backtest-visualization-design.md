# Backtest Trade Analysis & Visualization

## Goal

Analyze the trading algorithm's backtest results and visualize trades in a self-contained HTML report to support strategy tweaking. Two levels of insight: portfolio-level performance overview and trade-level drill-down.

## Approach

Option A: Separate data persistence from visualization.

1. Modify `scripts/run_backtest.py` to save backtest output (trades, portfolio values, raw bars, metrics) to a JSON file.
2. Create `scripts/visualize_backtest.py` that reads the JSON and generates a Plotly-based HTML report.

This decouples IB data fetching (slow, requires gateway) from visualization (fast, offline).

## Data Persistence

Modify `scripts/run_backtest.py` to save output after backtest completes.

**Output path:** `output/backtest_YYYYMMDD_HHMMSS.json`

**Schema:**

```json
{
  "config": {
    "tickers": ["AAPL", "MSFT", ...],
    "years": 10,
    "initial_capital": 100000,
    "slippage_bps": 10,
    "commission_per_share": 0.005
  },
  "metrics": {
    "total_return": 0.15,
    "sharpe_ratio": 1.2,
    "max_drawdown": 0.08,
    "win_rate": 0.55,
    "avg_holding_period_days": 20.5,
    "total_trades": 142
  },
  "trades": [
    {
      "ticker": "AAPL",
      "entry_date": "2020-03-15",
      "exit_date": "2020-04-20",
      "entry_price": 250.0,
      "exit_price": 270.0,
      "quantity": 20,
      "pnl": 399.5,
      "entry_commission": 0.1,
      "exit_commission": 0.1
    }
  ],
  "portfolio_values": [100000, 100050, ...],
  "dates": ["2016-01-04", "2016-01-05", ...],
  "bars": {
    "AAPL": [
      {"date": "2016-01-04", "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 1000000}
    ]
  }
}
```

**New CLI flag:** `--output-dir` (default: `output/`)

## Visualization: Portfolio Level

Top portion of the HTML report showing overall strategy health.

### Charts

1. **Summary Stats Panel** -- Table at the top: total return, Sharpe ratio, max drawdown, win rate, total trades, avg holding period. Sourced from `metrics`.

2. **Equity Curve** -- Line chart of daily NAV over time. Horizontal reference line at initial capital.

3. **Drawdown Chart** -- Area chart below equity curve showing drawdown from peak as negative percentage. Annotates max drawdown point.

4. **Monthly Returns Heatmap** -- Grid of months (columns) x years (rows), color-coded green/red by monthly return percentage. Shows seasonality and consistency.

5. **Trade PnL Distribution** -- Histogram of individual trade P&L values. Visualizes the shape of winners vs losers.

## Visualization: Trade Level

Below portfolio section, per-trade drill-down charts.

### Per-Trade Chart

For each completed trade (sorted by date):

- **Price chart** of the ticker for a window: 30 bars before entry through exit + 10 bars after
- **Entry marker** (green triangle up) and **exit marker** (red triangle down) with price annotations
- **Support levels** overlaid as horizontal dashed lines, computed from bars available at entry time
- **Signal values** shown in subtitle: proximity, strength, trend values at entry
- **Trade stats panel**: P&L, return %, holding period, exit reason (profit target / stop loss / time / technical breakdown)

### Navigation

- Ticker filter dropdown to focus on specific stocks
- Sort options: by P&L, by date, by ticker
- Collapsible sections per trade to manage page length

## New Dependency

- `plotly` -- for interactive HTML charts (hover, zoom, filter)

## File Changes

| File | Change |
|---|---|
| `scripts/run_backtest.py` | Add JSON output with bars, dates, and `--output-dir` flag |
| `scripts/visualize_backtest.py` | New file: reads JSON, generates HTML report |
| `pyproject.toml` | Add `plotly` to dependencies |
| `output/` | New directory for backtest data files (gitignored) |
| `.gitignore` | Add `output/` |
