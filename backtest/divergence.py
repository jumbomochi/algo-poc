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
from typing import Iterable, Mapping

from backtest.costs import (
    DEFAULT_COMMISSION_MINIMUM,
    DEFAULT_COMMISSION_PER_SHARE,
    DEFAULT_SLIPPAGE_BPS,
    CostModel,
)

# Thresholds tuned to be quiet during normal operation but loud when something
# real is breaking. Tuned for a 30-day rolling window; tighten for shorter windows.
DEFAULT_WINDOW_DAYS = 30
DEFAULT_THRESHOLD = 0.20  # relative divergence — warn above this
DEFAULT_ABSOLUTE_WARN_PP = 0.025  # 2.5 pp absolute return divergence
DEFAULT_ABSOLUTE_BREACH_PP = 0.05  # 5 pp absolute breach

# The backtest assumes these per-trade frictions. The monitor flags when live
# fills consistently exceed them.
ASSUMED_SLIPPAGE_BPS = DEFAULT_SLIPPAGE_BPS
ASSUMED_COMMISSION_PER_SHARE = DEFAULT_COMMISSION_PER_SHARE

# How a backtest filled its orders. Only a ``next_open`` baseline is a
# like-for-like comparison for live trading: a ``same_bar`` backtest filled
# entries at the decision day's low and exits at that day's open, which live
# cannot do, so live trails it by construction (finding 4.6 of the 2026-08-06
# review — the monitor was baselining against exactly that).
NEXT_OPEN_FILL_MODEL = "next_open"
SAME_BAR_FILL_MODEL = "same_bar"


@dataclass(frozen=True)
class ExecutionModel:
    """The execution assumptions a baseline backtest was run under."""

    fill_model: str
    slippage_bps: float = ASSUMED_SLIPPAGE_BPS
    commission_per_share: float = ASSUMED_COMMISSION_PER_SHARE
    commission_minimum: float = DEFAULT_COMMISSION_MINIMUM

    @property
    def is_like_for_like(self) -> bool:
        """Whether live performance can be fairly compared to this baseline.

        Requires next-open fills *and* a per-order commission floor — without
        the floor the baseline understates cost by 10-20x on the small orders
        the live account actually sends.
        """
        return self.fill_model == NEXT_OPEN_FILL_MODEL and self.commission_minimum > 0

    def commission_for(self, quantity: float) -> float:
        """Commission this baseline assumed for one order of ``quantity``."""
        return CostModel(
            commission_per_share=self.commission_per_share,
            commission_minimum=self.commission_minimum,
        ).commission_for(quantity)


LEGACY_EXECUTION_MODEL = ExecutionModel(
    fill_model=SAME_BAR_FILL_MODEL, commission_minimum=0.0
)
DEFAULT_EXECUTION_MODEL = ExecutionModel(fill_model=NEXT_OPEN_FILL_MODEL)


def execution_model_from_backtest_config(config: Mapping | None) -> ExecutionModel:
    """Read the execution model a saved backtest declared in its ``config``.

    A backtest that declares no ``fill_model`` predates the rebaseline and is
    treated as same-bar (not comparable) rather than optimistically assumed
    correct.
    """
    if not config:
        return LEGACY_EXECUTION_MODEL
    fill_model = str(config.get("fill_model") or SAME_BAR_FILL_MODEL)
    return ExecutionModel(
        fill_model=fill_model,
        slippage_bps=float(config.get("slippage_bps", ASSUMED_SLIPPAGE_BPS)),
        commission_per_share=float(
            config.get("commission_per_share", ASSUMED_COMMISSION_PER_SHARE)
        ),
        commission_minimum=float(config.get("commission_minimum", 0.0)),
    )


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
    # Which execution model the baseline backtest used, and whether comparing
    # live against it is meaningful at all.
    baseline_fill_model: str = NEXT_OPEN_FILL_MODEL
    baseline_comparable: bool = True


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


def commission_totals(
    trades: list[dict],
    execution_model: ExecutionModel | None = None,
) -> tuple[float, float]:
    """Return ``(realized, assumed)`` commission dollars for a list of trades.

    Realized comes from ``trade['commission']``. Assumed is what the baseline
    backtest's cost model charges for the same orders — including its per-order
    floor, so a handful of small live orders is not compared against a
    per-share figure of a few cents. Each Trade row is one exit, but a round
    trip is two orders (entry + exit), so the assumed figure counts both.
    """
    model = execution_model or DEFAULT_EXECUTION_MODEL
    realized = 0.0
    assumed = 0.0
    for t in trades:
        realized += t.get("commission", 0.0) or 0.0
        quantity = abs(t.get("quantity", 0.0) or 0.0)
        if quantity <= 0:
            continue
        assumed += 2 * model.commission_for(quantity)
    return realized, assumed


def build_report(
    portfolio: str,
    live: dict[date, float],
    backtest: dict[date, float],
    trades: list[dict],
    window_days: int = DEFAULT_WINDOW_DAYS,
    threshold: float = DEFAULT_THRESHOLD,
    execution_model: ExecutionModel | None = None,
) -> PortfolioDivergenceReport:
    """Top-level helper that runs the full divergence computation for one portfolio.

    The script feeds this with data it pulled from the DB and the backtest
    JSON. The function is pure so the same inputs always produce the same
    report — tests construct synthetic series directly.

    ``execution_model`` describes how the baseline backtest filled its orders.
    If that baseline is not like-for-like with live execution, the status is
    ``NO_DATA``: the numbers are still computed and shown, but they are not
    evidence of drift, because live trails such a baseline by construction.
    """
    model = execution_model or DEFAULT_EXECUTION_MODEL
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
            baseline_fill_model=model.fill_model,
            baseline_comparable=model.is_like_for_like,
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
    realized_comm, assumed_comm = commission_totals(
        window_trades, execution_model=model
    )
    status = classify_status(rel_div, abs_div, threshold=threshold)

    if status == "BREACH":
        notes.append("Divergence exceeds breach threshold — investigate before next entry.")
    elif status == "WARNING":
        notes.append("Divergence exceeds warning threshold — review at next checkpoint.")

    if not model.is_like_for_like:
        # Refuse to grade live against a baseline it cannot match. Reporting
        # OK/WARNING/BREACH here would either excuse real drift or flag drift
        # that is purely the baseline's optimism.
        notes.append(
            f"Baseline execution model is '{model.fill_model}' with a "
            f"${model.commission_minimum:.2f} commission floor — not "
            "like-for-like with live execution, so the divergence figures below "
            "are not evidence of drift. Re-run the baseline backtest with "
            "next-open fills and real costs "
            "(see docs/operations/backtest-baseline.md)."
        )
        status = "NO_DATA"

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
        baseline_fill_model=model.fill_model,
        baseline_comparable=model.is_like_for_like,
    )


def aggregate_reports(
    reports: list[PortfolioDivergenceReport],
    live_total: dict[date, float],
    backtest_total: dict[date, float],
    all_trades: list[dict],
    window_days: int = DEFAULT_WINDOW_DAYS,
    threshold: float = DEFAULT_THRESHOLD,
    execution_model: ExecutionModel | None = None,
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
        execution_model=execution_model,
    )


def any_breach(reports: list[PortfolioDivergenceReport]) -> bool:
    """True if any report has BREACH status. Used to set the script exit code."""
    return any(r.status == "BREACH" for r in reports)
