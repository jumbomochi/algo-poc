"""KAN-116: D10 statistics for saved multi-portfolio artifacts, as a REHEARSAL.

    PYTHONPATH=<repo> python d10_rehearsal.py <label>=<backtest_multi.json> [...]

Uses scripts/run_sleeve_evaluation.py's own build_mapping/evaluate_mapping, but
against a scratch copy of research/holdout_registry.json (written next to each
result, in the current directory) with the incumbent_sleeves_2026 burn removed,
so the already-spent split can be re-resolved for comparison. Nothing is spent
and nothing in the repo is written. Also reports SR* and each sleeve's DSR
against a reference run's SR* (``--ref <label>``), which isolates a sleeve's
own change from the cross-sleeve spread the SR* estimator depends on.
"""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]


def _scratch_registry(label: str) -> Path:
    registry = copy.deepcopy(json.loads((REPO / "research/holdout_registry.json").read_text()))
    registry["evaluations"] = [
        e for e in registry.get("evaluations", [])
        if e.get("split_id") != "incumbent_sleeves_2026"
    ]
    path = Path(f"holdout_registry_scratch_{label}.json")
    path.write_text(json.dumps(registry, indent=1))
    return path


def evaluate(label: str, artifact: str, stability_dir: str | None) -> dict:
    from research.evaluation.multiple_testing import expected_max_sharpe
    from scripts.run_sleeve_evaluation import build_mapping, evaluate_mapping, sleeve_trial_count

    mapping = build_mapping(json.loads(Path(artifact).read_text()), artifact, acceptances=[])
    ev = evaluate_mapping(mapping, n_trials=sleeve_trial_count(),
                          holdout_registry_path=_scratch_registry(label),
                          stability_dir=stability_dir)
    sleeves = {
        name: {
            **{k: s.get(k) for k in ("sharpe", "sr_per_period", "n_observations", "skew", "kurtosis",
                                     "probabilistic_sharpe", "deflated_sharpe", "passes_fdr", "verdict")},
            "holdout_sharpe": (s.get("holdout") or {}).get("sharpe"),
        }
        for name, s in ev["sleeves"].items()
    }
    return {"source": artifact, "holdout": ev["holdout"], "sleeves": sleeves,
            "sr_star": expected_max_sharpe([s["sr_per_period"] for s in sleeves.values()], ev["n_trials"])}


def main() -> None:
    from research.evaluation.multiple_testing import probabilistic_sharpe

    parser = argparse.ArgumentParser()
    parser.add_argument("runs", nargs="+", help="<label>=<artifact.json>")
    parser.add_argument("--ref", help="label whose SR* the other runs are also deflated against")
    parser.add_argument("--stability-dir", default=None)
    args = parser.parse_args()
    results = {label: evaluate(label, path, args.stability_dir)
               for label, path in (r.split("=", 1) for r in args.runs)}
    ref = results[args.ref]["sr_star"] if args.ref else None
    for label, res in results.items():
        print(f"== {label}  SR*/period {res['sr_star']:.4f}  holdout "
              f"{res['holdout']['first_session']}..{res['holdout']['last_session']} n={res['holdout']['n_sessions']}")
        for name, s in res["sleeves"].items():
            at_ref = (probabilistic_sharpe(s["sr_per_period"], s["n_observations"], s["skew"],
                                           s["kurtosis"], ref) if ref is not None else None)
            s["dsr_at_ref_sr_star"] = at_ref
            print(f"  {name:18s} SR {s['sharpe']:6.3f} PSR {s['probabilistic_sharpe']:.3f} "
                  f"DSR {s['deflated_sharpe']:.3f}" + (f" (at ref SR* {at_ref:.3f})" if at_ref is not None else "")
                  + f" FDR {'Y' if s['passes_fdr'] else 'N'} -> {s['verdict']}")
        Path(f"d10_rehearsal_{label}.json").write_text(json.dumps(res, indent=1, default=str))


if __name__ == "__main__":
    main()
