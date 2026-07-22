#!/usr/bin/env python3
"""Compare ranked candidate replacement policies on identical cached bars."""

# Direct script execution needs the worktree bootstrap below before local imports.
# ruff: noqa: E402

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

# Keep direct ``python scripts/...`` execution bound to this worktree instead
# of an editable-package path from the primary checkout.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backtest.metrics import BacktestMetrics
from backtest.ranked_selection import ReplacementPolicy
from backtest.runner import BacktestRunner
from backtest.simulator import SimulatedExecutor
from scripts.fetch_fundamentals import (
    SECTOR_MAP,
    build_fundamentals_lookup,
    load_fundamentals_cache,
)
from scripts.run_backtest import (
    PortfolioConfig,
    compute_aggregate_metrics,
    compute_regime_by_date,
    make_quality_value_signals_fn,
    make_thematic_momentum_signals_fn,
)
from services.risk_management.engine import RiskEngine
from shared.universe import UNIVERSE_REGISTRY


def annualized_turnover(
    trades: Sequence[Mapping[str, Any]],
    portfolio_values: Sequence[float],
    dates: Sequence[date],
) -> float:
    """Return closed-trade notional turnover per year over average NAV."""
    if not trades or not portfolio_values or len(dates) < 2:
        return 0.0
    elapsed_days = (dates[-1] - dates[0]).days
    if elapsed_days <= 0:
        return 0.0
    average_nav = sum(float(value) for value in portfolio_values) / len(
        portfolio_values
    )
    if average_nav <= 0:
        return 0.0
    bought_notional = sum(
        abs(float(trade["entry_price"]) * float(trade["quantity"]))
        for trade in trades
    )
    sold_notional = sum(
        abs(float(trade["exit_price"]) * float(trade["quantity"]))
        for trade in trades
    )
    traded_notional = (bought_notional + sold_notional) / 2.0
    years = elapsed_days / 365.25
    return traded_notional / average_nav / years


def promotion_decision(
    policies: Mapping[str, Mapping[str, Any]],
    *,
    turnover_ceiling: float = 2.0,
) -> dict[str, Any]:
    """Apply the documented Sharpe, drawdown, and turnover promotion gates."""
    baseline = policies[ReplacementPolicy.TECHNICAL_ONLY.value]
    baseline_walk_forward = baseline["walk_forward"]
    candidates: dict[str, Any] = {}

    for policy in (ReplacementPolicy.WEAKEST, ReplacementPolicy.SCORE_MARGIN):
        candidate = policies[policy.value]
        walk_forward = candidate["walk_forward"]
        checks = {
            "sharpe_ratio": float(walk_forward["mean_sharpe_ratio"])
            > float(baseline_walk_forward["mean_sharpe_ratio"]),
            "max_drawdown": float(walk_forward["max_drawdown"])
            <= float(baseline_walk_forward["max_drawdown"]),
            "annual_turnover": float(candidate["annual_turnover"])
            <= turnover_ceiling,
        }
        candidates[policy.value] = {
            "eligible": all(checks.values()),
            "checks": checks,
        }

    eligible = [
        policy
        for policy, result in candidates.items()
        if result["eligible"]
    ]
    recommended = max(
        eligible,
        key=lambda policy: float(
            policies[policy]["walk_forward"]["mean_sharpe_ratio"]
        ),
        default=ReplacementPolicy.TECHNICAL_ONLY.value,
    )
    return {
        "recommended_policy": recommended,
        "candidates": candidates,
        "turnover_ceiling": turnover_ceiling,
    }


def walk_forward_summary(
    *,
    portfolio_values: Sequence[float],
    dates: Sequence[date],
    trades: Sequence[Mapping[str, Any]],
    window_days: int = 252,
) -> dict[str, Any]:
    """Summarize non-overlapping chronological validation windows."""
    if window_days <= 0:
        raise ValueError("window_days must be positive")
    if len(portfolio_values) != len(dates) + 1:
        raise ValueError("portfolio_values must contain one initial value plus each date")

    windows: list[dict[str, Any]] = []
    for start in range(0, len(dates), window_days):
        window_dates = list(dates[start : start + window_days])
        if not window_dates:
            continue
        window_values = list(
            portfolio_values[start : start + len(window_dates) + 1]
        )
        window_trades = [
            dict(trade)
            for trade in trades
            if window_dates[0] <= trade["exit_date"] <= window_dates[-1]
        ]
        metrics = BacktestMetrics.compute(window_values, window_trades)
        windows.append(
            {
                "start_date": window_dates[0].isoformat(),
                "end_date": window_dates[-1].isoformat(),
                "trading_days": len(window_dates),
                "total_return": metrics["total_return"],
                "sharpe_ratio": metrics["sharpe_ratio"],
                "max_drawdown": metrics["max_drawdown"],
                "trade_count": metrics["total_trades"],
            }
        )

    return {
        "window_days": window_days,
        "windows": windows,
        "mean_sharpe_ratio": (
            sum(window["sharpe_ratio"] for window in windows) / len(windows)
            if windows
            else 0.0
        ),
        "max_drawdown": max(
            (window["max_drawdown"] for window in windows), default=0.0
        ),
    }


def _strategy_summary(result: Any) -> dict[str, Any]:
    return {
        "total_return": result.metrics["total_return"],
        "sharpe_ratio": result.metrics["sharpe_ratio"],
        "max_drawdown": result.metrics["max_drawdown"],
        "trade_count": result.metrics["total_trades"],
        "annual_turnover": annualized_turnover(
            result.trades, result.portfolio_values, result.dates
        ),
    }


def evaluate_replacement_policies(
    bars_by_ticker: dict[str, list[dict]],
    *,
    fundamentals_lookup: Any,
    sector_map: Mapping[str, str],
    quality_universe: Sequence[str],
    thematic_universe: Sequence[str],
    capital: float = 100_000.0,
    score_margin: float = 0.25,
    walk_forward_days: int = 252,
    slippage_bps: int = 10,
    commission_per_share: float = 0.005,
    turnover_ceiling: float = 2.0,
) -> dict[str, Any]:
    """Run all replacement policies against the same immutable bar set."""
    if capital <= 0:
        raise ValueError("capital must be positive")
    if score_margin < 0:
        raise ValueError("score_margin must be non-negative")

    regime_by_date = compute_regime_by_date(bars_by_ticker)
    policies: dict[str, dict[str, Any]] = {}

    for policy in ReplacementPolicy:
        quality_capital = capital * 0.1538
        thematic_capital = capital * 0.1410
        quality_signals = make_quality_value_signals_fn(
            fundamentals_lookup=fundamentals_lookup,
            sector_map=dict(sector_map),
            bars_by_ticker=bars_by_ticker,
            eligible_tickers=list(quality_universe),
            top_n=15,
            position_size_pct=0.06,
            initial_capital=quality_capital,
            trailing_stop_pct=0.12,
            regime_by_date=regime_by_date,
            replacement_policy=policy,
            replacement_score_margin=score_margin,
        )
        thematic_signals = make_thematic_momentum_signals_fn(
            bars_by_ticker=bars_by_ticker,
            eligible_tickers=list(thematic_universe),
            top_n=8,
            lookback_days=63,
            position_size_pct=0.135,
            initial_capital=thematic_capital,
            trailing_stop_pct=0.10,
            regime_by_date=regime_by_date,
            replacement_policy=policy,
            replacement_score_margin=score_margin,
        )
        portfolio_configs = {
            "quality_value": PortfolioConfig(
                name="quality_value",
                capital=quality_capital,
                signals_fn=quality_signals,
                risk_engine=RiskEngine(
                    position_entry_limit_pct=10.0,
                    sector_concentration_pct=30.0,
                    total_exposure_limit_pct=100.0,
                    max_lots_per_ticker=1,
                ),
            ),
            "thematic_momentum": PortfolioConfig(
                name="thematic_momentum",
                capital=thematic_capital,
                signals_fn=thematic_signals,
                risk_engine=RiskEngine(
                    position_entry_limit_pct=15.0,
                    sector_concentration_pct=50.0,
                    total_exposure_limit_pct=120.0,
                    max_lots_per_ticker=1,
                ),
            ),
        }
        executor = SimulatedExecutor(
            slippage_bps=slippage_bps,
            commission_per_share=commission_per_share,
        )
        results = {
            name: BacktestRunner(executor, config.capital).run(
                bars_by_ticker, config.signals_fn, config.risk_engine
            )
            for name, config in portfolio_configs.items()
        }
        aggregate = compute_aggregate_metrics(results, portfolio_configs)
        metrics = aggregate["metrics"]
        policies[policy.value] = {
            "total_return": metrics["total_return"],
            "sharpe_ratio": metrics["sharpe_ratio"],
            "max_drawdown": metrics["max_drawdown"],
            "trade_count": metrics["total_trades"],
            "annual_turnover": annualized_turnover(
                aggregate["trades"],
                aggregate["portfolio_values"],
                aggregate["dates"],
            ),
            "walk_forward": walk_forward_summary(
                portfolio_values=aggregate["portfolio_values"],
                dates=aggregate["dates"],
                trades=aggregate["trades"],
                window_days=walk_forward_days,
            ),
            "strategies": {
                name: _strategy_summary(result) for name, result in results.items()
            },
        }

    return {
        "config": {
            "capital": capital,
            "score_margin": score_margin,
            "walk_forward_days": walk_forward_days,
            "slippage_bps": slippage_bps,
            "commission_per_share": commission_per_share,
            "turnover_ceiling": turnover_ceiling,
        },
        "policies": policies,
        "promotion": promotion_decision(
            policies, turnover_ceiling=turnover_ceiling
        ),
    }


def load_cached_bars(path: Path) -> dict[str, list[dict]]:
    """Load date-normalized bars from a prior backtest JSON file."""
    with path.open() as handle:
        payload = json.load(handle)
    bars = payload.get("bars")
    if not bars:
        raise ValueError(f"No bars found in {path}")
    return {
        ticker: [
            {
                **bar,
                "date": (
                    date.fromisoformat(bar["date"])
                    if isinstance(bar["date"], str)
                    else bar["date"]
                ),
            }
            for bar in ticker_bars
        ]
        for ticker, ticker_bars in bars.items()
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate ranked replacement policies on cached backtest bars"
    )
    parser.add_argument("--bars-from-json", type=Path, required=True)
    parser.add_argument(
        "--fundamentals",
        type=Path,
        default=Path("data/cache/fundamentals.json"),
    )
    parser.add_argument("--capital", type=float, default=100_000.0)
    parser.add_argument("--score-margin", type=float, default=0.25)
    parser.add_argument("--walk-forward-days", type=int, default=252)
    parser.add_argument("--slippage-bps", type=int, default=10)
    parser.add_argument("--commission", type=float, default=0.005)
    parser.add_argument("--turnover-ceiling", type=float, default=2.0)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    bars = load_cached_bars(args.bars_from_json)
    fundamentals_cache = load_fundamentals_cache(str(args.fundamentals))
    if not fundamentals_cache:
        parser.error(f"No fundamentals found in {args.fundamentals}")
    report = evaluate_replacement_policies(
        bars,
        fundamentals_lookup=build_fundamentals_lookup(fundamentals_cache),
        sector_map=SECTOR_MAP,
        quality_universe=UNIVERSE_REGISTRY["quality_value"],
        thematic_universe=UNIVERSE_REGISTRY["thematic_momentum"],
        capital=args.capital,
        score_margin=args.score_margin,
        walk_forward_days=args.walk_forward_days,
        slippage_bps=args.slippage_bps,
        commission_per_share=args.commission,
        turnover_ceiling=args.turnover_ceiling,
    )
    report["source_bars"] = str(args.bars_from_json)
    report["generated_at"] = datetime.now().astimezone().isoformat()

    output = args.output or Path("output") / (
        f"replacement_policy_validation_{datetime.now():%Y%m%d_%H%M%S}.json"
    )
    if output.exists():
        parser.error(f"Refusing to overwrite existing output: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x") as handle:
        json.dump(report, handle, indent=2)
    print(f"Validation report saved to {output}")
    print(
        "Recommended policy: "
        f"{report['promotion']['recommended_policy']} "
        "(scheduled paper remains technical_only until explicitly changed)"
    )


if __name__ == "__main__":
    main()
