# Backtest Visualization Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Save backtest results to JSON and generate a self-contained Plotly HTML report with portfolio-level and trade-level visualizations.

**Architecture:** Modify `scripts/run_backtest.py` to persist results + raw bars to a timestamped JSON file. Create `scripts/visualize_backtest.py` that reads the JSON and produces a single HTML file with interactive Plotly charts. The backtest runner also needs to track dates alongside portfolio values and capture signal values at entry time.

**Tech Stack:** Plotly (new dep), existing numpy/pandas stack, JSON for data persistence.

---

### Task 1: Add plotly dependency and output directory

**Files:**
- Modify: `pyproject.toml:6-27`
- Modify: `.gitignore:1-10`

**Step 1: Add plotly to pyproject.toml dependencies**

In `pyproject.toml`, add `plotly` to the dependencies list after `joblib`:

```python
    "joblib>=1.3,<2.0",
    "plotly>=5.18,<6.0",
```

**Step 2: Add output/ to .gitignore**

Append to `.gitignore`:

```
output/
```

**Step 3: Create output directory**

Run: `mkdir -p output`

**Step 4: Install updated dependencies**

Run: `pip install -e ".[dev]"`
Expected: plotly installs successfully

**Step 5: Commit**

```bash
git add pyproject.toml .gitignore
git commit -m "chore: add plotly dependency and gitignore output/"
```

---

### Task 2: Track dates in BacktestResult and capture signal values in trades

The backtest runner currently stores `portfolio_values` without corresponding dates, and trades don't include signal values or exit reasons. We need both for the visualization.

**Files:**
- Modify: `backtest/runner.py:10-17` (BacktestResult dataclass)
- Modify: `backtest/runner.py:58-169` (run method — add dates tracking)
- Modify: `scripts/run_backtest.py:136-239` (make_signals_fn — capture signal values and exit reason)
- Test: `tests/backtest/test_runner.py`

**Step 1: Write test for dates in BacktestResult**

Create or modify `tests/backtest/test_runner_dates.py`:

```python
from __future__ import annotations

from datetime import date

from backtest.runner import BacktestRunner, BacktestResult
from backtest.simulator import SimulatedExecutor


def test_backtest_result_includes_dates():
    """BacktestResult.dates should have one entry per portfolio_value after initial."""
    executor = SimulatedExecutor(slippage_bps=10, commission_per_share=0.005)
    runner = BacktestRunner(executor=executor, initial_capital=100_000)

    bars_by_ticker = {
        "TEST": [
            {"date": date(2024, 1, 2), "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000},
            {"date": date(2024, 1, 3), "open": 100, "high": 102, "low": 99, "close": 101, "volume": 1000},
            {"date": date(2024, 1, 4), "open": 101, "high": 103, "low": 100, "close": 102, "volume": 1000},
        ],
    }

    def no_signals(ticker, bars):
        return None

    class NoOpRisk:
        def check_entry(self, *args):
            pass

    result = runner.run(bars_by_ticker, no_signals, NoOpRisk())

    # portfolio_values has initial + one per date = 4 entries
    assert len(result.portfolio_values) == 4
    # dates has one per trading date = 3 entries
    assert len(result.dates) == 3
    assert result.dates[0] == date(2024, 1, 2)
    assert result.dates[-1] == date(2024, 1, 4)
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/backtest/test_runner_dates.py -v`
Expected: FAIL — `BacktestResult` has no `dates` attribute

**Step 3: Add dates field to BacktestResult and populate it in run()**

In `backtest/runner.py`, add `dates` field to the `BacktestResult` dataclass:

```python
@dataclass
class BacktestResult:
    """Container for backtest output."""

    trades: list[dict] = field(default_factory=list)
    portfolio_values: list[float] = field(default_factory=list)
    dates: list = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
```

In the `run()` method, collect dates. After the `portfolio_values.append(nav)` line (line ~157), add:

```python
            portfolio_values.append(nav)
            dates.append(current_date)
```

Initialize `dates` near `portfolio_values` (around line 61):

```python
        portfolio_values: list[float] = [self.initial_capital]
        dates: list = []
```

Include `dates` in the return value (around line 165):

```python
        return BacktestResult(
            trades=trades,
            portfolio_values=portfolio_values,
            dates=dates,
            metrics=metrics,
        )
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/backtest/test_runner_dates.py -v`
Expected: PASS

**Step 5: Run full backtest test suite to check for regressions**

Run: `pytest tests/backtest/ -v`
Expected: All pass

**Step 6: Add signal values and exit reason to make_signals_fn**

In `scripts/run_backtest.py`, modify `make_signals_fn` to include signal values in buy signals and exit reasons in sell signals.

For **buy signals** (around line 229), add `signals` dict to the return:

```python
            return {
                "action": "buy",
                "ticker": ticker,
                "limit_price": limit_price,
                "quantity": quantity,
                "sector": "Unknown",
                "signals": {
                    "proximity": {"value": proximity.value, "confidence": proximity.confidence},
                    "strength": {"value": strength.value, "confidence": strength.confidence},
                    "trend": {"value": trend.value, "confidence": trend.confidence},
                },
            }
```

For **sell signals**, add `exit_reason` to the return dict. Track the reason with a variable before the `if should_sell:` block:

```python
            exit_reason = "unknown"

            # 1. Profit target hit
            if pct_change >= PROFIT_TARGET_PCT:
                should_sell = True
                exit_reason = "profit_target"

            # 2. Stop loss hit
            elif pct_change <= STOP_LOSS_PCT:
                should_sell = True
                exit_reason = "stop_loss"

            # 3. Time-based exit
            elif holding_bars >= MAX_HOLDING_BARS:
                should_sell = True
                exit_reason = "time_exit"

            # 4. Technical breakdown
            else:
                data = _build_data(bars)
                try:
                    proximity = proximity_signal.compute(data)
                    trend = trend_signal.compute(data)
                    if proximity.value < -0.1 and trend.value < -0.1:
                        should_sell = True
                        exit_reason = "technical_breakdown"
                except Exception:
                    pass

            if should_sell:
                del tracked[ticker]
                return {
                    "action": "sell",
                    "ticker": ticker,
                    "limit_price": current_price,
                    "quantity": 0,
                    "sector": "Unknown",
                    "exit_reason": exit_reason,
                }
```

**Step 7: Pass signal values and exit reasons through to trades in BacktestRunner**

In `backtest/runner.py`, the `run()` method should propagate these extra fields from the signal dict into the trade dict.

For **buy** entries (around line 140), store signals on the position. First, add `entry_signals` to `_Position`:

```python
@dataclass
class _Position:
    """Internal position tracker."""

    ticker: str
    quantity: int
    entry_price: float
    entry_date: Any
    entry_commission: float
    entry_signals: dict = field(default_factory=dict)
```

When creating the position (line ~140):

```python
                    positions[ticker] = _Position(
                        ticker=ticker,
                        quantity=fill["quantity"],
                        entry_price=fill["fill_price"],
                        entry_date=fill["date"],
                        entry_commission=fill["commission"],
                        entry_signals=signal.get("signals", {}),
                    )
```

For **sell** trades (line ~100), include both entry signals and exit reason:

```python
                    trades.append({
                        "ticker": ticker,
                        "entry_date": pos.entry_date,
                        "exit_date": fill["date"],
                        "entry_price": pos.entry_price,
                        "exit_price": fill["fill_price"],
                        "quantity": pos.quantity,
                        "pnl": pnl,
                        "entry_commission": pos.entry_commission,
                        "exit_commission": fill["commission"],
                        "entry_signals": pos.entry_signals,
                        "exit_reason": signal.get("exit_reason", "unknown"),
                    })
```

**Step 8: Run tests**

Run: `pytest tests/backtest/ -v`
Expected: All pass (existing tests shouldn't break since new fields have defaults)

**Step 9: Commit**

```bash
git add backtest/runner.py scripts/run_backtest.py tests/backtest/test_runner_dates.py
git commit -m "feat: track dates, signal values, and exit reasons in backtest results"
```

---

### Task 3: Add JSON export to run_backtest.py

**Files:**
- Modify: `scripts/run_backtest.py:290-357` (main function)

**Step 1: Write test for save_results function**

Create `tests/backtest/test_save_results.py`:

```python
from __future__ import annotations

import json
import os
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
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/backtest/test_save_results.py -v`
Expected: FAIL — `save_results` not found

**Step 3: Implement save_results in run_backtest.py**

Add this function to `scripts/run_backtest.py` (after `print_results`, before `main`):

```python
def save_results(
    config: dict,
    trades: list[dict],
    portfolio_values: list[float],
    dates: list,
    metrics: dict,
    bars: dict[str, list[dict]],
    output_dir: str = "output",
) -> str:
    """Save backtest results to a timestamped JSON file.

    Returns the path to the saved file.
    """
    import json as _json
    from pathlib import Path

    Path(output_dir).mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"backtest_{timestamp}.json"
    filepath = Path(output_dir) / filename

    def _serialize(obj):
        if isinstance(obj, date):
            return obj.isoformat()
        raise TypeError(f"Object of type {type(obj)} is not JSON serializable")

    payload = {
        "config": config,
        "metrics": metrics,
        "trades": trades,
        "portfolio_values": portfolio_values,
        "dates": dates,
        "bars": bars,
    }

    with open(filepath, "w") as f:
        _json.dump(payload, f, default=_serialize, indent=2)

    print(f"\n  Results saved to: {filepath}")
    return str(filepath)
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/backtest/test_save_results.py -v`
Expected: PASS

**Step 5: Wire save_results into main()**

Add `--output-dir` argument to the argparse block (after `--ib-port`):

```python
    parser.add_argument("--output-dir", default="output",
                        help="Directory for output files (default: output)")
```

After `print_results(result, elapsed)` in `main()`, add:

```python
    # 5. Save results to JSON
    print("\nStep 5: Saving results...")
    save_results(
        config={
            "tickers": tickers,
            "years": args.years,
            "initial_capital": args.capital,
            "slippage_bps": args.slippage_bps,
            "commission_per_share": args.commission,
        },
        trades=result.trades,
        portfolio_values=result.portfolio_values,
        dates=result.dates,
        metrics=result.metrics,
        bars=bars_by_ticker,
        output_dir=args.output_dir,
    )
```

**Step 6: Run tests**

Run: `pytest tests/backtest/ -v`
Expected: All pass

**Step 7: Commit**

```bash
git add scripts/run_backtest.py tests/backtest/test_save_results.py
git commit -m "feat: save backtest results to JSON with --output-dir flag"
```

---

### Task 4: Create visualize_backtest.py — portfolio-level charts

**Files:**
- Create: `scripts/visualize_backtest.py`
- Test: `tests/backtest/test_visualize_backtest.py`

**Step 1: Write test for portfolio chart generation**

Create `tests/backtest/test_visualize_backtest.py`:

```python
from __future__ import annotations

import json
from pathlib import Path


def _make_sample_data(tmp_path: Path) -> Path:
    """Create a minimal backtest JSON for testing."""
    data = {
        "config": {
            "tickers": ["AAPL", "MSFT"],
            "years": 1,
            "initial_capital": 100000,
            "slippage_bps": 10,
            "commission_per_share": 0.005,
        },
        "metrics": {
            "total_return": 0.05,
            "sharpe_ratio": 1.2,
            "max_drawdown": 0.03,
            "win_rate": 0.6,
            "avg_holding_period_days": 15.0,
            "total_trades": 5,
        },
        "trades": [
            {
                "ticker": "AAPL",
                "entry_date": "2024-03-15",
                "exit_date": "2024-04-10",
                "entry_price": 170.0,
                "exit_price": 180.0,
                "quantity": 29,
                "pnl": 289.71,
                "entry_commission": 0.15,
                "exit_commission": 0.15,
                "entry_signals": {
                    "proximity": {"value": 0.6, "confidence": 0.8},
                    "strength": {"value": 0.3, "confidence": 0.5},
                    "trend": {"value": 0.2, "confidence": 0.4},
                },
                "exit_reason": "profit_target",
            },
            {
                "ticker": "MSFT",
                "entry_date": "2024-05-01",
                "exit_date": "2024-05-20",
                "entry_price": 400.0,
                "exit_price": 390.0,
                "quantity": 12,
                "pnl": -120.12,
                "entry_commission": 0.06,
                "exit_commission": 0.06,
                "entry_signals": {
                    "proximity": {"value": 0.5, "confidence": 0.7},
                    "strength": {"value": 0.1, "confidence": 0.3},
                    "trend": {"value": 0.05, "confidence": 0.2},
                },
                "exit_reason": "stop_loss",
            },
        ],
        "portfolio_values": [100000 + i * 10 for i in range(252)],
        "dates": [f"2024-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}" for i in range(252)],
        "bars": {
            "AAPL": [
                {
                    "date": f"2024-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}",
                    "open": 170 + i * 0.1,
                    "high": 171 + i * 0.1,
                    "low": 169 + i * 0.1,
                    "close": 170.5 + i * 0.1,
                    "volume": 1000000,
                }
                for i in range(252)
            ],
            "MSFT": [
                {
                    "date": f"2024-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}",
                    "open": 400 + i * 0.05,
                    "high": 401 + i * 0.05,
                    "low": 399 + i * 0.05,
                    "close": 400.2 + i * 0.05,
                    "volume": 800000,
                }
                for i in range(252)
            ],
        },
    }
    json_path = tmp_path / "backtest_test.json"
    json_path.write_text(json.dumps(data))
    return json_path


def test_generate_report_creates_html(tmp_path):
    """generate_report produces an HTML file with expected content."""
    from scripts.visualize_backtest import generate_report

    json_path = _make_sample_data(tmp_path)
    output_path = tmp_path / "report.html"

    generate_report(str(json_path), str(output_path))

    assert output_path.exists()
    html = output_path.read_text()
    assert "plotly" in html.lower()
    assert "Equity Curve" in html
    assert "Drawdown" in html
    assert "AAPL" in html
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/backtest/test_visualize_backtest.py::test_generate_report_creates_html -v`
Expected: FAIL — module not found

**Step 3: Create scripts/visualize_backtest.py with portfolio charts**

Create `scripts/visualize_backtest.py`:

```python
#!/usr/bin/env python3
"""Generate an interactive HTML report from backtest results.

Usage:
    python scripts/visualize_backtest.py output/backtest_YYYYMMDD_HHMMSS.json
    python scripts/visualize_backtest.py output/backtest_YYYYMMDD_HHMMSS.json -o report.html
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

import plotly.graph_objects as go
from plotly.subplots import make_subplots


def load_data(json_path: str) -> dict:
    """Load backtest results from JSON file."""
    with open(json_path) as f:
        return json.load(f)


def _make_summary_html(metrics: dict, config: dict) -> str:
    """Render summary stats as an HTML table."""
    rows = [
        ("Total Return", f"{metrics.get('total_return', 0):.2%}"),
        ("Sharpe Ratio", f"{metrics.get('sharpe_ratio', 0):.2f}"),
        ("Max Drawdown", f"{metrics.get('max_drawdown', 0):.2%}"),
        ("Win Rate", f"{metrics.get('win_rate', 0):.2%}"),
        ("Total Trades", str(metrics.get("total_trades", 0))),
        ("Avg Holding Period", f"{metrics.get('avg_holding_period_days', 0):.1f} days"),
        ("Initial Capital", f"${config.get('initial_capital', 0):,.0f}"),
        ("Tickers", str(len(config.get("tickers", [])))),
        ("Slippage", f"{config.get('slippage_bps', 0)} bps"),
    ]
    cells = "".join(
        f'<div style="text-align:center;padding:12px 20px;'
        f'background:#1e1e2e;border-radius:8px;min-width:140px">'
        f'<div style="color:#888;font-size:12px">{label}</div>'
        f'<div style="color:#fff;font-size:20px;font-weight:bold;margin-top:4px">{val}</div>'
        f"</div>"
        for label, val in rows
    )
    return (
        f'<div style="display:flex;flex-wrap:wrap;gap:12px;'
        f'justify-content:center;margin:20px 0">{cells}</div>'
    )


def _make_equity_curve(dates: list[str], values: list[float]) -> go.Figure:
    """Create equity curve line chart."""
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=dates, y=values[1:],  # skip initial capital (no date for it)
        mode="lines", name="Portfolio Value",
        line=dict(color="#4ecdc4", width=2),
    ))
    fig.add_hline(
        y=values[0], line_dash="dash", line_color="#888",
        annotation_text=f"Initial: ${values[0]:,.0f}",
    )
    fig.update_layout(
        title="Equity Curve",
        xaxis_title="Date", yaxis_title="Portfolio Value ($)",
        template="plotly_dark", height=400,
    )
    return fig


def _make_drawdown_chart(dates: list[str], values: list[float]) -> go.Figure:
    """Create drawdown area chart."""
    nav = values[1:]  # skip initial
    peak = nav[0]
    drawdowns = []
    for v in nav:
        if v > peak:
            peak = v
        dd = (v - peak) / peak if peak > 0 else 0
        drawdowns.append(dd)

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=dates, y=[d * 100 for d in drawdowns],
        fill="tozeroy", name="Drawdown",
        line=dict(color="#ff6b6b", width=1),
        fillcolor="rgba(255,107,107,0.3)",
    ))

    # Annotate max drawdown
    if drawdowns:
        min_dd = min(drawdowns)
        min_idx = drawdowns.index(min_dd)
        fig.add_annotation(
            x=dates[min_idx], y=min_dd * 100,
            text=f"Max: {min_dd:.2%}",
            showarrow=True, arrowhead=2, arrowcolor="#ff6b6b",
        )

    fig.update_layout(
        title="Drawdown from Peak",
        xaxis_title="Date", yaxis_title="Drawdown (%)",
        template="plotly_dark", height=300,
    )
    return fig


def _make_monthly_returns_heatmap(dates: list[str], values: list[float]) -> go.Figure:
    """Create monthly returns heatmap (years x months)."""
    nav = values[1:]
    if len(nav) < 2 or len(dates) < 2:
        return go.Figure()

    # Compute daily returns keyed by (year, month)
    monthly: dict[tuple[int, int], list[float]] = defaultdict(list)
    for i in range(1, len(nav)):
        d = dates[i] if isinstance(dates[i], str) else str(dates[i])
        dt = datetime.strptime(d[:10], "%Y-%m-%d")
        ret = (nav[i] - nav[i - 1]) / nav[i - 1] if nav[i - 1] != 0 else 0
        monthly[(dt.year, dt.month)].append(ret)

    # Aggregate to monthly returns (compound daily)
    monthly_ret: dict[tuple[int, int], float] = {}
    for key, daily_rets in monthly.items():
        compound = 1.0
        for r in daily_rets:
            compound *= (1 + r)
        monthly_ret[key] = compound - 1

    if not monthly_ret:
        return go.Figure()

    years = sorted(set(k[0] for k in monthly_ret))
    months = list(range(1, 13))
    month_labels = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

    z = []
    for y in years:
        row = []
        for m in months:
            val = monthly_ret.get((y, m))
            row.append(val * 100 if val is not None else None)
        z.append(row)

    fig = go.Figure(data=go.Heatmap(
        z=z, x=month_labels, y=[str(y) for y in years],
        colorscale=[[0, "#ff6b6b"], [0.5, "#1e1e2e"], [1, "#4ecdc4"]],
        zmid=0,
        text=[[f"{v:.1f}%" if v is not None else "" for v in row] for row in z],
        texttemplate="%{text}",
        hovertemplate="Year: %{y}<br>Month: %{x}<br>Return: %{text}<extra></extra>",
    ))
    fig.update_layout(
        title="Monthly Returns (%)",
        template="plotly_dark", height=max(200, len(years) * 40 + 100),
    )
    return fig


def _make_pnl_distribution(trades: list[dict]) -> go.Figure:
    """Create histogram of trade PnL."""
    pnls = [t["pnl"] for t in trades]
    colors = ["#4ecdc4" if p >= 0 else "#ff6b6b" for p in pnls]

    fig = go.Figure()
    fig.add_trace(go.Histogram(
        x=pnls, nbinsx=30, name="Trade PnL",
        marker_color="#4ecdc4",
    ))
    fig.add_vline(x=0, line_dash="dash", line_color="#888")
    fig.update_layout(
        title="Trade PnL Distribution",
        xaxis_title="PnL ($)", yaxis_title="Count",
        template="plotly_dark", height=300,
    )
    return fig


def _make_trade_chart(
    trade: dict,
    bars: list[dict],
    all_bar_dates: list[str],
) -> go.Figure | None:
    """Create a price chart for a single trade with entry/exit markers and support levels."""
    entry_date = trade["entry_date"]
    exit_date = trade["exit_date"]

    # Find index range: 30 bars before entry to 10 bars after exit
    bar_date_list = [b["date"] for b in bars]

    try:
        entry_idx = bar_date_list.index(entry_date)
    except ValueError:
        # Find closest date
        entry_idx = min(range(len(bar_date_list)),
                        key=lambda i: abs(
                            datetime.strptime(bar_date_list[i][:10], "%Y-%m-%d")
                            - datetime.strptime(entry_date[:10], "%Y-%m-%d")
                        ))

    try:
        exit_idx = bar_date_list.index(exit_date)
    except ValueError:
        exit_idx = min(range(len(bar_date_list)),
                       key=lambda i: abs(
                           datetime.strptime(bar_date_list[i][:10], "%Y-%m-%d")
                           - datetime.strptime(exit_date[:10], "%Y-%m-%d")
                       ))

    start = max(0, entry_idx - 30)
    end = min(len(bars), exit_idx + 11)
    window = bars[start:end]

    if not window:
        return None

    dates_w = [b["date"] for b in window]
    closes = [b["close"] for b in window]
    highs = [b["high"] for b in window]
    lows_w = [b["low"] for b in window]
    opens = [b["open"] for b in window]

    # Build candlestick
    fig = go.Figure()
    fig.add_trace(go.Candlestick(
        x=dates_w, open=opens, high=highs, low=lows_w, close=closes,
        name="Price",
    ))

    # Entry marker
    fig.add_trace(go.Scatter(
        x=[entry_date], y=[trade["entry_price"]],
        mode="markers+text",
        marker=dict(symbol="triangle-up", size=14, color="#4ecdc4"),
        text=[f"Buy ${trade['entry_price']:.2f}"],
        textposition="top center",
        textfont=dict(color="#4ecdc4", size=10),
        name="Entry",
    ))

    # Exit marker
    fig.add_trace(go.Scatter(
        x=[exit_date], y=[trade["exit_price"]],
        mode="markers+text",
        marker=dict(symbol="triangle-down", size=14, color="#ff6b6b"),
        text=[f"Sell ${trade['exit_price']:.2f}"],
        textposition="bottom center",
        textfont=dict(color="#ff6b6b", size=10),
        name="Exit",
    ))

    # Compute support levels from bars available at entry time
    entry_bars = bars[:entry_idx + 1]
    if len(entry_bars) > 10:
        from services.signal_generation.technical import find_support_levels
        entry_data = {
            "low": [b["low"] for b in entry_bars],
            "close": [b["close"] for b in entry_bars],
        }
        levels = find_support_levels(entry_data)
        price_range = max(closes) - min(lows_w) if closes and lows_w else 0
        for lvl in levels[:5]:  # top 5 support levels
            if min(lows_w) - price_range * 0.1 <= lvl <= max(highs) + price_range * 0.1:
                fig.add_hline(
                    y=lvl, line_dash="dot", line_color="#888",
                    annotation_text=f"Support ${lvl:.2f}",
                    annotation_font_size=9,
                    annotation_font_color="#888",
                )

    # Build subtitle with signal values and trade stats
    signals = trade.get("entry_signals", {})
    sig_text = "  |  ".join(
        f"{name}: {vals.get('value', 0):.2f} (conf: {vals.get('confidence', 0):.2f})"
        for name, vals in signals.items()
    ) if signals else "No signal data"

    ret_pct = (trade["exit_price"] - trade["entry_price"]) / trade["entry_price"]
    exit_reason = trade.get("exit_reason", "unknown").replace("_", " ").title()
    holding = ""
    try:
        d1 = datetime.strptime(entry_date[:10], "%Y-%m-%d")
        d2 = datetime.strptime(exit_date[:10], "%Y-%m-%d")
        holding = f"  |  Holding: {(d2-d1).days}d"
    except (ValueError, TypeError):
        pass

    pnl_color = "#4ecdc4" if trade["pnl"] >= 0 else "#ff6b6b"

    fig.update_layout(
        title=dict(
            text=(
                f"<b>{trade['ticker']}</b>  "
                f"<span style='color:{pnl_color}'>"
                f"PnL: ${trade['pnl']:+,.2f} ({ret_pct:+.2%})</span>  |  "
                f"Exit: {exit_reason}{holding}<br>"
                f"<span style='font-size:11px;color:#888'>"
                f"Signals at entry: {sig_text}</span>"
            ),
        ),
        template="plotly_dark", height=400,
        xaxis_rangeslider_visible=False,
        showlegend=False,
    )
    return fig


def generate_report(json_path: str, output_path: str = "") -> str:
    """Generate a self-contained HTML report from backtest JSON.

    Args:
        json_path: Path to the backtest JSON file.
        output_path: Path for the output HTML. If empty, uses same dir as JSON.

    Returns:
        Path to the generated HTML file.
    """
    data = load_data(json_path)
    config = data["config"]
    metrics = data["metrics"]
    trades = data["trades"]
    portfolio_values = data["portfolio_values"]
    dates = data["dates"]
    bars = data["bars"]

    if not output_path:
        output_path = str(Path(json_path).with_suffix(".html"))

    # --- Portfolio-level charts ---
    summary_html = _make_summary_html(metrics, config)
    equity_fig = _make_equity_curve(dates, portfolio_values)
    drawdown_fig = _make_drawdown_chart(dates, portfolio_values)
    monthly_fig = _make_monthly_returns_heatmap(dates, portfolio_values)
    pnl_fig = _make_pnl_distribution(trades)

    # --- Trade-level charts ---
    trade_figs: list[tuple[dict, go.Figure]] = []
    all_bar_dates = dates
    for trade in sorted(trades, key=lambda t: t["entry_date"]):
        ticker = trade["ticker"]
        ticker_bars = bars.get(ticker, [])
        fig = _make_trade_chart(trade, ticker_bars, all_bar_dates)
        if fig is not None:
            trade_figs.append((trade, fig))

    # --- Assemble HTML ---
    # Get unique tickers for filter
    tickers = sorted(set(t["ticker"] for t in trades))

    plotly_js = go.Figure().to_html(include_plotlyjs="cdn", full_html=False)
    # Extract just the CDN script tag
    cdn_tag = '<script src="https://cdn.plot.ly/plotly-latest.min.js"></script>'

    charts_html = []
    charts_html.append(equity_fig.to_html(include_plotlyjs=False, full_html=False))
    charts_html.append(drawdown_fig.to_html(include_plotlyjs=False, full_html=False))
    charts_html.append(monthly_fig.to_html(include_plotlyjs=False, full_html=False))
    charts_html.append(pnl_fig.to_html(include_plotlyjs=False, full_html=False))

    # Trade filter + sort controls
    ticker_options = "".join(f'<option value="{t}">{t}</option>' for t in tickers)
    filter_html = f"""
    <div style="margin:30px 0 10px;display:flex;gap:15px;align-items:center;flex-wrap:wrap">
        <h2 style="color:#fff;margin:0">Trade Details</h2>
        <select id="tickerFilter" onchange="filterTrades()"
                style="padding:6px 12px;background:#1e1e2e;color:#fff;border:1px solid #444;border-radius:4px">
            <option value="all">All Tickers</option>
            {ticker_options}
        </select>
        <select id="sortBy" onchange="filterTrades()"
                style="padding:6px 12px;background:#1e1e2e;color:#fff;border:1px solid #444;border-radius:4px">
            <option value="date">Sort by Date</option>
            <option value="pnl_desc">Sort by PnL (Best First)</option>
            <option value="pnl_asc">Sort by PnL (Worst First)</option>
            <option value="ticker">Sort by Ticker</option>
        </select>
    </div>
    """

    trade_html_parts = []
    for i, (trade, fig) in enumerate(trade_figs):
        pnl = trade["pnl"]
        chart_div = fig.to_html(include_plotlyjs=False, full_html=False)
        trade_html_parts.append(
            f'<div class="trade-card" data-ticker="{trade["ticker"]}" '
            f'data-pnl="{pnl}" data-date="{trade["entry_date"]}" '
            f'style="margin:10px 0;border:1px solid #333;border-radius:8px;overflow:hidden">'
            f"{chart_div}</div>"
        )

    filter_script = """
    <script>
    function filterTrades() {
        var ticker = document.getElementById('tickerFilter').value;
        var sortBy = document.getElementById('sortBy').value;
        var cards = Array.from(document.querySelectorAll('.trade-card'));

        // Sort
        cards.sort(function(a, b) {
            if (sortBy === 'pnl_desc') return parseFloat(b.dataset.pnl) - parseFloat(a.dataset.pnl);
            if (sortBy === 'pnl_asc') return parseFloat(a.dataset.pnl) - parseFloat(b.dataset.pnl);
            if (sortBy === 'ticker') return a.dataset.ticker.localeCompare(b.dataset.ticker);
            return a.dataset.date.localeCompare(b.dataset.date);
        });

        var container = document.getElementById('tradeContainer');
        cards.forEach(function(card) {
            container.appendChild(card);
            if (ticker === 'all' || card.dataset.ticker === ticker) {
                card.style.display = 'block';
            } else {
                card.style.display = 'none';
            }
        });
    }
    </script>
    """

    html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Backtest Report</title>
    {cdn_tag}
    <style>
        body {{ background: #121212; color: #fff; font-family: -apple-system, sans-serif; margin: 0; padding: 20px; }}
        h1 {{ text-align: center; }}
    </style>
</head>
<body>
    <h1>Backtest Report</h1>
    {summary_html}
    {"".join(charts_html)}
    {filter_html}
    <div id="tradeContainer">
        {"".join(trade_html_parts)}
    </div>
    {filter_script}
</body>
</html>"""

    Path(output_path).write_text(html)
    print(f"Report saved to: {output_path}")
    return output_path


def main():
    parser = argparse.ArgumentParser(description="Generate backtest visualization report")
    parser.add_argument("json_path", help="Path to backtest JSON file")
    parser.add_argument("-o", "--output", default="",
                        help="Output HTML path (default: same as JSON with .html)")
    args = parser.parse_args()

    if not Path(args.json_path).exists():
        print(f"ERROR: File not found: {args.json_path}")
        sys.exit(1)

    generate_report(args.json_path, args.output)


if __name__ == "__main__":
    main()
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/backtest/test_visualize_backtest.py -v`
Expected: PASS

**Step 5: Run full test suite**

Run: `pytest tests/ -v`
Expected: All pass

**Step 6: Commit**

```bash
git add scripts/visualize_backtest.py tests/backtest/test_visualize_backtest.py
git commit -m "feat: add backtest visualization script with portfolio and trade charts"
```

---

### Task 5: Run the backtest and generate the report

This is a manual integration step that requires IB Gateway to be running.

**Step 1: Verify IB Gateway is accessible**

Run: `python -c "from ib_insync import IB; ib = IB(); ib.connect('127.0.0.1', 7497, clientId=99, timeout=5); print('Connected:', ib.managedAccounts()); ib.disconnect()"`
Expected: Prints account info

**Step 2: Run backtest (start small to validate)**

Run: `python scripts/run_backtest.py --tickers 5 --years 2 --output-dir output`
Expected: Fetches data, runs backtest, prints results, saves JSON to `output/backtest_*.json`

**Step 3: Generate the HTML report**

Run: `python scripts/visualize_backtest.py output/backtest_*.json`
Expected: Creates `output/backtest_*.html`

**Step 4: Open the report**

Run: `open output/backtest_*.html`
Expected: Browser opens with interactive report showing equity curve, drawdown, monthly heatmap, PnL distribution, and trade-level charts

**Step 5: If happy with small run, do full backtest**

Run: `python scripts/run_backtest.py --tickers 50 --years 10 --output-dir output`
Then: `python scripts/visualize_backtest.py output/backtest_*.json`

**Step 6: Commit any tweaks**

```bash
git add -A && git commit -m "chore: finalize backtest visualization pipeline"
```
