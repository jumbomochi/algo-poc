#!/usr/bin/env python3
"""Artifacts the weekly prune must never delete, one absolute path per line.

``deploy/launchd/run_backtest_refresh.sh`` deletes ``output/backtest_multi_*.json``
older than 90 days. Its only exclusion used to be ``divergence.baseline_pin``,
which incidentally covered the D18 evidence artifact because the pin and
``research/bias_acceptances.json`` happened to name the same file. Nothing
enforced that alignment, and there are two ways to break it:

1. a re-pin moves the pin to a fresher baseline;
2. someone deletes the pin as dead config, since the divergence monitor stopped
   reading it when the feed became the rolling shadow, leaving the prune as its
   only consumer.

Either way the accepted artifact ages past 90 days and goes. It cannot be
rebuilt: the holdout was spent, and a re-run today prices a different set of
bars, so its sha256 could never match the acceptance again — the acceptance is
pinned by that sha precisely so re-accepting is a deliberate act.

So protection follows the *decision* rather than a second hand-maintained list.
An entry in the registry is what makes an artifact evidence, and this reads the
same file.

Exits 1 on an unreadable registry, printing nothing. The caller treats that as
"protect everything" and skips the prune — disk is cheap and the evidence is
not reproducible. A MISSING registry is not an error: a repo with no accepted
biases has nothing extra to protect, and refusing to prune there would grow
``output/`` forever.

Usage:
    python scripts/ops/protected_artifacts.py [--repo-root DIR]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

REGISTRY_RELATIVE = "research/bias_acceptances.json"


def protected_paths(repo_root: Path) -> list[Path]:
    """Absolute paths of every artifact the acceptance registry names.

    Raises:
        ValueError: The registry exists but cannot be parsed. Refusing loudly
            is the point: a silently-empty list would read as "nothing to
            protect" and hand the prune the evidence.
    """
    registry = repo_root / REGISTRY_RELATIVE
    if not registry.is_file():
        return []

    try:
        payload = json.loads(registry.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{REGISTRY_RELATIVE} is unreadable: {exc}") from exc

    paths: list[Path] = []
    for entry in payload.get("acceptances", []):
        source = (entry or {}).get("source")
        if not source:
            continue
        candidate = Path(source)
        if not candidate.is_absolute():
            candidate = repo_root / candidate
        paths.append(candidate)
    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=str(_REPO_ROOT))
    args = parser.parse_args(argv)

    try:
        paths = protected_paths(Path(args.repo_root))
    except ValueError as exc:
        print(f"protected_artifacts: {exc}", file=sys.stderr)
        return 1

    for path in paths:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
