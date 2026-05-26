"""Pure functions for comparing live paper-trading equity to backtest expectations.

This module is the math layer for ``scripts/divergence_monitor.py``. It contains
no I/O — the script handles DB queries, file reading, Prometheus, and CLI; the
functions here just take in-memory data and return computed results.

The intent: detect when live performance silently drifts away from what the
backtest predicted, before drawdowns or operational issues compound. Catches
things like:
  - Fills consistently worse than the 10 bps slippage assumed
  - A signal not firing live the same way it fired in backtest
  - Order rejections or stuck positions
  - Universe drift (live trading a ticker no longer in the backtest universe)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Iterable

# Thresholds tuned to be quiet during normal operation but loud when something
# real is breaking. Tuned for a 30-day rolling window; tighten for shorter windows.
DEFAULT_WINDOW_DAYS = 30
DEFAULT_THRESHOLD = 0.20  # relative divergence — warn above this
DEFAULT_ABSOLUTE_WARN_PP = 0.025  # 2.5 pp absolute return divergence
DEFAULT_ABSOLUTE_BREACH_PP = 0.05  # 5 pp absolute breach

# The backtest assumes these per-trade frictions. The monitor flags when live
# fills consistently exceed them.
ASSUMED_SLIPPAGE_BPS = 10.0
ASSUMED_COMMISSION_PER_SHARE = 0.005


@dataclass
class PortfolioDivergenceReport:
    """Result of comparing one portfolio's live vs backtest performance.

    Fields are explicitly Optional where the data may be missing (e.g. no live
    equity yet, no overlapping dates) so the script can render "—" instead of
    misleading zeros.
    """

    portfolio: str
    window_start: date | None
    window_end: date | None
    days_compared: int
    live_return: float | None
    backtest_return: float | None
    absolute_divergence_pp: float | None  # live_ret - bt_ret, in decimal (0.01 = 1 pp)
    relative_divergence: float | None  # (live_ret - bt_ret) / |bt_ret|
    daily_correlation: float | None
    live_trades_in_window: int
    realized_slippage_total: float  # raw dollar slippage from Trade.slippage
    realized_slippage_bps: float | None  # average bps on notional
    realized_commission_total: float
    assumed_commission_total: float
    status: str  # "OK" | "WARNING" | "BREACH" | "NO_DATA"
    notes: list[str] = field(default_factory=list)


def align_and_window(
    live: dict[date, float],
    backtest: dict[date, float],
    window_days: int,
) -> tuple[list[date], list[float], list[float]]:
    """Take the last ``window_days`` of dates that appear in both series.

    Returns (dates, live_values, backtest_values) — same length. Empty lists if
    no overlap. The intersection guards against weekends, holidays, or partial
    backfills where one side has a date the other doesn't.
    """
    shared = sorted(set(live.keys()) & set(backtest.keys()))
    if not shared:
        return [], [], []
    window = shared[-window_days:]
    return window, [live[d] for d in window], [backtest[d] for d in window]


def window_return(values: list[float]) -> float | None:
    """Total return over a series: last/first - 1. ``None`` if degenerate."""
    if len(values) < 2 or values[0] == 0:
        return None
    return values[-1] / values[0] - 1.0


def daily_returns(values: list[float]) -> list[float]:
    """Day-over-day arithmetic returns. Skips zero-denominator transitions."""
    out: list[float] = []
    for i in range(len(values) - 1):
        if values[i] == 0:
            continue
        out.append(values[i + 1] / values[i] - 1.0)
    return out


def correlation(xs: list[float], ys: list[float]) -> float | None:
    """Pearson correlation. ``None`` if undefined (length mismatch, < 2 obs, or
    constant series).
    """
    if len(xs) != len(ys) or len(xs) < 2:
        return None
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    denx = sum((x - mean_x) ** 2 for x in xs)
    deny = sum((y - mean_y) ** 2 for y in ys)
    if denx == 0 or deny == 0:
        return None
    return num / (denx * deny) ** 0.5


def compute_divergence(
    live_ret: float | None,
    bt_ret: float | None,
) -> tuple[float | None, float | None]:
    """Return ``(absolute_pp, relative)``.

    - absolute_pp = live_ret - bt_ret (decimal, so 0.01 = 1 pp)
    - relative = absolute_pp / |bt_ret|, or ``None`` if bt_ret is ~0 (the
      ratio is meaningless when the baseline is flat)
    """
    if live_ret is None or bt_ret is None:
        return None, None
    absolute = live_ret - bt_ret
    if abs(bt_ret) < 1e-9:
        return absolute, None
    return absolute, absolute / abs(bt_ret)


def classify_status(
    relative: float | None,
    absolute_pp: float | None,
    threshold: float = DEFAULT_THRESHOLD,
    absolute_warn_pp: float = DEFAULT_ABSOLUTE_WARN_PP,
    absolute_breach_pp: float = DEFAULT_ABSOLUTE_BREACH_PP,
) -> str:
    """Return ``OK`` / ``WARNING`` / ``BREACH`` / ``NO_DATA``.

    Two-axis test: divergence is concerning if EITHER the relative or absolute
    figure exceeds its threshold. Using both prevents false alarms when the
    backtest return happens to be tiny (relative blows up) AND when both
    returns are large but a fixed pp gap is meaningful.
    """
    if relative is None and absolute_pp is None:
        return "NO_DATA"

    rel_breach = relative is not None and abs(relative) > 2 * threshold
    abs_breach = absolute_pp is not None and abs(absolute_pp) > absolute_breach_pp
    if rel_breach or abs_breach:
        return "BREACH"

    rel_warn = relative is not None and abs(relative) > threshold
    abs_warn = absolute_pp is not None and abs(absolute_pp) > absolute_warn_pp
    if rel_warn or abs_warn:
        return "WARNING"

    return "OK"


def filter_trades_to_window(
    trades: Iterable[dict],
    window_start: date,
    window_end: date,
) -> list[dict]:
    """Trades whose ``exit_date`` falls within [window_start, window_end].

    Uses exit_date rather than entry_date because that's when the realized
    P&L, slippage, and commission land — and that's what we're comparing to
    the backtest's same-window expectation.
    """
    in_window: list[dict] = []
    for t in trades:
        ed = t.get("exit_date")
        if not ed:
            continue
        if isinstance(ed, str):
            try:
                ed = date.fromisoformat(ed)
            except ValueError:
                continue
        if window_start <= ed <= window_end:
            in_window.append(t)
    return in_window


def slippage_bps(trades: list[dict]) -> float | None:
    """Mean realized slippage in basis points, weighted by trade notional.

    Notional = ``|quantity * exit_price|``. Weighting prevents a tiny test
    fill from dominating the average. ``None`` if there are no qualifying
    trades.
    """
    total_slippage_dollars = 0.0
    total_notional = 0.0
    for t in trades:
        slip = t.get("slippage", 0.0) or 0.0
        qty = abs(t.get("quantity", 0.0) or 0.0)
        price = t.get("exit_price") or t.get("price") or 0.0
        notional = qty * price
        if notional <= 0:
            continue
        total_slippage_dollars += slip
        total_notional += notional
    if total_notional == 0:
        return None
    return (total_slippage_dollars / total_notional) * 10000


def commission_totals(trades: list[dict]) -> tuple[float, float]:
    """Return ``(realized, assumed)`` commission dollars for a list of trades.

    Realized comes from ``trade['commission']``; assumed is
    ``shares * ASSUMED_COMMISSION_PER_SHARE`` per fill. Each Trade row is one
    exit, but a round trip is two fills (entry + exit) — the assumed figure
    multiplies by 2 to match.
    """
    realized = 0.0
    assumed_shares = 0.0
    for t in trades:
        realized += t.get("commission", 0.0) or 0.0
        assumed_shares += abs(t.get("quantity", 0.0) or 0.0)
    # Two fills per round trip (entry + exit) at $0.005/share.
    assumed = assumed_shares * ASSUMED_COMMISSION_PER_SHARE * 2
    return realized, assumed


def build_report(
    portfolio: str,
    live: dict[date, float],
    backtest: dict[date, float],
    trades: list[dict],
    window_days: int = DEFAULT_WINDOW_DAYS,
    threshold: float = DEFAULT_THRESHOLD,
) -> PortfolioDivergenceReport:
    """Top-level helper that runs the full divergence computation for one portfolio.

    The script feeds this with data it pulled from the DB and the backtest
    JSON. The function is pure so the same inputs always produce the same
    report — tests construct synthetic series directly.
    """
    dates, lvals, btvals = align_and_window(live, backtest, window_days)
    notes: list[str] = []

    if not dates:
        return PortfolioDivergenceReport(
            portfolio=portfolio,
            window_start=None,
            window_end=None,
            days_compared=0,
            live_return=None,
            backtest_return=None,
            absolute_divergence_pp=None,
            relative_divergence=None,
            daily_correlation=None,
            live_trades_in_window=0,
            realized_slippage_total=0.0,
            realized_slippage_bps=None,
            realized_commission_total=0.0,
            assumed_commission_total=0.0,
            status="NO_DATA",
            notes=["No overlapping dates between live and backtest series."],
        )

    if len(dates) < window_days:
        notes.append(
            f"Only {len(dates)} overlapping days available "
            f"(requested {window_days}). Live history may be too short."
        )

    live_ret = window_return(lvals)
    bt_ret = window_return(btvals)
    abs_div, rel_div = compute_divergence(live_ret, bt_ret)
    corr = correlation(daily_returns(lvals), daily_returns(btvals))
    window_trades = filter_trades_to_window(trades, dates[0], dates[-1])
    slip_total = sum((t.get("slippage", 0.0) or 0.0) for t in window_trades)
    realized_comm, assumed_comm = commission_totals(window_trades)
    status = classify_status(rel_div, abs_div, threshold=threshold)

    if status == "BREACH":
        notes.append("Divergence exceeds breach threshold — investigate before next entry.")
    elif status == "WARNING":
        notes.append("Divergence exceeds warning threshold — review at next checkpoint.")

    return PortfolioDivergenceReport(
        portfolio=portfolio,
        window_start=dates[0],
        window_end=dates[-1],
        days_compared=len(dates),
        live_return=live_ret,
        backtest_return=bt_ret,
        absolute_divergence_pp=abs_div,
        relative_divergence=rel_div,
        daily_correlation=corr,
        live_trades_in_window=len(window_trades),
        realized_slippage_total=slip_total,
        realized_slippage_bps=slippage_bps(window_trades),
        realized_commission_total=realized_comm,
        assumed_commission_total=assumed_comm,
        status=status,
        notes=notes,
    )


def aggregate_reports(
    reports: list[PortfolioDivergenceReport],
    live_total: dict[date, float],
    backtest_total: dict[date, float],
    all_trades: list[dict],
    window_days: int = DEFAULT_WINDOW_DAYS,
    threshold: float = DEFAULT_THRESHOLD,
) -> PortfolioDivergenceReport:
    """Build an aggregate ("portfolio of portfolios") report.

    The caller passes in the summed equity series across sleeves rather than
    relying on this function to sum the inputs — that keeps the responsibility
    for "what counts as the aggregate" with the orchestration layer (which
    knows which sleeves are active).
    """
    return build_report(
        portfolio="AGGREGATE",
        live=live_total,
        backtest=backtest_total,
        trades=all_trades,
        window_days=window_days,
        threshold=threshold,
    )


def any_breach(reports: list[PortfolioDivergenceReport]) -> bool:
    """True if any report has BREACH status. Used to set the script exit code."""
    return any(r.status == "BREACH" for r in reports)
