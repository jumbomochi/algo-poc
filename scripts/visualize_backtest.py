#!/usr/bin/env python3
"""Generate an interactive HTML backtest report from a JSON results file.

Usage:
    python scripts/visualize_backtest.py output/backtest_20260220_120000.json
    python scripts/visualize_backtest.py output/backtest.json -o report.html
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from services.signal_generation.technical import find_support_levels

# ---------------------------------------------------------------------------
# Colour palette
# ---------------------------------------------------------------------------
GREEN = "#4ecdc4"
RED = "#ff6b6b"
DARK_BG = "#1e1e2e"
BODY_BG = "#121212"
TEXT_COLOR = "#fff"

STRATEGY_COLORS = [
    "#4ecdc4", "#ff6b6b", "#ffd93d", "#6bcb77",
    "#4d96ff", "#ff922b", "#845ef7", "#f06595",
]

# ---------------------------------------------------------------------------
# Summary stats panel
# ---------------------------------------------------------------------------


def _summary_panel(data: dict[str, Any]) -> str:
    """Return an HTML block with summary statistic cards."""
    metrics = data["metrics"]
    config = data["config"]
    num_tickers = len(config.get("tickers", []))

    cards = [
        ("Total Return", f"{metrics.get('total_return', 0):.2%}"),
        ("Sharpe Ratio", f"{metrics.get('sharpe_ratio', 0):.2f}"),
        ("Max Drawdown", f"{metrics.get('max_drawdown', 0):.2%}"),
        ("Win Rate", f"{metrics.get('win_rate', 0):.2%}"),
        ("Total Trades", f"{metrics.get('total_trades', 0)}"),
        ("Avg Holding Period", f"{metrics.get('avg_holding_period_days', 0):.1f} days"),
        ("Initial Capital", f"${config.get('initial_capital', 0):,.0f}"),
        ("Ticker Count", f"{num_tickers}"),
        ("Slippage", f"{config.get('slippage_bps', 0)} bps"),
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

    return (
        '<div style="display:flex;flex-wrap:wrap;gap:12px;margin:20px 0;">\n'
        f"{html_cards}"
        "</div>\n"
    )


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------


def _is_multi_portfolio(data: dict[str, Any]) -> bool:
    """Return True if data uses the multi-portfolio JSON format."""
    return "portfolios" in data and isinstance(data["portfolios"], dict)


# ---------------------------------------------------------------------------
# Multi-portfolio summary panel
# ---------------------------------------------------------------------------


def _multi_summary_panel(data: dict[str, Any]) -> str:
    """Return an HTML block with aggregate summary cards and per-strategy table."""
    agg_metrics = data["aggregate"]["metrics"]
    config = data["config"]
    portfolios = data["portfolios"]

    # Aggregate metric cards
    # Total capital = sum of per-portfolio allocations (config['portfolios'] is a
    # dict of name -> capital). Fall back to top-level initial_capital for older
    # JSONs that didn't write the portfolios map.
    total_capital = (
        sum(config.get("portfolios", {}).values())
        or config.get("initial_capital", 0)
    )
    cards = [
        ("Total Return", f"{agg_metrics.get('total_return', 0):.2%}"),
        ("Sharpe Ratio", f"{agg_metrics.get('sharpe_ratio', 0):.2f}"),
        ("Max Drawdown", f"{agg_metrics.get('max_drawdown', 0):.2%}"),
        ("Win Rate", f"{agg_metrics.get('win_rate', 0):.2%}"),
        ("Total Trades", f"{agg_metrics.get('total_trades', 0)}"),
        ("Strategies", f"{len(portfolios)}"),
        ("Total Capital", f"${total_capital:,.0f}"),
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

    # Per-strategy metrics table
    table_rows = ""
    for name, pdata in portfolios.items():
        m = pdata["metrics"]
        cap = pdata["config"].get("capital", 0)
        # Use mark-to-market P&L (final portfolio value minus starting capital) so the
        # column matches the Total Return % shown alongside it. Summing trades["pnl"]
        # would only count *closed* trades and exclude unrealized P&L in still-open
        # positions at end of backtest, producing rows that look internally
        # inconsistent (e.g. 790% return but a P&L that doesn't equal 790% × cap).
        pv = pdata.get("portfolio_values") or []
        strat_pnl = (pv[-1] - cap) if pv else 0.0
        table_rows += (
            f"<tr>"
            f"<td>{name}</td>"
            f"<td>${cap:,.0f}</td>"
            f"<td>{m.get('total_return', 0):.2%}</td>"
            f"<td>{m.get('sharpe_ratio', 0):.2f}</td>"
            f"<td>{m.get('max_drawdown', 0):.2%}</td>"
            f"<td>{m.get('win_rate', 0):.2%}</td>"
            f"<td>{m.get('total_trades', 0)}</td>"
            f"<td>${strat_pnl:+,.2f}</td>"
            f"</tr>\n"
        )

    table_html = (
        '<table style="width:100%;border-collapse:collapse;margin-top:16px;">'
        "<thead><tr>"
        '<th style="text-align:left;padding:8px;border-bottom:1px solid #444;">Strategy</th>'
        '<th style="text-align:left;padding:8px;border-bottom:1px solid #444;">Capital</th>'
        '<th style="text-align:left;padding:8px;border-bottom:1px solid #444;">Return</th>'
        '<th style="text-align:left;padding:8px;border-bottom:1px solid #444;">Sharpe</th>'
        '<th style="text-align:left;padding:8px;border-bottom:1px solid #444;">Max DD</th>'
        '<th style="text-align:left;padding:8px;border-bottom:1px solid #444;">Win Rate</th>'
        '<th style="text-align:left;padding:8px;border-bottom:1px solid #444;">Trades</th>'
        '<th style="text-align:left;padding:8px;border-bottom:1px solid #444;">P&amp;L</th>'
        "</tr></thead>"
        f"<tbody>{table_rows}</tbody>"
        "</table>"
    )

    return (
        '<div style="display:flex;flex-wrap:wrap;gap:12px;margin:20px 0;">\n'
        f"{html_cards}"
        "</div>\n"
        f"{table_html}\n"
    )


# ---------------------------------------------------------------------------
# Portfolio-level charts
# ---------------------------------------------------------------------------


def _equity_curve(data: dict[str, Any], include_plotlyjs: str | bool) -> str:
    """Plotly line chart of daily NAV over time."""
    dates = data["dates"]
    # portfolio_values has one more entry than dates — first is initial capital
    values = data["portfolio_values"][1:]
    initial = data["portfolio_values"][0]

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=dates,
        y=values,
        mode="lines",
        name="Portfolio NAV",
        line=dict(color=GREEN, width=2),
    ))
    fig.add_hline(
        y=initial,
        line_dash="dash",
        line_color="#888",
        annotation_text=f"Initial: ${initial:,.0f}",
        annotation_font_color="#aaa",
    )
    fig.update_layout(
        title="Equity Curve",
        xaxis_title="Date",
        yaxis_title="NAV ($)",
        template="plotly_dark",
        paper_bgcolor=BODY_BG,
        plot_bgcolor=DARK_BG,
        height=400,
    )
    return fig.to_html(full_html=False, include_plotlyjs=include_plotlyjs)


def _drawdown_chart(data: dict[str, Any]) -> str:
    """Area chart of drawdown from peak as negative percentage."""
    dates = data["dates"]
    values = np.array(data["portfolio_values"][1:], dtype=float)

    running_max = np.maximum.accumulate(values)
    drawdown = (values - running_max) / running_max

    # Find max drawdown point
    max_dd_idx = int(np.argmin(drawdown))
    max_dd_val = float(drawdown[max_dd_idx])
    max_dd_date = dates[max_dd_idx]

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=dates,
        y=(drawdown * 100).tolist(),
        fill="tozeroy",
        mode="lines",
        name="Drawdown",
        line=dict(color=RED, width=1),
        fillcolor="rgba(255,107,107,0.3)",
    ))
    fig.add_annotation(
        x=max_dd_date,
        y=max_dd_val * 100,
        text=f"Max DD: {max_dd_val:.2%}",
        showarrow=True,
        arrowhead=2,
        arrowcolor=RED,
        font=dict(color=RED, size=12),
    )
    fig.update_layout(
        title="Drawdown",
        xaxis_title="Date",
        yaxis_title="Drawdown (%)",
        template="plotly_dark",
        paper_bgcolor=BODY_BG,
        plot_bgcolor=DARK_BG,
        height=300,
    )
    return fig.to_html(full_html=False, include_plotlyjs=False)


def _monthly_returns_heatmap(data: dict[str, Any]) -> str:
    """Heatmap of monthly returns: rows=years, columns=months."""
    from datetime import datetime

    dates = data["dates"]
    values = data["portfolio_values"][1:]

    if len(dates) < 2:
        return "<p>Not enough data for monthly heatmap.</p>"

    # Build a mapping of (year, month) -> list of daily values
    monthly: dict[tuple[int, int], list[float]] = {}
    for i, d in enumerate(dates):
        dt = datetime.fromisoformat(d) if isinstance(d, str) else d
        key = (dt.year, dt.month)
        monthly.setdefault(key, []).append(values[i])

    # Compute monthly returns
    years_set: set[int] = set()
    returns: dict[tuple[int, int], float] = {}

    sorted_keys = sorted(monthly.keys())
    for i, key in enumerate(sorted_keys):
        yr, mo = key
        years_set.add(yr)
        # Monthly return: (last_value - first_value) / first_value
        vals = monthly[key]
        if len(vals) >= 2:
            ret = (vals[-1] - vals[0]) / vals[0] if vals[0] != 0 else 0
        else:
            ret = 0.0
        returns[key] = ret

    years = sorted(years_set)
    months = list(range(1, 13))
    month_labels = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

    z = []
    text = []
    for yr in years:
        row = []
        text_row = []
        for mo in months:
            val = returns.get((yr, mo))
            if val is not None:
                row.append(val * 100)
                text_row.append(f"{val:.1%}")
            else:
                row.append(None)
                text_row.append("")
        z.append(row)
        text.append(text_row)

    fig = go.Figure(data=go.Heatmap(
        z=z,
        x=month_labels,
        y=[str(y) for y in years],
        text=text,
        texttemplate="%{text}",
        colorscale=[[0, RED], [0.5, "#333"], [1, GREEN]],
        zmid=0,
        colorbar=dict(title="%"),
    ))
    fig.update_layout(
        title="Monthly Returns",
        template="plotly_dark",
        paper_bgcolor=BODY_BG,
        plot_bgcolor=DARK_BG,
        height=max(200, 80 * len(years) + 100),
    )
    return fig.to_html(full_html=False, include_plotlyjs=False)


def _trade_pnl_histogram(data: dict[str, Any]) -> str:
    """Histogram of individual trade PnL values."""
    pnls = [t["pnl"] for t in data.get("trades", [])]
    if not pnls:
        return "<p>No trades to plot.</p>"

    fig = go.Figure()
    fig.add_trace(go.Histogram(
        x=pnls,
        marker_color=GREEN,
        opacity=0.75,
        name="Trade PnL",
    ))
    fig.add_vline(
        x=0,
        line_dash="dash",
        line_color="#fff",
        line_width=2,
    )
    fig.update_layout(
        title="Trade PnL Distribution",
        xaxis_title="PnL ($)",
        yaxis_title="Count",
        template="plotly_dark",
        paper_bgcolor=BODY_BG,
        plot_bgcolor=DARK_BG,
        height=350,
    )
    return fig.to_html(full_html=False, include_plotlyjs=False)


# ---------------------------------------------------------------------------
# Multi-portfolio charts
# ---------------------------------------------------------------------------


def _multi_equity_curve(
    data: dict[str, Any], include_plotlyjs: str | bool
) -> str:
    """Plotly multi-line equity chart: one thin line per strategy + bold aggregate."""
    portfolios = data["portfolios"]
    agg = data["aggregate"]
    agg_dates = agg["dates"]
    agg_values = agg["portfolio_values"][1:]
    initial = agg["portfolio_values"][0]

    fig = go.Figure()

    # Per-strategy lines
    for idx, (name, pdata) in enumerate(portfolios.items()):
        color = STRATEGY_COLORS[idx % len(STRATEGY_COLORS)]
        vals = pdata["portfolio_values"][1:]
        dates = pdata["dates"]
        fig.add_trace(go.Scatter(
            x=dates,
            y=vals,
            mode="lines",
            name=name,
            line=dict(color=color, width=1.5),
            opacity=0.7,
        ))

    # Aggregate line (bold white on top)
    fig.add_trace(go.Scatter(
        x=agg_dates,
        y=agg_values,
        mode="lines",
        name="Aggregate",
        line=dict(color="#ffffff", width=3),
    ))

    # Initial capital reference
    fig.add_hline(
        y=initial,
        line_dash="dash",
        line_color="#888",
        annotation_text=f"Initial: ${initial:,.0f}",
        annotation_font_color="#aaa",
    )

    fig.update_layout(
        title="Equity Curve",
        xaxis_title="Date",
        yaxis_title="NAV ($)",
        template="plotly_dark",
        paper_bgcolor=BODY_BG,
        plot_bgcolor=DARK_BG,
        height=450,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
    )
    return fig.to_html(full_html=False, include_plotlyjs=include_plotlyjs)


def _strategy_comparison_chart(data: dict[str, Any]) -> str:
    """3-panel bar chart comparing Total Return, Sharpe, Max DD across strategies."""
    portfolios = data["portfolios"]
    names = list(portfolios.keys())
    colors = [STRATEGY_COLORS[i % len(STRATEGY_COLORS)] for i in range(len(names))]

    returns = [portfolios[n]["metrics"].get("total_return", 0) * 100 for n in names]
    sharpes = [portfolios[n]["metrics"].get("sharpe_ratio", 0) for n in names]
    max_dds = [portfolios[n]["metrics"].get("max_drawdown", 0) * 100 for n in names]

    fig = make_subplots(
        rows=1, cols=3,
        subplot_titles=["Total Return (%)", "Sharpe Ratio", "Max Drawdown (%)"],
    )

    fig.add_trace(
        go.Bar(x=names, y=returns, marker_color=colors, showlegend=False),
        row=1, col=1,
    )
    fig.add_trace(
        go.Bar(x=names, y=sharpes, marker_color=colors, showlegend=False),
        row=1, col=2,
    )
    fig.add_trace(
        go.Bar(x=names, y=max_dds, marker_color=colors, showlegend=False),
        row=1, col=3,
    )

    fig.update_layout(
        title="Strategy Comparison",
        template="plotly_dark",
        paper_bgcolor=BODY_BG,
        plot_bgcolor=DARK_BG,
        height=350,
    )
    return fig.to_html(full_html=False, include_plotlyjs=False)


# ---------------------------------------------------------------------------
# Trade-level charts
# ---------------------------------------------------------------------------


def _fig_to_compact_json(fig: go.Figure) -> str:
    """Serialize a Plotly figure to JSON, stripping the bulky template."""
    fig_dict = json.loads(fig.to_json())
    fig_dict.get("layout", {}).pop("template", None)
    return json.dumps(fig_dict, separators=(",", ":"))


def _trade_figure(trade: dict[str, Any], bars: list[dict[str, Any]]) -> go.Figure | None:
    """Build a Plotly figure for a single trade (candlestick + entry/exit + support)."""
    ticker = trade["ticker"]
    entry_date = trade["entry_date"]
    exit_date = trade["exit_date"]
    entry_price = trade["entry_price"]
    exit_price = trade["exit_price"]
    pnl = trade["pnl"]
    exit_reason = trade.get("exit_reason", "unknown")
    entry_signals = trade.get("entry_signals", {})

    # Find bar indices for entry and exit dates
    bar_dates = [b["date"] for b in bars]

    def _find_idx(target_date: str) -> int:
        try:
            return bar_dates.index(target_date)
        except ValueError:
            closest = min(range(len(bar_dates)), key=lambda i: abs(
                (int(bar_dates[i].replace("-", "")) - int(target_date.replace("-", "")))
            ))
            return closest

    entry_idx = _find_idx(entry_date)
    exit_idx = _find_idx(exit_date)

    # Window: 30 bars before entry through exit + 10 bars after
    start_idx = max(0, entry_idx - 30)
    end_idx = min(len(bars) - 1, exit_idx + 10)
    window_bars = bars[start_idx:end_idx + 1]

    if not window_bars:
        return None

    w_dates = [b["date"] for b in window_bars]
    w_open = [b["open"] for b in window_bars]
    w_high = [b["high"] for b in window_bars]
    w_low = [b["low"] for b in window_bars]
    w_close = [b["close"] for b in window_bars]

    fig = go.Figure()
    fig.add_trace(go.Candlestick(
        x=w_dates,
        open=w_open,
        high=w_high,
        low=w_low,
        close=w_close,
        name=ticker,
        increasing_line_color=GREEN,
        decreasing_line_color=RED,
    ))

    # Entry marker
    fig.add_trace(go.Scatter(
        x=[entry_date],
        y=[entry_price],
        mode="markers+text",
        marker=dict(symbol="triangle-up", size=14, color=GREEN),
        text=[f"Entry ${entry_price:.2f}"],
        textposition="top center",
        textfont=dict(color=GREEN, size=10),
        name="Entry",
        showlegend=False,
    ))

    # Exit marker
    fig.add_trace(go.Scatter(
        x=[exit_date],
        y=[exit_price],
        mode="markers+text",
        marker=dict(symbol="triangle-down", size=14, color=RED),
        text=[f"Exit ${exit_price:.2f}"],
        textposition="bottom center",
        textfont=dict(color=RED, size=10),
        name="Exit",
        showlegend=False,
    ))

    # Support levels — compute from bars available at entry time
    bars_at_entry = bars[:entry_idx + 1]
    if len(bars_at_entry) >= 3:
        support_data = {
            "low": [b["low"] for b in bars_at_entry],
            "high": [b["high"] for b in bars_at_entry],
            "close": [b["close"] for b in bars_at_entry],
            "open": [b["open"] for b in bars_at_entry],
            "volume": [b.get("volume", 0) for b in bars_at_entry],
        }
        try:
            all_levels = find_support_levels(support_data)
            price_min = min(w_low)
            price_max = max(w_high)
            visible_levels = [lvl for lvl in all_levels if price_min <= lvl <= price_max]
            top_levels = visible_levels[:5]

            for lvl in top_levels:
                fig.add_hline(
                    y=lvl,
                    line_dash="dash",
                    line_color="#888",
                    line_width=1,
                    annotation_text=f"S: ${lvl:.2f}",
                    annotation_font_color="#888",
                    annotation_font_size=9,
                )
        except Exception:
            pass

    # Holding period
    try:
        from datetime import datetime
        d1 = datetime.fromisoformat(entry_date)
        d2 = datetime.fromisoformat(exit_date)
        holding_days = (d2 - d1).days
    except Exception:
        holding_days = "?"

    pnl_color = GREEN if pnl >= 0 else RED

    # Subtitle: signal values at entry (show confidence where that's the threshold)
    prox = entry_signals.get("proximity", {})
    strength = entry_signals.get("strength", {})
    trend = entry_signals.get("trend", {})
    subtitle_parts = []
    if prox:
        subtitle_parts.append(f"Proximity: {prox.get('value', 0):.2f}")
    if strength:
        subtitle_parts.append(
            f"Strength: {strength.get('confidence', 0):.2f}"
        )
    if trend:
        subtitle_parts.append(
            f"Trend: {trend.get('value', 0):.2f} (conf={trend.get('confidence', 0):.2f})"
        )
    subtitle = " | ".join(subtitle_parts) if subtitle_parts else ""

    fig.update_layout(
        title=dict(
            text=(
                f"{ticker} | PnL: <span style='color:{pnl_color}'>${pnl:+,.2f}</span>"
                f" | {exit_reason} | {holding_days}d"
                f"<br><span style='font-size:11px;color:#aaa'>{subtitle}</span>"
            ),
        ),
        template="plotly_dark",
        paper_bgcolor=BODY_BG,
        plot_bgcolor=DARK_BG,
        xaxis_rangeslider_visible=False,
        height=450,
    )
    return fig


def _trade_chart(trade: dict[str, Any], bars: list[dict[str, Any]]) -> str:
    """Candlestick chart HTML for a single trade."""
    fig = _trade_figure(trade, bars)
    if fig is None:
        return ""
    return fig.to_html(full_html=False, include_plotlyjs=False)


# ---------------------------------------------------------------------------
# Navigation JavaScript
# ---------------------------------------------------------------------------

_NAV_JS = """
<script>
var PAGE_SIZE = 20;
var currentPage = 0;
var filteredIndices = [];
var renderedPlots = {};

function getFilteredAndSorted() {
    var portfolioEl = document.getElementById('portfolio-filter');
    var portfolio = portfolioEl ? portfolioEl.value : 'all';
    var tickerEl = document.getElementById('ticker-filter');
    var ticker = tickerEl ? tickerEl.value : 'all';
    var sortEl = document.getElementById('sort-select');
    var sort = sortEl ? sortEl.value : 'date';

    // Filter trade metadata
    var indices = [];
    for (var i = 0; i < TRADE_META.length; i++) {
        var m = TRADE_META[i];
        var matchPortfolio = portfolio === 'all' || m.portfolio === portfolio;
        var matchTicker = ticker === 'all' || m.ticker === ticker;
        if (matchPortfolio && matchTicker) indices.push(i);
    }

    // Sort
    indices.sort(function(a, b) {
        var ma = TRADE_META[a], mb = TRADE_META[b];
        if (sort === 'date') return ma.date.localeCompare(mb.date);
        if (sort === 'pnl-best') return mb.pnl - ma.pnl;
        if (sort === 'pnl-worst') return ma.pnl - mb.pnl;
        if (sort === 'ticker') return ma.ticker.localeCompare(mb.ticker);
        return 0;
    });
    return indices;
}

function purgeRendered() {
    for (var id in renderedPlots) {
        var el = document.getElementById(id);
        if (el && typeof Plotly !== 'undefined') Plotly.purge(el);
    }
    renderedPlots = {};
}

function renderPage() {
    purgeRendered();
    var container = document.getElementById('trade-container');
    container.innerHTML = '';

    var start = currentPage * PAGE_SIZE;
    var end = Math.min(start + PAGE_SIZE, filteredIndices.length);

    for (var i = start; i < end; i++) {
        var idx = filteredIndices[i];
        var m = TRADE_META[idx];
        var plotId = 'plot-' + idx;
        var card = document.createElement('div');
        card.className = 'trade-card';
        card.innerHTML = '<div id="' + plotId + '" style="height:450px;width:100%;"></div>';
        container.appendChild(card);

        var fig = TRADE_FIGURES[idx];
        if (fig && typeof Plotly !== 'undefined') {
            var layout = Object.assign({template: 'plotly_dark'}, fig.layout);
            Plotly.newPlot(plotId, fig.data, layout, {responsive: true});
            renderedPlots[plotId] = true;
        }
    }

    updatePaginationInfo();
}

function updatePaginationInfo() {
    var totalPages = Math.max(1, Math.ceil(filteredIndices.length / PAGE_SIZE));
    var info = document.getElementById('page-info');
    if (info) {
        info.textContent = 'Page ' + (currentPage + 1) + ' of ' + totalPages +
            ' (' + filteredIndices.length + ' trades)';
    }
    var prevBtn = document.getElementById('prev-btn');
    var nextBtn = document.getElementById('next-btn');
    if (prevBtn) prevBtn.disabled = currentPage === 0;
    if (nextBtn) nextBtn.disabled = currentPage >= totalPages - 1;
}

function filterTrades() {
    currentPage = 0;
    filteredIndices = getFilteredAndSorted();
    renderPage();
}

function prevPage() {
    if (currentPage > 0) {
        currentPage--;
        renderPage();
        document.getElementById('trade-container').scrollIntoView({behavior: 'smooth'});
    }
}

function nextPage() {
    var totalPages = Math.ceil(filteredIndices.length / PAGE_SIZE);
    if (currentPage < totalPages - 1) {
        currentPage++;
        renderPage();
        document.getElementById('trade-container').scrollIntoView({behavior: 'smooth'});
    }
}

document.addEventListener('DOMContentLoaded', function() {
    filteredIndices = getFilteredAndSorted();
    renderPage();
});
</script>
"""


# ---------------------------------------------------------------------------
# Multi-portfolio report generator
# ---------------------------------------------------------------------------


def _generate_multi_report(data: dict[str, Any], output_path: str) -> str:
    """Generate an HTML report for multi-portfolio backtest data."""
    agg = data["aggregate"]
    portfolios = data["portfolios"]
    bars = data.get("bars", {})

    all_trades = agg.get("trades", [])
    trades_sorted = sorted(all_trades, key=lambda t: t.get("entry_date", ""))

    # Collect unique tickers and portfolio names
    tickers = sorted({t["ticker"] for t in trades_sorted})
    portfolio_names = sorted(portfolios.keys())

    parts: list[str] = []

    # HTML head
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
table {{ color: {TEXT_COLOR}; }}
th, td {{ padding: 8px; text-align: left; }}
</style>
</head>
<body>
<h1>Multi-Portfolio Backtest Report</h1>
""")

    # Aggregate summary panel + per-strategy table
    parts.append('<div class="section">')
    parts.append("<h2>Aggregate Summary</h2>")
    parts.append(_multi_summary_panel(data))
    parts.append("</div>")

    # Multi equity curves
    parts.append('<div class="section">')
    parts.append(_multi_equity_curve(data, include_plotlyjs="cdn"))
    parts.append("</div>")

    # Aggregate drawdown (reuse _drawdown_chart with aggregate data)
    parts.append('<div class="section">')
    parts.append(_drawdown_chart(agg))
    parts.append("</div>")

    # Strategy comparison chart
    parts.append('<div class="section">')
    parts.append(_strategy_comparison_chart(data))
    parts.append("</div>")

    # Monthly returns heatmap (from aggregate data)
    parts.append('<div class="section">')
    parts.append(_monthly_returns_heatmap(agg))
    parts.append("</div>")

    # Trade PnL histogram (from aggregate trades)
    parts.append('<div class="section">')
    parts.append(_trade_pnl_histogram(agg))
    parts.append("</div>")

    # --- Trade-level charts with portfolio filter ---
    parts.append('<div class="section">')
    parts.append("<h2>Individual Trades</h2>")

    # Navigation controls with portfolio filter
    portfolio_options = '<option value="all">All Portfolios</option>\n'
    for pname in portfolio_names:
        portfolio_options += f'<option value="{pname}">{pname}</option>\n'

    ticker_options = '<option value="all">All Tickers</option>\n'
    for t in tickers:
        ticker_options += f'<option value="{t}">{t}</option>\n'

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
    <div style="display:flex;gap:8px;align-items:center;margin-left:auto;">
        <button id="prev-btn" onclick="prevPage()"
            style="background:{DARK_BG};color:{TEXT_COLOR};border:1px solid #444;border-radius:4px;padding:6px 14px;cursor:pointer;">&laquo; Prev</button>
        <span id="page-info" style="font-size:13px;color:#aaa;min-width:160px;text-align:center;">Page 1</span>
        <button id="next-btn" onclick="nextPage()"
            style="background:{DARK_BG};color:{TEXT_COLOR};border:1px solid #444;border-radius:4px;padding:6px 14px;cursor:pointer;">Next &raquo;</button>
    </div>
</div>
""")

    # Build figure JSON array and trade metadata for JS-side rendering
    trade_meta: list[dict[str, Any]] = []
    trade_figures: list[str] = []  # JSON strings
    for trade in trades_sorted:
        ticker = trade["ticker"]
        pnl = trade["pnl"]
        entry_date = trade.get("entry_date", "")
        portfolio = trade.get("portfolio", "unknown")
        ticker_bars = bars.get(ticker, [])

        fig = _trade_figure(trade, ticker_bars)
        trade_meta.append({
            "ticker": ticker,
            "pnl": pnl,
            "date": entry_date,
            "portfolio": portfolio,
        })
        trade_figures.append(_fig_to_compact_json(fig) if fig else "null")

    parts.append("<script>")
    parts.append(f"var TRADE_META = {json.dumps(trade_meta, separators=(',', ':'))};")
    parts.append("var TRADE_FIGURES = [" + ",".join(trade_figures) + "];")
    parts.append("</script>")

    parts.append('<div id="trade-container"></div>')
    parts.append("</div>")  # section

    # Navigation JS (renders charts on demand from TRADE_FIGURES)
    parts.append(_NAV_JS)

    parts.append("</body>\n</html>")

    html = "\n".join(parts)
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    Path(output_path).write_text(html)
    print(f"Report saved to {output_path}")
    return output_path


# ---------------------------------------------------------------------------
# Single-portfolio report generator
# ---------------------------------------------------------------------------


def _generate_single_report(data: dict[str, Any], output_path: str) -> str:
    """Generate an HTML report for single-portfolio backtest data."""
    trades = data.get("trades", [])
    bars = data.get("bars", {})

    # Sort trades by entry_date
    trades_sorted = sorted(trades, key=lambda t: t.get("entry_date", ""))

    # Collect unique tickers
    tickers = sorted({t["ticker"] for t in trades_sorted})

    # --- Build HTML ---
    parts: list[str] = []

    # HTML head
    parts.append(f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Backtest Report</title>
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
<h1>Backtest Report</h1>
""")

    # Summary panel
    parts.append('<div class="section">')
    parts.append("<h2>Summary</h2>")
    parts.append(_summary_panel(data))
    parts.append("</div>")

    # Equity curve (first chart -- include Plotly JS via CDN)
    parts.append('<div class="section">')
    parts.append(_equity_curve(data, include_plotlyjs="cdn"))
    parts.append("</div>")

    # Drawdown
    parts.append('<div class="section">')
    parts.append(_drawdown_chart(data))
    parts.append("</div>")

    # Monthly returns heatmap
    parts.append('<div class="section">')
    parts.append(_monthly_returns_heatmap(data))
    parts.append("</div>")

    # Trade PnL distribution
    parts.append('<div class="section">')
    parts.append(_trade_pnl_histogram(data))
    parts.append("</div>")

    # --- Trade-level charts ---
    parts.append('<div class="section">')
    parts.append("<h2>Individual Trades</h2>")

    # Navigation controls
    ticker_options = '<option value="all">All Tickers</option>\n'
    for t in tickers:
        ticker_options += f'<option value="{t}">{t}</option>\n'

    parts.append(f"""
<div class="controls">
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
    <div style="display:flex;gap:8px;align-items:center;margin-left:auto;">
        <button id="prev-btn" onclick="prevPage()"
            style="background:{DARK_BG};color:{TEXT_COLOR};border:1px solid #444;border-radius:4px;padding:6px 14px;cursor:pointer;">&laquo; Prev</button>
        <span id="page-info" style="font-size:13px;color:#aaa;min-width:160px;text-align:center;">Page 1</span>
        <button id="next-btn" onclick="nextPage()"
            style="background:{DARK_BG};color:{TEXT_COLOR};border:1px solid #444;border-radius:4px;padding:6px 14px;cursor:pointer;">Next &raquo;</button>
    </div>
</div>
""")

    # Build figure JSON array and trade metadata for JS-side rendering
    trade_meta: list[dict[str, Any]] = []
    trade_figures: list[str] = []
    for trade in trades_sorted:
        ticker = trade["ticker"]
        pnl = trade["pnl"]
        entry_date = trade.get("entry_date", "")
        ticker_bars = bars.get(ticker, [])

        fig = _trade_figure(trade, ticker_bars)
        trade_meta.append({
            "ticker": ticker,
            "pnl": pnl,
            "date": entry_date,
            "portfolio": "",
        })
        trade_figures.append(_fig_to_compact_json(fig) if fig else "null")

    parts.append("<script>")
    parts.append(f"var TRADE_META = {json.dumps(trade_meta, separators=(',', ':'))};")
    parts.append("var TRADE_FIGURES = [" + ",".join(trade_figures) + "];")
    parts.append("</script>")

    parts.append('<div id="trade-container"></div>')
    parts.append("</div>")  # section

    # Navigation JS
    parts.append(_NAV_JS)

    parts.append("</body>\n</html>")

    html = "\n".join(parts)
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    Path(output_path).write_text(html)
    print(f"Report saved to {output_path}")
    return output_path


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def generate_report(json_path: str, output_path: str = "") -> str:
    """Read a backtest JSON file and generate an interactive HTML report.

    Supports both single-portfolio and multi-portfolio JSON formats.

    Args:
        json_path: Path to the backtest JSON file.
        output_path: Path for the output HTML file. If empty, uses the JSON
            filename with a ``.html`` extension.

    Returns:
        The path of the generated HTML file.
    """
    with open(json_path) as f:
        data = json.load(f)

    if not output_path:
        output_path = str(Path(json_path).with_suffix(".html"))

    if _is_multi_portfolio(data):
        return _generate_multi_report(data, output_path)
    return _generate_single_report(data, output_path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


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
