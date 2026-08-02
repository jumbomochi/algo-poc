# scripts/run_factor_evaluation.py
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import date
from pathlib import Path

from research.evaluation.evaluator import EvaluationConfig, evaluate_factors
from research.evaluation.runcard import build_run_card, write_run_card


def _load_bars(path: str) -> dict:
    payload = json.loads(Path(path).read_text())
    bars = payload.get("bars")
    if not bars:
        raise ValueError(f"no bars found in {path}")
    return {
        ticker: [
            {**bar, "date": date.fromisoformat(bar["date"]) if isinstance(bar["date"], str) else bar["date"]}
            for bar in rows
        ]
        for ticker, rows in bars.items()
    }


def _git_revision() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    except Exception:
        return "unknown"


def _checksum(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline factor evaluation run card generator")
    parser.add_argument("--bars-from-json", required=True)
    parser.add_argument("--shadow-from-json", default=None)
    parser.add_argument("--horizon", type=int, default=21)
    parser.add_argument("--outer-folds", type=int, default=4)
    parser.add_argument("--inner-folds", type=int, default=3)
    parser.add_argument("--embargo", type=int, default=None)
    parser.add_argument("--quantiles", type=float, nargs="+", default=[0.2, 0.3])
    parser.add_argument("--fdr-q", type=float, default=0.10)
    parser.add_argument("--min-names", type=int, default=5)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)

    bars = _load_bars(args.bars_from_json)
    baseline_records = None
    if args.shadow_from_json:
        baseline_records = json.loads(Path(args.shadow_from_json).read_text())

    config = EvaluationConfig(
        horizon=args.horizon, n_outer=args.outer_folds, n_inner=args.inner_folds,
        embargo=args.embargo if args.embargo is not None else args.horizon,
        quantiles=tuple(args.quantiles), fdr_q=args.fdr_q, min_names=args.min_names, seed=args.seed,
    )
    evaluation = evaluate_factors(bars, baseline_records=baseline_records, config=config)
    card = build_run_card(evaluation, _git_revision(), _checksum(args.bars_from_json))
    path = write_run_card(card, args.output_dir)
    print(f"wrote factor evaluation run card: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
