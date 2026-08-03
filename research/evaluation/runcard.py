# research/evaluation/runcard.py
from __future__ import annotations

import json
from pathlib import Path


def build_run_card(evaluation: dict, git_revision: str, input_checksum: str) -> dict:
    return {
        "git_revision": git_revision,
        "input_artifact_checksum": input_checksum,
        "snapshot_identity": evaluation["snapshot_identity"],
        "provenance": evaluation["provenance"],
        "config": evaluation["config"],
        "evaluation": evaluation,
    }


def write_run_card(card: dict, output_dir: str) -> Path:
    directory = Path(output_dir)
    if "output" in directory.parts:
        raise ValueError("run cards must not be written under output/")
    directory.mkdir(parents=True, exist_ok=True)
    cutoff = card["provenance"]["data_cutoff"]
    path = directory / f"factor_evaluation_{cutoff}.json"
    path.write_text(json.dumps(card, sort_keys=True, indent=2))
    return path
