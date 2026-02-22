# Phase 8: Multi-Portfolio Visualization

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Update the backtest visualization script to handle multi-portfolio JSON output, showing per-strategy equity curves, strategy comparison charts, and aggregate views alongside existing single-portfolio support.

**Architecture:** Detect format via `"portfolios"` key presence. Multi-portfolio mode adds: overlaid equity curves (one per strategy + bold aggregate), strategy comparison bar charts, per-strategy metrics table, and portfolio-filtered trade charts. Single-portfolio mode unchanged.

**Tech Stack:** Python, Plotly (existing dependency)

---

## Context

### What we have

- `scripts/visualize_backtest.py` — 618-line report generator handling single-portfolio JSON:
  - `_summary_panel(data)` — HTML cards showing metrics
  - `_equity_curve(data)` — single equity line chart
  - `_drawdown_chart(data)` — drawdown area chart
  - `_monthly_returns_heatmap(data)` — monthly return heatmap
  - `_trade_pnl_histogram(data)` — trade PnL distribution
  - `_trade_chart(trade, bars)` — per-trade candlestick chart
  - `generate_report(json_path, output_path)` — main entry point

- `tests/backtest/test_visualize_backtest.py` — 2 tests with sample data builder

### Single-portfolio JSON format

```json
{
  "config": { "tickers": [...], "initial_capital": 100000, ... },
  "metrics": { "total_return": 0.06, "sharpe_ratio": 1.12, ... },
  "trades": [ ... ],
  "portfolio_values": [100000, 100050, ...],
  "dates": ["2023-01-03", ...],
  "bars": { "AAPL": [...], ... }
}
```

### Multi-portfolio JSON format

```json
{
  "config": { "total_capital": 100000, ... },
  "portfolios": {
    "mean_reversion": {
      "config": { "capital": 12000 },
      "trades": [...],
      "portfolio_values": [12000, 12010, ...],
      "dates": ["2023-01-03", ...],
      "metrics": { "total_return": 0.06, ... }
    },
    ...
  },
  "aggregate": {
    "portfolio_values": [100000, 100500, ...],
    "trades": [{ "portfolio": "momentum", ... }, ...],
    "dates": ["2023-01-03", ...],
    "metrics": { "total_return": 0.19, ... }
  },
  "bars": { ... }
}
```

---

## Task 1: Format Detection and Multi-Portfolio Summary

**Files:**
- Modify: `scripts/visualize_backtest.py`
- Modify: `tests/backtest/test_visualize_backtest.py`

### Step 1: Write failing tests

Add to `tests/backtest/test_visualize_backtest.py`:

```python
def _build_multi_portfolio_json(tmp_path: Path) -> Path:
    """Build a sample multi-portfolio backtest JSON file."""
    start_date = date(2023, 1, 3)
    num_days = 252

    aapl_bars = _generate_sample_bars("AAPL", start_date, num_days, 150.0)
    msft_bars = _generate_sample_bars("MSFT", start_date, num_days, 250.0)
    xlk_bars = _generate_sample_bars("XLK", start_date, num_days, 170.0)

    dates = [b["date"] for b in aapl_bars]

    # Mean-reversion portfolio
    mr_capital = 12_000.0
    mr_values = [mr_capital]
    nav = mr_capital
    for i in range(len(dates)):
        nav += (i - 126) * 0.2
        mr_values.append(round(nav, 2))

    # Momentum portfolio
    mom_capital = 18_000.0
    mom_values = [mom_capital]
    nav = mom_capital
    for i in range(len(dates)):
        nav += (i - 100) * 0.5
        mom_values.append(round(nav, 2))

    # Aggregate
    agg_values = [mr_values[i] + mom_values[i] for i in range(len(mr_values))]

    mr_trades = [
        {
            "ticker": "AAPL",
            "entry_date": dates[60],
            "exit_date": dates[90],
            "entry_price": float(aapl_bars[60]["close"]),
            "exit_price": float(aapl_bars[90]["close"]),
            "quantity": 5,
            "pnl": 120.50,
            "entry_commission": 0.03,
            "exit_commission": 0.03,
            "entry_signals": {},
            "exit_reason": "trailing_stop",
            "portfolio": "mean_reversion",
        },
    ]

    mom_trades = [
        {
            "ticker": "MSFT",
            "entry_date": dates[100],
            "exit_date": dates[140],
            "entry_price": float(msft_bars[100]["close"]),
            "exit_price": float(msft_bars[140]["close"]),
            "quantity": 8,
            "pnl": -90.10,
            "entry_commission": 0.04,
            "exit_commission": 0.04,
            "entry_signals": {},
            "exit_reason": "stop_loss",
            "portfolio": "momentum",
        },
        {
            "ticker": "XLK",
            "entry_date": dates[50],
            "exit_date": dates[80],
            "entry_price": float(xlk_bars[50]["close"]),
            "exit_price": float(xlk_bars[80]["close"]),
            "quantity": 10,
            "pnl": 200.00,
            "entry_commission": 0.05,
            "exit_commission": 0.05,
            "entry_signals": {},
            "exit_reason": "trailing_stop",
            "portfolio": "momentum",
        },
    ]

    data = {
        "config": {
            "total_capital": 30_000,
            "years": 1,
            "slippage_bps": 10,
            "commission_per_share": 0.005,
        },
        "portfolios": {
            "mean_reversion": {
                "config": {"capital": mr_capital},
                "trades": mr_trades,
                "portfolio_values": mr_values,
                "dates": dates,
                "metrics": {
                    "total_return": 0.04,
                    "sharpe_ratio": 0.95,
                    "max_drawdown": -0.03,
                    "win_rate": 1.0,
                    "avg_holding_period_days": 30.0,
                    "total_trades": 1,
                },
            },
            "momentum": {
                "config": {"capital": mom_capital},
                "trades": mom_trades,
                "portfolio_values": mom_values,
                "dates": dates,
                "metrics": {
                    "total_return": 0.08,
                    "sharpe_ratio": 1.30,
                    "max_drawdown": -0.05,
                    "win_rate": 0.50,
                    "avg_holding_period_days": 35.0,
                    "total_trades": 2,
                },
            },
        },
        "aggregate": {
            "portfolio_values": agg_values,
            "trades": mr_trades + mom_trades,
            "dates": dates,
            "metrics": {
                "total_return": 0.065,
                "sharpe_ratio": 1.15,
                "max_drawdown": -0.04,
                "win_rate": 0.67,
                "avg_holding_period_days": 33.0,
                "total_trades": 3,
            },
        },
        "bars": {
            "AAPL": aapl_bars,
            "MSFT": msft_bars,
            "XLK": xlk_bars,
        },
    }

    json_path = tmp_path / "test_multi_backtest.json"
    with open(json_path, "w") as f:
        json.dump(data, f, indent=2)

    return json_path


def test_multi_portfolio_report_creates_html(tmp_path):
    """generate_report handles multi-portfolio JSON format."""
    from scripts.visualize_backtest import generate_report

    json_path = _build_multi_portfolio_json(tmp_path)
    output_path = str(tmp_path / "multi_report.html")

    result_path = generate_report(str(json_path), output_path)

    assert Path(result_path).exists()
    html = Path(result_path).read_text()

    # Must contain Plotly
    assert "plotly" in html.lower()

    # Must contain strategy names
    assert "mean_reversion" in html
    assert "momentum" in html

    # Must contain key chart sections
    assert "Equity Curve" in html
    assert "Drawdown" in html

    # Must contain aggregate section
    assert "Aggregate" in html or "AGGREGATE" in html

    # Must contain per-strategy comparison
    assert "Strategy Comparison" in html


def test_multi_portfolio_has_strategy_equity_curves(tmp_path):
    """Multi-portfolio report should show per-strategy equity curves."""
    from scripts.visualize_backtest import generate_report

    json_path = _build_multi_portfolio_json(tmp_path)
    output_path = str(tmp_path / "multi_report.html")

    generate_report(str(json_path), output_path)
    html = Path(output_path).read_text()

    # Each strategy should appear as a trace name in equity chart
    assert "mean_reversion" in html
    assert "momentum" in html


def test_multi_portfolio_has_portfolio_filter(tmp_path):
    """Multi-portfolio trade charts should support filtering by portfolio."""
    from scripts.visualize_backtest import generate_report

    json_path = _build_multi_portfolio_json(tmp_path)
    output_path = str(tmp_path / "multi_report.html")

    generate_report(str(json_path), output_path)
    html = Path(output_path).read_text()

    # Portfolio filter dropdown should exist
    assert "portfolio-filter" in html


def test_single_portfolio_still_works(tmp_path):
    """Existing single-portfolio format should still work after changes."""
    from scripts.visualize_backtest import generate_report

    json_path = _build_sample_json(tmp_path)
    output_path = str(tmp_path / "report.html")

    result_path = generate_report(str(json_path), output_path)

    assert Path(result_path).exists()
    html = Path(result_path).read_text()
    assert "Equity Curve" in html
    assert "AAPL" in html
```

### Step 2: Run tests to verify they fail

Run: `pytest tests/backtest/test_visualize_backtest.py -v`
Expected: FAIL — new tests fail because `generate_report()` doesn't handle multi-portfolio format

### Step 3: Add format detection and multi-portfolio summary panel

In `scripts/visualize_backtest.py`, modify `generate_report()` to detect format and add `_multi_summary_panel()`:

```python
def _is_multi_portfolio(data: dict[str, Any]) -> bool:
    """Check if data uses multi-portfolio format."""
    return "portfolios" in data and isinstance(data["portfolios"], dict)


def _multi_summary_panel(data: dict[str, Any]) -> str:
    """Return an HTML block with aggregate + per-strategy metrics."""
    aggregate = data["aggregate"]
    agg_metrics = aggregate["metrics"]
    portfolios = data["portfolios"]
    config = data["config"]

    # Aggregate cards
    cards = [
        ("Total Return", f"{agg_metrics.get('total_return', 0):.2%}"),
        ("Sharpe Ratio", f"{agg_metrics.get('sharpe_ratio', 0):.2f}"),
        ("Max Drawdown", f"{agg_metrics.get('max_drawdown', 0):.2%}"),
        ("Win Rate", f"{agg_metrics.get('win_rate', 0):.2%}"),
        ("Total Trades", f"{agg_metrics.get('total_trades', 0)}"),
        ("Strategies", f"{len(portfolios)}"),
        ("Total Capital", f"${config.get('total_capital', 0):,.0f}"),
    ]

    html_cards = ""
    for label, value in cards:
        html_cards += (
            f'<div style="background:{DARK_BG};border-radius:8px;padding:16px 20px;'
            f'min-width:140px;text-align:center;">'
            f'<div style="font-size:12px;color:#aaa;margin-bottom:4px;">{label}</div>'
            f'<div style="font-size:22px;font-weight:bold;color:{TEXT_COLOR};">{value}</div>'
            f"</div>\n"
        )

    # Per-strategy table
    rows = ""
    for name, pf in portfolios.items():
        m = pf["metrics"]
        cap = pf["config"].get("capital", 0)
        values = pf["portfolio_values"]
        pnl = values[-1] - values[0] if len(values) > 1 else 0.0
        pnl_color = GREEN if pnl >= 0 else RED
        rows += (
            f"<tr>"
            f'<td style="padding:8px 12px;">{name}</td>'
            f'<td style="padding:8px 12px;text-align:right;">${cap:,.0f}</td>'
            f'<td style="padding:8px 12px;text-align:right;">{m.get("total_return", 0):.2%}</td>'
            f'<td style="padding:8px 12px;text-align:right;">{m.get("sharpe_ratio", 0):.2f}</td>'
            f'<td style="padding:8px 12px;text-align:right;">{m.get("max_drawdown", 0):.2%}</td>'
            f'<td style="padding:8px 12px;text-align:right;">{m.get("win_rate", 0):.2%}</td>'
            f'<td style="padding:8px 12px;text-align:right;">{m.get("total_trades", 0)}</td>'
            f'<td style="padding:8px 12px;text-align:right;color:{pnl_color};">${pnl:+,.2f}</td>'
            f"</tr>\n"
        )

    table = (
        f'<table style="width:100%;border-collapse:collapse;margin:20px 0;background:{DARK_BG};border-radius:8px;">'
        f'<thead><tr style="border-bottom:1px solid #444;">'
        f'<th style="padding:10px 12px;text-align:left;">Strategy</th>'
        f'<th style="padding:10px 12px;text-align:right;">Capital</th>'
        f'<th style="padding:10px 12px;text-align:right;">Return</th>'
        f'<th style="padding:10px 12px;text-align:right;">Sharpe</th>'
        f'<th style="padding:10px 12px;text-align:right;">Max DD</th>'
        f'<th style="padding:10px 12px;text-align:right;">Win Rate</th>'
        f'<th style="padding:10px 12px;text-align:right;">Trades</th>'
        f'<th style="padding:10px 12px;text-align:right;">P&L</th>'
        f"</tr></thead>\n"
        f"<tbody>{rows}</tbody></table>\n"
    )

    return (
        '<div style="display:flex;flex-wrap:wrap;gap:12px;margin:20px 0;">\n'
        f"{html_cards}</div>\n"
        f"<h3>Per-Strategy Metrics</h3>\n{table}"
    )
```

### Step 4: Add multi-portfolio equity curve with overlaid strategy lines

```python
STRATEGY_COLORS = [
    "#4ecdc4", "#ff6b6b", "#ffd93d", "#6bcb77", "#4d96ff",
    "#ff922b", "#845ef7", "#f06595",
]


def _multi_equity_curve(data: dict[str, Any], include_plotlyjs: str | bool) -> str:
    """Plotly line chart with per-strategy + aggregate equity curves."""
    aggregate = data["aggregate"]
    portfolios = data["portfolios"]
    dates = aggregate["dates"]
    agg_values = aggregate["portfolio_values"][1:]  # skip initial capital entry
    initial = aggregate["portfolio_values"][0]

    fig = go.Figure()

    # Per-strategy lines (thinner, colored)
    for i, (name, pf) in enumerate(portfolios.items()):
        pf_values = pf["portfolio_values"][1:]
        color = STRATEGY_COLORS[i % len(STRATEGY_COLORS)]
        fig.add_trace(go.Scatter(
            x=dates,
            y=pf_values,
            mode="lines",
            name=name,
            line=dict(color=color, width=1.5),
            opacity=0.7,
        ))

    # Aggregate line (bold, white)
    fig.add_trace(go.Scatter(
        x=dates,
        y=agg_values,
        mode="lines",
        name="Aggregate",
        line=dict(color="#ffffff", width=3),
    ))

    fig.add_hline(
        y=initial,
        line_dash="dash",
        line_color="#888",
        annotation_text=f"Initial: ${initial:,.0f}",
        annotation_font_color="#aaa",
    )
    fig.update_layout(
        title="Equity Curves — Per-Strategy + Aggregate",
        xaxis_title="Date",
        yaxis_title="NAV ($)",
        template="plotly_dark",
        paper_bgcolor=BODY_BG,
        plot_bgcolor=DARK_BG,
        height=500,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    return fig.to_html(full_html=False, include_plotlyjs=include_plotlyjs)
```

### Step 5: Add strategy comparison bar chart

```python
def _strategy_comparison_chart(data: dict[str, Any]) -> str:
    """Grouped bar chart comparing Return, Sharpe, and Max DD across strategies."""
    portfolios = data["portfolios"]
    names = list(portfolios.keys())

    returns = [portfolios[n]["metrics"].get("total_return", 0) * 100 for n in names]
    sharpes = [portfolios[n]["metrics"].get("sharpe_ratio", 0) for n in names]
    max_dds = [portfolios[n]["metrics"].get("max_drawdown", 0) * 100 for n in names]

    from plotly.subplots import make_subplots

    fig = make_subplots(rows=1, cols=3, subplot_titles=("Total Return (%)", "Sharpe Ratio", "Max Drawdown (%)"))

    colors = [STRATEGY_COLORS[i % len(STRATEGY_COLORS)] for i in range(len(names))]

    fig.add_trace(go.Bar(x=names, y=returns, marker_color=colors, showlegend=False), row=1, col=1)
    fig.add_trace(go.Bar(x=names, y=sharpes, marker_color=colors, showlegend=False), row=1, col=2)
    fig.add_trace(go.Bar(x=names, y=max_dds, marker_color=colors, showlegend=False), row=1, col=3)

    fig.update_layout(
        title="Strategy Comparison",
        template="plotly_dark",
        paper_bgcolor=BODY_BG,
        plot_bgcolor=DARK_BG,
        height=400,
    )
    fig.update_xaxes(tickangle=45)
    return fig.to_html(full_html=False, include_plotlyjs=False)
```

### Step 6: Update `generate_report()` for multi-portfolio

Modify `generate_report()` to branch on format:

```python
def generate_report(json_path: str, output_path: str = "") -> str:
    with open(json_path) as f:
        data = json.load(f)

    if not output_path:
        output_path = str(Path(json_path).with_suffix(".html"))

    if _is_multi_portfolio(data):
        return _generate_multi_report(data, output_path)
    return _generate_single_report(data, output_path)
```

Extract current `generate_report()` body into `_generate_single_report(data, output_path)`.

Create `_generate_multi_report(data, output_path)`:

```python
def _generate_multi_report(data: dict[str, Any], output_path: str) -> str:
    """Generate HTML report for multi-portfolio backtest data."""
    aggregate = data["aggregate"]
    trades = aggregate.get("trades", [])
    bars = data.get("bars", {})

    trades_sorted = sorted(trades, key=lambda t: t.get("entry_date", ""))
    tickers = sorted({t["ticker"] for t in trades_sorted})
    portfolios_list = sorted(data["portfolios"].keys())

    parts: list[str] = []

    # HTML head (same styling as single)
    parts.append(f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Multi-Portfolio Backtest Report</title>
<style>
body {{ background: {BODY_BG}; color: {TEXT_COLOR}; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 0; padding: 20px; }}
h1, h2, h3 {{ color: {TEXT_COLOR}; }}
.section {{ margin: 30px 0; }}
.controls {{ display: flex; gap: 16px; align-items: center; margin: 20px 0; }}
.controls select {{ background: {DARK_BG}; color: {TEXT_COLOR}; border: 1px solid #444; border-radius: 4px; padding: 6px 12px; font-size: 14px; }}
.trade-card {{ background: {DARK_BG}; border-radius: 8px; padding: 16px; margin: 16px 0; }}
</style>
</head>
<body>
<h1>Multi-Portfolio Backtest Report</h1>
""")

    # Summary panel with per-strategy table
    parts.append('<div class="section">')
    parts.append("<h2>Aggregate Summary</h2>")
    parts.append(_multi_summary_panel(data))
    parts.append("</div>")

    # Equity curves (first chart — include Plotly JS via CDN)
    parts.append('<div class="section">')
    parts.append(_multi_equity_curve(data, include_plotlyjs="cdn"))
    parts.append("</div>")

    # Aggregate drawdown (reuse existing function with aggregate data)
    agg_data = {
        "dates": aggregate["dates"],
        "portfolio_values": aggregate["portfolio_values"],
    }
    parts.append('<div class="section">')
    parts.append(_drawdown_chart(agg_data))
    parts.append("</div>")

    # Strategy comparison
    parts.append('<div class="section">')
    parts.append(_strategy_comparison_chart(data))
    parts.append("</div>")

    # Monthly returns heatmap (from aggregate)
    parts.append('<div class="section">')
    parts.append(_monthly_returns_heatmap(agg_data))
    parts.append("</div>")

    # Trade PnL histogram (from aggregate trades)
    agg_trade_data = {"trades": trades}
    parts.append('<div class="section">')
    parts.append(_trade_pnl_histogram(agg_trade_data))
    parts.append("</div>")

    # Individual trade charts with portfolio + ticker filters
    parts.append('<div class="section">')
    parts.append("<h2>Individual Trades</h2>")

    ticker_options = '<option value="all">All Tickers</option>\n'
    for t in tickers:
        ticker_options += f'<option value="{t}">{t}</option>\n'

    portfolio_options = '<option value="all">All Portfolios</option>\n'
    for p in portfolios_list:
        portfolio_options += f'<option value="{p}">{p}</option>\n'

    parts.append(f"""
<div class="controls">
    <label>Portfolio:
        <select id="portfolio-filter" onchange="filterTrades()">
            {portfolio_options}
        </select>
    </label>
    <label>Ticker:
        <select id="ticker-filter" onchange="filterTrades()">
            {ticker_options}
        </select>
    </label>
    <label>Sort:
        <select id="sort-select" onchange="filterTrades()">
            <option value="date">By Date</option>
            <option value="pnl-best">By PnL (Best)</option>
            <option value="pnl-worst">By PnL (Worst)</option>
            <option value="ticker">By Ticker</option>
        </select>
    </label>
</div>
""")

    parts.append('<div id="trade-container">')
    for trade in trades_sorted:
        ticker = trade["ticker"]
        pnl = trade["pnl"]
        entry_date = trade.get("entry_date", "")
        portfolio = trade.get("portfolio", "unknown")
        ticker_bars = bars.get(ticker, [])

        chart_html = _trade_chart(trade, ticker_bars)

        parts.append(
            f'<div class="trade-card" data-ticker="{ticker}" '
            f'data-pnl="{pnl}" data-date="{entry_date}" data-portfolio="{portfolio}">'
        )
        parts.append(chart_html)
        parts.append("</div>")
    parts.append("</div>")  # trade-container
    parts.append("</div>")  # section

    # Navigation JS (updated for portfolio filter)
    parts.append(_MULTI_NAV_JS)

    parts.append("</body>\n</html>")

    html = "\n".join(parts)
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    Path(output_path).write_text(html)
    print(f"Report saved to {output_path}")
    return output_path
```

### Step 7: Add updated navigation JS for portfolio filter

```python
_MULTI_NAV_JS = """
<script>
function filterTrades() {
    var portfolio = document.getElementById('portfolio-filter').value;
    var ticker = document.getElementById('ticker-filter').value;
    var sort = document.getElementById('sort-select').value;
    var cards = Array.from(document.querySelectorAll('.trade-card'));

    cards.forEach(function(card) {
        var matchPortfolio = portfolio === 'all' || card.getAttribute('data-portfolio') === portfolio;
        var matchTicker = ticker === 'all' || card.getAttribute('data-ticker') === ticker;
        card.style.display = (matchPortfolio && matchTicker) ? '' : 'none';
    });

    var container = document.getElementById('trade-container');
    var visible = cards.filter(function(c) { return c.style.display !== 'none'; });

    visible.sort(function(a, b) {
        if (sort === 'date') {
            return a.getAttribute('data-date').localeCompare(b.getAttribute('data-date'));
        } else if (sort === 'pnl-best') {
            return parseFloat(b.getAttribute('data-pnl')) - parseFloat(a.getAttribute('data-pnl'));
        } else if (sort === 'pnl-worst') {
            return parseFloat(a.getAttribute('data-pnl')) - parseFloat(b.getAttribute('data-pnl'));
        } else if (sort === 'ticker') {
            return a.getAttribute('data-ticker').localeCompare(b.getAttribute('data-ticker'));
        }
        return 0;
    });

    visible.forEach(function(card) {
        container.appendChild(card);
    });
}
</script>
"""
```

### Step 8: Run tests

Run: `pytest tests/backtest/test_visualize_backtest.py -v`
Expected: All 6 tests pass (2 existing + 4 new)

Run: `pytest tests/backtest/ -v --tb=short`
Expected: All tests pass

### Step 9: Commit

```bash
git add scripts/visualize_backtest.py tests/backtest/test_visualize_backtest.py
git commit -m "feat: add multi-portfolio visualization with per-strategy equity curves and comparison charts"
```

---

## Task 2: Update Documentation

**Files:**
- Modify: `docs/strategy.md`

### Step 1: Update infrastructure table

Add `scripts/visualize_backtest.py` update note to the infrastructure table if not already there. Add note about multi-portfolio report support.

### Step 2: Commit

```bash
git add docs/strategy.md
git commit -m "docs: note multi-portfolio visualization support in strategy.md"
```

---

## Verification

```bash
# All visualization tests
pytest tests/backtest/test_visualize_backtest.py -v

# All backtest tests
pytest tests/backtest/ -v --tb=short

# Full test suite
pytest tests/ -v --tb=short
```
