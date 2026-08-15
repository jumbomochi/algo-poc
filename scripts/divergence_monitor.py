#!/usr/bin/env python3
"""Daily divergence monitor: live paper-trading vs. latest backtest.

For each paper-trading portfolio (and the aggregate), this script:

1. Loads live equity history from the ``equity_snapshots`` table.
2. Loads the corresponding daily equity series from the most recent
   ``output/backtest_multi_*.json``.
3. Aligns the two by date, takes the last N (default 30) trading days, and
   computes return divergence + daily-returns correlation + realized
   slippage/commission from the ``trades`` table.
4. Prints a console table, writes a JSON report, and optionally emits a
   Prometheus textfile for ``node_exporter`` to scrape.
5. Exits non-zero if any portfolio's divergence breaches the threshold, or if
   the baseline backtest is not comparable to live at all — so cron/launchd
   jobs can alert. See ``exit_code_for`` for the contract.

This is **not** a kill switch. It surfaces divergence so a human can decide
whether to investigate, disable a sleeve, or carry on. Automated sleeve
disable on persistent breach can be layered on later (see notes in
``docs/strategies/mean-reversion-failure-analysis.md`` for the phase plan).

Usage:
    python scripts/divergence_monitor.py
    python scripts/divergence_monitor.py --backtest output/backtest_multi_20260526_235302.json
    python scripts/divergence_monitor.py --window 60 --threshold 0.30
    python scripts/divergence_monitor.py --portfolio momentum
    python scripts/divergence_monitor.py --prometheus-textfile /var/lib/node_exporter/textfile/divergence.prom
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from datetime import date, datetime, timezone
from glob import glob
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backtest.divergence import (
    ASSUMED_SLIPPAGE_BPS,
    DEFAULT_THRESHOLD,
    DEFAULT_WINDOW_DAYS,
    ExecutionModel,
    PortfolioDivergenceReport,
    aggregate_reports,
    any_breach,
    build_report,
    execution_model_from_backtest_config,
)
from scripts.paper_state import PaperTradingState
from shared.config import load_config
from shared.universe import is_excluded_portfolio


# ---------------------------------------------------------------------------
# Exit-code contract (mirrored in deploy/launchd/run_divergence.sh)
# ---------------------------------------------------------------------------

EXIT_OK = 0
EXIT_BREACH = 1
EXIT_ERROR = 2
# The monitor ran but could not judge anything: the baseline backtest is not
# like-for-like with live execution, so every report was forced to NO_DATA.
# This needs its own code — folding it into EXIT_OK made a blind monitor
# indistinguishable from a healthy one in the daily log.
EXIT_BASELINE_NOT_COMPARABLE = 3


def exit_code_for(
    reports: list[PortfolioDivergenceReport],
    execution_model: ExecutionModel | None = None,
) -> int:
    """Map the run's outcome onto the process exit code.

    A breach outranks a stale baseline: it is the louder signal, and in practice
    the two cannot co-occur (a non-comparable baseline forces every status to
    NO_DATA). A genuine NO_DATA on a good baseline — no overlapping live history
    yet — is not a fault and stays at EXIT_OK.
    """
    if any_breach(reports):
        return EXIT_BREACH
    if execution_model is not None and not execution_model.is_like_for_like:
        return EXIT_BASELINE_NOT_COMPARABLE
    if any(not r.baseline_comparable for r in reports):
        return EXIT_BASELINE_NOT_COMPARABLE
    return EXIT_OK


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


def find_latest_backtest_json(output_dir: str = "output") -> str | None:
    """Return the path to the most recent ``backtest_multi_*.json``, or None."""
    candidates = sorted(glob(f"{output_dir}/backtest_multi_*.json"))
    return candidates[-1] if candidates else None


def load_backtest_equity_series(
    backtest_path: str,
) -> tuple[dict[str, dict[date, float]], dict[date, float]]:
    """Load per-portfolio and aggregate equity series from a backtest results JSON.

    ``portfolio_values`` has ``len(dates) + 1`` entries — the first is the
    pre-day-0 initial capital. We drop it so that
    ``portfolio_values[i+1]`` aligns with ``dates[i]`` (end-of-day equity).

    Returns:
        ``(per_portfolio, aggregate)`` where each maps ``date -> equity``.
    """
    with open(backtest_path) as f:
        data = json.load(f)

    dates_iso = data["aggregate"]["dates"]
    dates = [date.fromisoformat(d) for d in dates_iso]

    def _series(pv: list[float]) -> dict[date, float]:
        # End-of-day values aligned to dates.
        eod = pv[1:] if len(pv) == len(dates) + 1 else pv
        return dict(zip(dates, eod))

    per_portfolio = {
        name: _series(p["portfolio_values"])
        for name, p in data["portfolios"].items()
    }
    aggregate = _series(data["aggregate"]["portfolio_values"])
    return per_portfolio, aggregate


def load_backtest_execution_model(backtest_path: str) -> ExecutionModel:
    """Read the execution assumptions the baseline backtest ran under.

    A baseline that filled same-bar, or that modelled no per-order commission
    floor, is not something live execution can match — the monitor reports its
    figures but refuses to grade live against it (finding 4.6).
    """
    with open(backtest_path) as f:
        data = json.load(f)
    return execution_model_from_backtest_config(data.get("config"))


def load_live_equity_series(state: PaperTradingState, portfolio: str) -> dict[date, float]:
    """Pull live daily equity for a portfolio from ``equity_snapshots``."""
    rows = state.get_equity_history(portfolio)
    return {
        date.fromisoformat(r["date"]): float(r["equity"])
        for r in rows
    }


def load_live_aggregate_series(
    per_portfolio: dict[str, dict[date, float]],
) -> dict[date, float]:
    """Sum per-portfolio equity across dates that appear in every portfolio.

    Restricting to fully-overlapping dates avoids spurious aggregate dips
    when one sleeve happens to be missing a snapshot for a given day.
    """
    if not per_portfolio:
        return {}
    common_dates = set.intersection(*(set(s.keys()) for s in per_portfolio.values()))
    return {
        d: sum(s[d] for s in per_portfolio.values())
        for d in sorted(common_dates)
    }


# ---------------------------------------------------------------------------
# Console rendering
# ---------------------------------------------------------------------------


STATUS_GLYPHS = {"OK": "✓", "WARNING": "⚠", "BREACH": "✗", "NO_DATA": "·"}


def _fmt_pct(v: float | None, decimals: int = 2) -> str:
    return f"{v * 100:+.{decimals}f}%" if v is not None else "—"


def _fmt_money(v: float | None) -> str:
    return f"${v:,.2f}" if v is not None else "—"


def _fmt_corr(v: float | None) -> str:
    return f"{v:+.3f}" if v is not None else "—"


def print_report_table(
    reports: list[PortfolioDivergenceReport],
    window_days: int,
    threshold: float,
    execution_model: ExecutionModel | None = None,
) -> None:
    print("=" * 110)
    print(f"  DIVERGENCE MONITOR — window {window_days} days, threshold {threshold:.0%}")
    if execution_model is not None:
        print(
            f"  Baseline execution: {execution_model.fill_model} fills, "
            f"{execution_model.slippage_bps:.0f} bps slippage, "
            f"max(${execution_model.commission_minimum:.2f}, "
            f"${execution_model.commission_per_share}/share) commission"
            + ("" if execution_model.is_like_for_like else "  [NOT LIKE-FOR-LIKE]")
        )
    print("=" * 110)
    print()
    header = (
        f"  {'':2}  {'Portfolio':<22}{'Days':>6}{'Live':>10}{'Backtest':>10}"
        f"{'Δ pp':>10}{'Δ rel':>10}{'Corr':>8}{'Slip bps':>11}{'Trades':>8}"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))
    for r in reports:
        glyph = STATUS_GLYPHS.get(r.status, "?")
        print(
            f"  {glyph:<2}  {r.portfolio:<22}{r.days_compared:>6}"
            f"{_fmt_pct(r.live_return, 1):>10}"
            f"{_fmt_pct(r.backtest_return, 1):>10}"
            f"{_fmt_pct(r.absolute_divergence_pp, 2):>10}"
            f"{_fmt_pct(r.relative_divergence, 1):>10}"
            f"{_fmt_corr(r.daily_correlation):>8}"
            f"{(f'{r.realized_slippage_bps:.1f}' if r.realized_slippage_bps is not None else '—'):>11}"
            f"{r.live_trades_in_window:>8}"
        )
    print()

    # Surface anything non-OK in plain English at the bottom.
    actionable = [
        r for r in reports
        if r.status in ("WARNING", "BREACH") or not r.baseline_comparable
    ]
    assumed_slippage = (
        execution_model.slippage_bps
        if execution_model is not None
        else ASSUMED_SLIPPAGE_BPS
    )
    for r in actionable:
        print(f"  [{r.status}] {r.portfolio}:")
        for note in r.notes:
            print(f"     • {note}")
        if (
            r.realized_slippage_bps is not None
            and r.realized_slippage_bps > 1.5 * assumed_slippage
        ):
            print(
                f"     • Realized slippage {r.realized_slippage_bps:.1f} bps "
                f"exceeds the {assumed_slippage:.0f} bps backtest assumption."
            )
        if r.assumed_commission_total > 0 and r.realized_commission_total > 1.5 * r.assumed_commission_total:
            print(
                f"     • Realized commission ${r.realized_commission_total:.2f} is "
                f"{r.realized_commission_total / r.assumed_commission_total:.1f}× "
                f"the ${r.assumed_commission_total:.2f} assumed."
            )
    if actionable:
        print()


# ---------------------------------------------------------------------------
# JSON / Prometheus output
# ---------------------------------------------------------------------------


def write_json_report(
    reports: list[PortfolioDivergenceReport],
    output_path: str,
    backtest_path: str,
    window_days: int,
    threshold: float,
    execution_model: ExecutionModel | None = None,
) -> None:
    """Write the full report to a JSON file for historical tracking / Grafana."""

    def _serialize(r: PortfolioDivergenceReport) -> dict[str, Any]:
        d = asdict(r)
        # asdict() turns dates into date objects; serialize as ISO strings.
        d["window_start"] = r.window_start.isoformat() if r.window_start else None
        d["window_end"] = r.window_end.isoformat() if r.window_end else None
        return d

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "backtest_source": backtest_path,
        "window_days": window_days,
        "threshold": threshold,
        "reports": [_serialize(r) for r in reports],
    }
    if execution_model is not None:
        payload["baseline_execution_model"] = asdict(execution_model)
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"  Report saved to {output_path}")


def write_prometheus_textfile(
    reports: list[PortfolioDivergenceReport],
    textfile_path: str,
) -> None:
    """Write Prometheus-format gauges for ``node_exporter`` textfile collector.

    Atomic-write into a ``.prom`` file. node_exporter's textfile collector
    will pick it up on next scrape; no metrics HTTP server needed.
    """
    from prometheus_client import CollectorRegistry, Gauge, write_to_textfile

    registry = CollectorRegistry()
    g_abs = Gauge(
        "algo_poc_divergence_absolute_pp",
        "Absolute return divergence (live - backtest) in decimal (0.01 = 1 pp).",
        ["portfolio"], registry=registry,
    )
    g_rel = Gauge(
        "algo_poc_divergence_relative",
        "Relative return divergence: (live - backtest) / |backtest|.",
        ["portfolio"], registry=registry,
    )
    g_corr = Gauge(
        "algo_poc_divergence_correlation",
        "Pearson correlation of daily returns over the window.",
        ["portfolio"], registry=registry,
    )
    g_slip = Gauge(
        "algo_poc_realized_slippage_bps",
        "Realized slippage in basis points, weighted by notional.",
        ["portfolio"], registry=registry,
    )
    g_comm = Gauge(
        "algo_poc_realized_commission_dollars",
        "Realized commission dollars over the window.",
        ["portfolio"], registry=registry,
    )
    g_status = Gauge(
        "algo_poc_divergence_status",
        "Status (0=OK, 1=WARNING, 2=BREACH, 3=NO_DATA).",
        ["portfolio"], registry=registry,
    )

    status_code = {"OK": 0, "WARNING": 1, "BREACH": 2, "NO_DATA": 3}

    for r in reports:
        if r.absolute_divergence_pp is not None:
            g_abs.labels(portfolio=r.portfolio).set(r.absolute_divergence_pp)
        if r.relative_divergence is not None:
            g_rel.labels(portfolio=r.portfolio).set(r.relative_divergence)
        if r.daily_correlation is not None:
            g_corr.labels(portfolio=r.portfolio).set(r.daily_correlation)
        if r.realized_slippage_bps is not None:
            g_slip.labels(portfolio=r.portfolio).set(r.realized_slippage_bps)
        g_comm.labels(portfolio=r.portfolio).set(r.realized_commission_total)
        g_status.labels(portfolio=r.portfolio).set(status_code.get(r.status, -1))

    Path(textfile_path).parent.mkdir(parents=True, exist_ok=True)
    write_to_textfile(textfile_path, registry)
    print(f"  Prometheus textfile written to {textfile_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    # Load default DB URL from config (may fail if config file missing, that's OK).
    try:
        default_db_url = load_config("config/default.yaml").database.url
    except Exception:
        # Honour ALGO_DATABASE_URL even when the config file can't be loaded, so
        # a missing/unreadable config can't silently fall back to the wrong DB
        # (the launchd wrapper always exports ALGO_DATABASE_URL).
        default_db_url = os.environ.get(
            "ALGO_DATABASE_URL", "postgresql://algo:algo@localhost:5432/algo_poc"
        )

    parser = argparse.ArgumentParser(
        description="Compare live paper-trading equity to backtest expectations."
    )
    parser.add_argument(
        "--backtest", default=None,
        help="Path to backtest results JSON. Default: latest output/backtest_multi_*.json",
    )
    parser.add_argument(
        "--window", type=int, default=DEFAULT_WINDOW_DAYS,
        help=f"Rolling window size in trading days (default {DEFAULT_WINDOW_DAYS}).",
    )
    parser.add_argument(
        "--threshold", type=float, default=DEFAULT_THRESHOLD,
        help=f"Relative divergence warning threshold (default {DEFAULT_THRESHOLD}).",
    )
    parser.add_argument(
        "--portfolio", default=None,
        help="Limit comparison to a single named portfolio.",
    )
    parser.add_argument(
        "--output", default=None,
        help="Path to write JSON output. Default: output/divergence_<YYYYMMDD>.json",
    )
    parser.add_argument(
        "--no-output", action="store_true",
        help="Skip writing the JSON output file.",
    )
    parser.add_argument(
        "--prometheus-textfile", default=None,
        help="Write Prometheus gauges to this .prom file for node_exporter to scrape.",
    )
    parser.add_argument("--db-url", default=default_db_url, help="PostgreSQL database URL.")
    args = parser.parse_args()

    # --- Resolve inputs ---
    backtest_path = args.backtest or find_latest_backtest_json()
    if backtest_path is None:
        print("ERROR: No backtest JSON found. Pass --backtest or run scripts/run_backtest.py first.")
        return EXIT_ERROR
    if not Path(backtest_path).is_file():
        print(f"ERROR: Backtest file not found: {backtest_path}")
        return EXIT_ERROR
    print(f"  Backtest source: {backtest_path}")

    bt_per_portfolio, bt_aggregate = load_backtest_equity_series(backtest_path)
    execution_model = load_backtest_execution_model(backtest_path)
    if not execution_model.is_like_for_like:
        print(
            "  ⚠ Baseline is not like-for-like with live: "
            + "; ".join(execution_model.unmet_requirements())
            + ". Divergence will be reported as NO_DATA; re-run the baseline "
            "per docs/operations/backtest-baseline.md."
        )

    # --- Open DB and load paper state ---
    # SQLAlchemy lazy-connects, so the actual connection attempt happens inside
    # PaperTradingState.load() when it runs its first SELECT. Wrap both phases.
    try:
        engine = create_engine(args.db_url)
        session_factory = sessionmaker(bind=engine)
        session: Session = session_factory()
    except Exception as e:
        print(f"ERROR: Failed to construct DB engine ({args.db_url}): {e}")
        return EXIT_ERROR

    try:
        state = PaperTradingState.load(session)
    except ValueError:
        print("ERROR: No paper trading state in DB. Run scripts/run_paper.py --init first.")
        return EXIT_ERROR
    except Exception as e:
        # Auth failure, network unreachable, missing tables, etc.
        print(f"ERROR: Could not load paper state from DB ({args.db_url}):")
        print(f"       {type(e).__name__}: {e}")
        print(
            "       Check that the DB is running, credentials are correct, "
            "and migrations have run (`alembic upgrade head`)."
        )
        return EXIT_ERROR

    portfolios = state.get_portfolio_names()
    if args.portfolio:
        if args.portfolio not in portfolios:
            print(
                f"ERROR: Portfolio '{args.portfolio}' not in DB. "
                f"Available: {', '.join(portfolios)}"
            )
            return EXIT_ERROR
        portfolios = [args.portfolio]

    # --- Build per-portfolio reports ---
    reports: list[PortfolioDivergenceReport] = []
    live_series_by_portfolio: dict[str, dict[date, float]] = {}
    for name in portfolios:
        # Synthetic portfolios ("__drill__", "_aggregate", "__liquidation__")
        # are never divergence input: a drill's fills exist to prove the safety
        # machinery works, not to be scored against a strategy baseline. Today
        # such a sleeve is also absent from the backtest JSON and would fall
        # through the branch below, but that is incidental — the exclusion is
        # explicit here so it cannot silently stop working if a same-named
        # sleeve ever appears in a baseline.
        if is_excluded_portfolio(name):
            print(
                f"  ⚠ Skipping '{name}': excluded portfolio (synthetic/drill "
                f"tag — never scored against a backtest sleeve; see "
                f"docs/operations/drill-evidence-isolation.md)."
            )
            continue
        live = load_live_equity_series(state, name)
        live_series_by_portfolio[name] = live
        if name not in bt_per_portfolio:
            print(
                f"  ⚠ Skipping '{name}': not present in backtest "
                f"(likely a sleeve that was dropped — see "
                f"docs/strategies/ for the rationale)."
            )
            continue
        trades = state.get_trades(name)
        report = build_report(
            portfolio=name,
            live=live,
            backtest=bt_per_portfolio[name],
            trades=trades,
            window_days=args.window,
            threshold=args.threshold,
            execution_model=execution_model,
        )
        reports.append(report)

    # --- Aggregate report (only over sleeves that exist in both) ---
    if not args.portfolio:
        comparable = {
            n: s for n, s in live_series_by_portfolio.items()
            if n in bt_per_portfolio
        }
        live_total = load_live_aggregate_series(comparable)
        all_trades = state.get_all_trades()
        # Filter to only trades from portfolios that are in the comparison.
        all_trades = [t for t in all_trades if t["portfolio"] in comparable]
        agg_report = aggregate_reports(
            reports=reports,
            live_total=live_total,
            backtest_total=bt_aggregate,
            all_trades=all_trades,
            window_days=args.window,
            threshold=args.threshold,
            execution_model=execution_model,
        )
        reports.append(agg_report)

    # --- Output ---
    print()
    print_report_table(
        reports,
        window_days=args.window,
        threshold=args.threshold,
        execution_model=execution_model,
    )

    if not args.no_output:
        output_path = args.output or f"output/divergence_{date.today().strftime('%Y%m%d')}.json"
        write_json_report(
            reports, output_path,
            backtest_path=backtest_path,
            window_days=args.window,
            threshold=args.threshold,
            execution_model=execution_model,
        )

    if args.prometheus_textfile:
        write_prometheus_textfile(reports, args.prometheus_textfile)

    return exit_code_for(reports, execution_model)


if __name__ == "__main__":
    sys.exit(main())
