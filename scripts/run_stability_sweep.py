#!/usr/bin/env python3
"""Sweep one sleeve parameter across a grid and judge the surface it traces.

Usage:
    python scripts/run_stability_sweep.py \\
        --sleeve momentum --parameter lookback_days \\
        --grid 100,113,126,139,152 --center 126 \\
        --bars-from-json data/cache/bars.json \\
        --out output/stability/momentum-lookback_days.json

Direction doc D10 asks whether a shipped parameter value sits on a plateau or
on a knife's edge. The verdict itself lives in
``research.evaluation.stability``, which is pure and may not import
``backtest`` (``tests/research/test_architecture.py``). This driver is the
other half: it lives outside ``research/``, replays one real sleeve once per
grid point, and writes the ``{parameter value -> metric}`` mapping the analysis
consumes -- crossing the architectural boundary as a file, the way
``--bars-from-json`` already does.

The metric is annualised Sharpe from the sleeve's own equity curve. Every grid
point replays the *same* bars over the *same* dates with only the swept
parameter changed, so the differences between points are attributable to the
parameter and nothing else.

Scope: one parameter at a time (a full interaction grid is a different, much
larger exercise), and only the sleeves whose signal functions need nothing but
bars. ``thematic_momentum``, ``quality_value``, ``earnings_drift`` and
``tail_risk_hedge`` additionally need the regime series, the fundamentals cache
or the earnings cache; wiring those in is deliberately left out rather than
half-done, because a sweep run against a missing cache would silently measure a
sleeve that never trades.
"""

# Direct script execution needs the worktree bootstrap below before local imports.
# ruff: noqa: E402

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable

# When invoked as ``python scripts/run_stability_sweep.py``, prefer this
# worktree over any editable-package path installed from another checkout.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backtest.metrics import BacktestMetrics
from backtest.runner import BacktestRunner
from backtest.simulator import SimulatedExecutor
from research.evaluation.stability import (
    DEFAULT_PLATEAU_TOLERANCE,
    parameter_stability,
)
from scripts.run_backtest import (
    ALWAYS_TRADABLE,
    build_cost_model,
    load_membership_calendar,
    make_momentum_signals_fn,
    make_sector_rotation_signals_fn,
)
from services.risk_management.engine import RiskEngine
from shared.universe import BEAR_TICKERS, UNIVERSE_REGISTRY, MembershipCalendar


@dataclass(frozen=True)
class SleeveSpec:
    """One sweepable sleeve, mirroring its call site in ``run_backtest``."""

    name: str
    signals_factory: Callable[..., Any]
    #: The sweepable parameters the shipped backtest passes, at the values it
    #: passes them. Sweeping a parameter overrides exactly one of these.
    defaults: dict[str, Any]
    #: Fraction of total capital the sleeve runs, from run_backtest's split.
    capital_fraction: float
    risk_engine_kwargs: dict[str, Any] = field(default_factory=dict)
    #: Non-numeric arguments the shipped call site passes that are not
    #: sweepable but do change behaviour (momentum's inverse-ETF set, for one).
    fixed_kwargs: dict[str, Any] = field(default_factory=dict)
    #: Whether a point-in-time calendar narrows this sleeve's candidate list.
    #: The ETF sleeves are not index constituents, so it does not.
    scoped_by_membership: bool = False

    def build_risk_engine(self) -> RiskEngine:
        return RiskEngine(**self.risk_engine_kwargs)

    def eligible_tickers(self, membership: MembershipCalendar | None) -> list[str]:
        """The sleeve's candidate list, mirroring run_backtest's construction."""
        if membership is None or not self.scoped_by_membership:
            return list(UNIVERSE_REGISTRY[self.name])
        equity = [
            ticker
            for ticker in membership.all_tickers()
            if ticker not in ALWAYS_TRADABLE
        ]
        return equity + sorted(BEAR_TICKERS)


# Values copied from scripts/run_backtest.py's portfolio construction. Kept in
# a table so the sweep is provably measuring the shipped configuration;
# tests/scripts/test_run_stability_sweep.py asserts the momentum row against it.
SLEEVES: dict[str, SleeveSpec] = {
    "momentum": SleeveSpec(
        name="momentum",
        signals_factory=make_momentum_signals_fn,
        defaults={
            "top_n": 5,
            "lookback_days": 126,
            "position_size_pct": 0.12,
            "trailing_stop_pct": 0.10,
        },
        capital_fraction=0.2308,
        risk_engine_kwargs={
            "position_entry_limit_pct": 12.0,
            "sector_concentration_pct": 30.0,
            "total_exposure_limit_pct": 150.0,
            "max_lots_per_ticker": 1,
        },
        fixed_kwargs={"bear_tickers": BEAR_TICKERS},
        scoped_by_membership=True,
    ),
    "sector_rotation": SleeveSpec(
        name="sector_rotation",
        signals_factory=make_sector_rotation_signals_fn,
        defaults={
            "top_n": 3,
            "lookback_days": 63,
            "position_size_pct": 0.20,
            "trailing_stop_pct": 0.08,
        },
        capital_fraction=0.1538,
        risk_engine_kwargs={
            "position_entry_limit_pct": 20.0,
            "sector_concentration_pct": 50.0,
            "total_exposure_limit_pct": 100.0,
            "max_lots_per_ticker": 1,
        },
    ),
}

# Parameters that must stay integral when swept: passing 126.0 where the sleeve
# indexes a list would fail deep inside the ranking code.
_INTEGER_PARAMETERS = frozenset({"lookback_days", "top_n"})


def parse_grid(raw: str) -> list[float]:
    """Parse ``"100,126,152"`` into ascending floats.

    Duplicates are rejected rather than collapsed: a repeated point silently
    shrinks the neighborhood the verdict is computed over.
    """
    values: list[float] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            values.append(float(token))
        except ValueError:
            raise ValueError(
                f"grid point {token!r} is not numeric; expected a "
                "comma-separated list like 100,126,152"
            ) from None
    if len(set(values)) != len(values):
        raise ValueError(f"grid {raw!r} contains a duplicate point")
    return sorted(values)


def load_bars(path: str, tickers: list[str]) -> dict[str, list[dict]]:
    """Load the cached-bar JSON that ``run_backtest --bars-from-json`` reads."""
    with open(path) as handle:
        cached = json.load(handle)
    wanted = set(tickers)
    return {
        ticker: [{**bar, "date": date.fromisoformat(bar["date"])} for bar in bars]
        for ticker, bars in (cached.get("bars") or {}).items()
        if ticker in wanted
    }


def run_grid_point(
    spec: SleeveSpec,
    *,
    parameter: str,
    value: float,
    bars_by_ticker: dict[str, list[dict]],
    eligible_tickers: list[str],
    capital: float,
    cost_model: Any,
    membership: MembershipCalendar | None = None,
) -> dict:
    """Replay the sleeve once with ``parameter`` set to ``value``."""
    kwargs = dict(spec.defaults)
    kwargs.update(spec.fixed_kwargs)
    kwargs[parameter] = int(value) if parameter in _INTEGER_PARAMETERS else value

    factory_kwargs = dict(kwargs)
    if spec.scoped_by_membership:
        factory_kwargs["membership"] = membership
    signals_fn = spec.signals_factory(
        bars_by_ticker=bars_by_ticker,
        eligible_tickers=eligible_tickers,
        initial_capital=capital,
        **factory_kwargs,
    )
    runner = BacktestRunner(
        executor=SimulatedExecutor(cost_model), initial_capital=capital
    )
    result = runner.run(
        bars_by_ticker,
        signals_fn,
        spec.build_risk_engine(),
        portfolio_name=spec.name,
        membership=membership,
    )
    metrics = BacktestMetrics.compute(result.portfolio_values, result.trades)
    return {"value": value, **metrics}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Sweep one sleeve parameter and judge its stability",
    )
    parser.add_argument("--sleeve", required=True, choices=sorted(SLEEVES))
    parser.add_argument("--parameter", required=True,
                        help="Sleeve parameter to sweep (e.g. lookback_days)")
    parser.add_argument("--grid", required=True,
                        help="Comma-separated parameter values to test")
    parser.add_argument("--center", required=True, type=float,
                        help="The shipped value; must be one of the grid points")
    parser.add_argument("--capital", type=float, default=100_000,
                        help="Total backtest capital; the sleeve takes its share")
    parser.add_argument("--bars-from-json", required=True,
                        help="Cached bars, same format as run_backtest.py")
    parser.add_argument("--universe-snapshots", default=None,
                        help="Point-in-time membership calendar; without it the "
                             "swept surface is survivorship biased")
    parser.add_argument("--metric", default="sharpe_ratio",
                        help="Metric key from BacktestMetrics.compute to sweep on")
    parser.add_argument("--plateau-tolerance", type=float,
                        default=DEFAULT_PLATEAU_TOLERANCE,
                        help="Fraction the neighborhood may fall below the center")
    parser.add_argument("--out", required=True, help="Path for the mapping file")
    args = parser.parse_args(argv)

    spec = SLEEVES[args.sleeve]
    if args.parameter not in spec.defaults:
        parser.error(
            f"sleeve {args.sleeve!r} takes no parameter {args.parameter!r}; "
            f"sweepable: {', '.join(sorted(spec.defaults))}"
        )

    try:
        grid = parse_grid(args.grid)
    except ValueError as exc:
        parser.error(str(exc))

    # Refuse before spending one backtest on a sweep whose verdict cannot be
    # computed: the center has to be scored, and it needs two neighbors.
    if args.center not in grid:
        parser.error(
            f"center {args.center} is not one of the grid points {grid}; the "
            "verdict is a claim about the shipped value, so it must be measured"
        )
    if len(grid) < 3:
        parser.error(
            f"grid {grid} gives {len(grid) - 1} neighbor(s) around the center; "
            "a stability verdict needs at least 2"
        )

    membership = (
        load_membership_calendar(args.universe_snapshots)
        if args.universe_snapshots
        else None
    )
    eligible_tickers = spec.eligible_tickers(membership)
    bars_by_ticker = load_bars(args.bars_from_json, eligible_tickers)
    if not bars_by_ticker:
        parser.error(
            f"no bars for the {args.sleeve} universe in {args.bars_from_json}"
        )

    capital = args.capital * spec.capital_fraction
    cost_model = build_cost_model()

    if membership is None:
        # Same warning run_backtest prints, for the same reason: a surface
        # traced over present-day survivors is a surface for a strategy that
        # could not have been traded, so its plateau is not evidence either.
        print(
            "  Universe: STATIC present-day ticker list — SURVIVORSHIP BIASED. "
            "Every point on this surface is inflated. Pass "
            "--universe-snapshots for a sweep you can act on."
        )

    print(
        f"Sweeping {args.sleeve}.{args.parameter} over {grid} "
        f"(center {args.center}) on {len(bars_by_ticker)} tickers, "
        f"${capital:,.0f} sleeve capital"
    )
    runs: list[dict] = []
    for value in grid:
        run = run_grid_point(
            spec,
            parameter=args.parameter,
            value=value,
            bars_by_ticker=bars_by_ticker,
            eligible_tickers=eligible_tickers,
            capital=capital,
            cost_model=cost_model,
            membership=membership,
        )
        if args.metric not in run:
            parser.error(
                f"metric {args.metric!r} is not produced by the backtest; "
                f"available: {', '.join(k for k in run if k != 'value')}"
            )
        runs.append(run)
        marker = " <- center" if value == args.center else ""
        print(
            f"  {args.parameter}={value:g}: {args.metric}="
            f"{run[args.metric]:.4f} ({run['total_trades']} trades){marker}"
        )

    results = {run["value"]: float(run[args.metric]) for run in runs}
    report = parameter_stability(
        results,
        center=args.center,
        parameter=args.parameter,
        plateau_tolerance=args.plateau_tolerance,
    )

    artifact = {
        "sleeve": args.sleeve,
        "parameter": args.parameter,
        "center": args.center,
        "metric": args.metric,
        "capital": capital,
        "bars_source": args.bars_from_json,
        # Absence has to stay visible: a reader cannot tell a survivorship-free
        # surface from a biased one unless the run says which it is.
        "point_in_time_universe": membership is not None,
        "tickers": sorted(bars_by_ticker),
        # String keys because JSON objects have no float keys. Readers rebuild
        # the mapping with ``{float(k): v for k, v in results.items()}``.
        "results": {f"{value}": metric for value, metric in results.items()},
        "runs": runs,
        "stability": asdict(report),
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(artifact, indent=2))

    verdict = "PLATEAU" if report.is_plateau else "KNIFE EDGE"
    print(f"\n{verdict}: {report.verdict_reason}")
    print(f"Wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
