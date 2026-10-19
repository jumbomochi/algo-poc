#!/usr/bin/env python3
"""Replay and judge the cross-asset trend sleeve (research only).

Implements the protocol pre-registered in
docs/decisions/cross-asset-trend-2026-10.md and nothing else: the candidate
(``sma``) and the declared second trial (``tsmom``) are replayed through the
same BacktestRunner, cost model and next-open fills as every sleeve. With
``--incumbents`` (a sleeve-returns mapping written by
run_sleeve_evaluation.py), it then scores D10 against the six incumbents.

    PYTHONPATH=. python scripts/run_cross_asset_trend.py \\
        --bars bars_trades.json --incumbents sleeve_returns.json \\
        --market-ticker SPY --output out.json

``--bars`` is ``{ticker: [{date, open, high, low, close, volume}]}``, as
``fetch_bars_from_ib`` returns it with ISO dates. Nothing here touches paper,
live, the database or a holdout split.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.metrics import BacktestMetrics
from backtest.portfolio_context import RunnerBook
from backtest.runner import BacktestRunner
from backtest.simulator import SimulatedExecutor
from research.evaluation.metrics import probabilistic_sharpe, sharpe_stats
from research.evaluation.multiple_testing import control
from scripts.run_backtest import (
    CROSS_ASSET_ETFS,
    CROSS_ASSET_RULES,
    SkipLedger,
    build_cost_model,
    make_cross_asset_trend_signals_fn,
)
from services.risk_management.engine import RiskEngine

#: The pre-registered window: the KAN-120 record's sessions.
TRADE_START = date(2016, 10, 17)
WINDOW_END = date(2026, 10, 12)
#: Every sleeve-level trial on record (the decision doc, "Search size").
N_TRIALS = 13
CANDIDATE = "cross_asset_trend"
SECOND_TRIAL = "cross_asset_tsmom"
NAMES = {"sma": CANDIDATE, "tsmom": SECOND_TRIAL}
INSURANCE_SLEEVE = "tail_risk_hedge"
ANNUALIZATION = 252.0


def load_bars(path: str) -> dict[str, list[dict]]:
    raw = json.loads(Path(path).read_text())
    out: dict[str, list[dict]] = {}
    for ticker in raw:
        bars = [{**b, "date": date.fromisoformat(b["date"][:10])} for b in raw[ticker]]
        out[ticker] = [b for b in bars if b["date"] <= WINDOW_END]
    return out


def replay(
    bars_by_ticker: dict[str, list[dict]],
    rule: str,
    capital: float,
    whole_shares: bool,
) -> dict:
    """One replay of one rule; returns aligned to sessions from TRADE_START."""
    bars = {t: bars_by_ticker[t] for t in CROSS_ASSET_ETFS}
    book = RunnerBook()
    ledger = SkipLedger()
    signals_fn = make_cross_asset_trend_signals_fn(
        bars_by_ticker=bars,
        eligible_tickers=list(CROSS_ASSET_ETFS),
        rule=rule,
        initial_capital=capital,
        portfolio_context=book,
        whole_shares=whole_shares,
        skip_ledger=ledger,
    )
    runner = BacktestRunner(
        executor=SimulatedExecutor(build_cost_model()),
        initial_capital=capital,
        whole_shares=whole_shares,
        skip_ledger=ledger,
    )
    entries_signalled = 0

    def counting_signals_fn(ticker: str, bars_so_far: list[dict]) -> dict | None:
        nonlocal entries_signalled
        signal = signals_fn(ticker, bars_so_far)
        if signal and signal["action"] == "buy" and bars_so_far[-1]["date"] >= TRADE_START:
            entries_signalled += 1
        return signal

    result = runner.run(
        bars,
        counting_signals_fn,
        RiskEngine(
            position_entry_limit_pct=20.0,
            sector_concentration_pct=100.0,
            total_exposure_limit_pct=150.0,
            max_lots_per_ticker=1,
        ),
        trade_start_date=TRADE_START,
        portfolio_name=NAMES[rule],
        book=book,
    )
    window = window_from_result(result.portfolio_values, list(result.dates))
    trades = result.trades
    opened = len(trades) + len(result.open_positions)
    return {
        "rule": rule,
        "dates": window["dates"],
        "returns": window["returns"],
        "metrics": BacktestMetrics.compute(window["values"], trades),
        "round_trips": len(trades),
        # Closed round trips both ways, plus the entry leg of lots still open.
        "commission": float(
            sum(t.get("entry_commission", 0.0) + t.get("exit_commission", 0.0) for t in trades)
            + sum(p.get("entry_commission", 0.0) for p in result.open_positions)
        ),
        "open_positions": result.open_positions,
        # The entry funnel. Entries are DAY limits at the decision close (the
        # house fill model), so a signal can expire unfilled and leave its slot
        # in cash until the next month's decision.
        "entries_signalled_in_window": entries_signalled,
        "entries_filled": opened,
        "entries_below_one_share": ledger.to_dict()["count"],
    }


def window_from_result(values: list[float], dates: list) -> dict:
    """Slice a runner curve to sessions from TRADE_START, returns aligned.

    ``values`` is one longer than ``dates``: ``values[0]`` is the initial
    capital and ``values[i + 1]`` the close of ``dates[i]``. The window's
    curve starts from the close before TRADE_START.
    """
    if len(values) != len(dates) + 1:
        raise ValueError(f"{len(values)} values for {len(dates)} dates; expected one more")
    keep = [i for i, d in enumerate(dates) if TRADE_START <= d <= WINDOW_END]
    return {
        "dates": [dates[i].isoformat() for i in keep],
        "returns": [values[i + 1] / values[i] - 1.0 for i in keep],
        "values": [values[keep[0]]] + [values[i + 1] for i in keep],
    }


def _row(returns: pd.Series) -> dict:
    stats = sharpe_stats(returns)
    psr = probabilistic_sharpe(stats["sr"], stats["n"], stats["skew"], stats["kurt"], 0.0)
    return {**stats, "psr": psr, "ic_p": 1.0 - psr}


def _annual_sharpe(r: np.ndarray) -> float:
    sd = r.std(ddof=1)
    return float(r.mean() / sd * math.sqrt(ANNUALIZATION)) if sd > 0 else 0.0


def drawdown_profile(returns: list[float], dates: list[str]) -> dict:
    """Depth and timing of the worst decline, measured from the starting capital."""
    equity = np.concatenate([[1.0], np.cumprod(1.0 + np.asarray(returns))])
    labels = ["start"] + list(dates)
    peak = np.maximum.accumulate(equity)
    under = equity / peak - 1.0
    trough = int(under.argmin())
    peak_at = int(np.argmax(equity[: trough + 1]))
    recovered = next((k for k in range(trough, len(equity)) if equity[k] >= equity[peak_at]), None)
    longest = run = 0
    for x in under:
        run = run + 1 if x < 0 else 0
        longest = max(longest, run)
    return {
        "max_drawdown": float(under.min()),
        "peak": labels[peak_at],
        "trough": labels[trough],
        "recovered": labels[recovered] if recovered is not None else None,
        "sessions_peak_to_recovery": (recovered - peak_at) if recovered is not None else None,
        "longest_underwater_sessions": longest,
    }


def evaluate(replays: dict[str, dict], incumbents: dict, market: pd.Series | None) -> dict:
    """D10 as pre-registered, plus the reported-not-verdict figures."""
    sleeves = incumbents["sleeves"]
    series = {
        name: pd.Series(row["returns"], index=pd.to_datetime(row["dates"]), dtype=float)
        for name, row in sleeves.items()
    }
    for rep in replays.values():
        series[NAMES[rep["rule"]]] = pd.Series(
            rep["returns"], index=pd.to_datetime(rep["dates"]), dtype=float
        )
    rows = {name: _row(s) for name, s in series.items()}

    def verdicts(names: list[str]) -> dict:
        v = control({n: dict(rows[n]) for n in names}, q=0.10, dsr_threshold=0.95, n_trials=N_TRIALS)
        return {
            n: {"dsr": x.deflated_sharpe, "passes_dsr": x.passes_dsr,
                "passes_fdr": x.passes_fdr, "verdict": "PASS" if x.survives else "FAIL"}
            for n, x in v.items()
        }

    everyone = list(sleeves) + [CANDIDATE, SECOND_TRIAL]
    primary = verdicts(everyone)
    without_insurance = verdicts([n for n in everyone if n != INSURANCE_SLEEVE])

    frame = pd.DataFrame(series).dropna()
    cand = frame[CANDIDATE].to_numpy()
    diversification = {
        "aligned_sessions": int(len(frame)),
        "correlation": {n: float(np.corrcoef(cand, frame[n])[0, 1]) for n in sleeves},
        "blend_sharpe": {
            "candidate+momentum": _annual_sharpe(frame[[CANDIDATE, "momentum"]].mean(axis=1).to_numpy()),
            "candidate+five_return_seeking": _annual_sharpe(
                frame[[CANDIDATE] + [n for n in sleeves if n != INSURANCE_SLEEVE]].mean(axis=1).to_numpy()
            ),
            "five_return_seeking_alone": _annual_sharpe(
                frame[[n for n in sleeves if n != INSURANCE_SLEEVE]].mean(axis=1).to_numpy()
            ),
        },
    }
    if market is not None:
        m = market.reindex(frame.index).fillna(0.0).to_numpy()
        beta = float(np.cov(cand, m)[0, 1] / m.var(ddof=1))
        alpha = cand - beta * m
        diversification["market"] = {
            "correlation": float(np.corrcoef(cand, m)[0, 1]),
            "beta": beta,
            "alpha_pct_per_year": float(alpha.mean() * ANNUALIZATION * 100),
            "alpha_information_ratio": _annual_sharpe(alpha),
            "market_sharpe": _annual_sharpe(m),
        }
    return {
        "n_trials": N_TRIALS,
        "primary": primary,
        "reported_not_verdict": {"without_insurance_sleeve_in_spread": without_insurance},
        "per_candidate_stats": {n: rows[n] for n in (CANDIDATE, SECOND_TRIAL)},
        "diversification": diversification,
    }


def market_returns(bars_by_ticker: dict[str, list[dict]], ticker: str) -> pd.Series:
    bars = bars_by_ticker[ticker]
    closes = pd.Series([b["close"] for b in bars], index=pd.to_datetime([b["date"] for b in bars]))
    return closes.pct_change().dropna()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bars", required=True)
    parser.add_argument("--capital", type=float, default=100_000)
    parser.add_argument("--whole-shares", action="store_true")
    parser.add_argument("--incumbents", default=None,
                        help="sleeve-returns mapping (run_sleeve_evaluation.py) to judge D10 against")
    parser.add_argument("--market-ticker", default=None,
                        help="a ticker in --bars to measure beta against (e.g. SPY)")
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    bars = load_bars(args.bars)
    missing = [t for t in CROSS_ASSET_ETFS if t not in bars]
    if missing:
        parser.error(f"--bars lacks {missing}")
    replays = {rule: replay(bars, rule, args.capital, args.whole_shares) for rule in CROSS_ASSET_RULES}
    out: dict = {
        "protocol": "docs/decisions/cross-asset-trend-2026-10.md",
        "bars": args.bars,
        "capital": args.capital,
        "whole_shares": args.whole_shares,
        "window": [TRADE_START.isoformat(), WINDOW_END.isoformat()],
        "replays": {},
    }
    for rule, rep in replays.items():
        rep["drawdown"] = drawdown_profile(rep["returns"], rep["dates"])
        out["replays"][NAMES[rule]] = rep
        m = rep["metrics"]
        print(
            f"{NAMES[rule]:<18} sharpe {_annual_sharpe(np.asarray(rep['returns'])):.3f}  "
            f"return {m.get('total_return', float('nan')):.1%}  maxDD {rep['drawdown']['max_drawdown']:.1%}  "
            f"round trips {rep['round_trips']}  commission ${rep['commission']:,.0f}"
        )
    if args.incumbents:
        market = market_returns(bars, args.market_ticker) if args.market_ticker else None
        incumbents = json.loads(Path(args.incumbents).read_text())
        out["evaluation"] = evaluate(replays, incumbents, market)
        ev = out["evaluation"]
        for name in (CANDIDATE, SECOND_TRIAL):
            p, alt = ev["primary"][name], ev["reported_not_verdict"]["without_insurance_sleeve_in_spread"][name]
            print(f"{name:<18} DSR {p['dsr']:.3f} (n={N_TRIALS}) {p['verdict']}   "
                  f"[without the insurance sleeve in the spread: {alt['dsr']:.3f}]")
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
