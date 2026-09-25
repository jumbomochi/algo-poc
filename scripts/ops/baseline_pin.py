#!/usr/bin/env python3
"""Resolve the pinned divergence baseline for the launchd wrappers (KAN-51).

The baseline of record is a configuration fact — `divergence.baseline_pin` in
`config/default.yaml` — not whatever `output/backtest_multi_*.json` happens to
sort last. Both wrappers that care are bash, so this is the one seam that turns
the config value into an absolute path a shell can use:

* `deploy/launchd/run_divergence.sh` passes it as `--backtest <pin> --pinned`;
* `deploy/launchd/run_backtest_refresh.sh` excludes it from the 90-day prune, so
  a refresh cannot delete the artifact the gate evidence is measured against.

Deliberately quiet on failure: nothing on stdout and exit 1. Deciding that an
unresolvable pin should stop the run belongs to exactly one place, and that
place is the monitor — it exits 3 with `BASELINE_PIN_MISSING`, which alerts.
If this script also refused, the wrapper would have two authorities that could
disagree about whether the 04:45 job happens at all, and the failure mode of
the quieter one is a job that skips silently. That is the 2026-08-13 pattern.

Usage:
    python scripts/ops/baseline_pin.py
    ALGO_BASELINE_PIN=/path/to/backtest_multi_x.json python scripts/ops/baseline_pin.py
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Executed by path from the launchd wrappers (`$VENV scripts/ops/baseline_pin.py`),
# which puts `scripts/ops/` — not the repo root — on sys.path. Pin the repo root
# explicitly so `shared.config` resolves to THIS checkout rather than to
# whatever tree an editable install happens to point at.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from shared.config import load_config  # noqa: E402

DEFAULT_CONFIG = "config/default.yaml"

#: Overrides the config pin. Exists so the wrapper tests can drive a stub tree,
#: and so an operator can score one ad-hoc run against a different artifact
#: without editing the committed pin. Never export it in a login shell: the
#: nightly job would then judge against whatever it points at.
ENV_OVERRIDE = "ALGO_BASELINE_PIN"

#: The rung-0 pin's own override, so redirecting one pin can never move the other.
RUNG0_ENV_OVERRIDE = "ALGO_RUNG0_BASELINE_PIN"


def resolve_pin(config_path: str = DEFAULT_CONFIG, *, rung0: bool = False) -> str | None:
    """Return the pinned baseline as an absolute path, or None if unpinned.

    Absolute because the callers need it that way: the refresh wrapper compares
    with ``find ... ! -samefile "$PIN"``, and a relative path would match only
    by accident of where the job happened to be standing. A relative config
    value resolves against the working directory — the wrappers cd to the
    deployed checkout before asking, which is what makes `output/...` mean the
    deployed tree's `output/` and not the repo the config was read from.

    Never raises. The refresh wrapper runs this from a scratch tree that may
    carry no config at all, and a traceback on stdout would be substituted into
    ``--backtest`` as though it were a path.

    ``rung0`` selects ``divergence.rung0_baseline_pin`` (KAN-60) and its own
    override. An unset rung-0 pin is None, never the edge pin.
    """
    env_name = RUNG0_ENV_OVERRIDE if rung0 else ENV_OVERRIDE
    override = os.environ.get(env_name)
    if override and override.strip():
        return str(Path(override.strip()).absolute())

    try:
        divergence = load_config(config_path).divergence
    except Exception as exc:  # noqa: BLE001 - stdout must stay a path or empty
        print(f"baseline_pin: could not read {config_path}: {exc}", file=sys.stderr)
        return None

    pin = divergence.rung0_baseline_pin if rung0 else divergence.baseline_pin
    if not pin or not pin.strip():
        return None
    return str(Path(pin.strip()).absolute())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Print the pinned divergence baseline path, if one is configured."
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--rung0", action="store_true", help="Resolve divergence.rung0_baseline_pin (KAN-60) instead.")
    args = parser.parse_args(argv)

    pin = resolve_pin(args.config, rung0=args.rung0)
    if pin is None:
        key = "divergence.rung0_baseline_pin" if args.rung0 else "divergence.baseline_pin"
        env = RUNG0_ENV_OVERRIDE if args.rung0 else ENV_OVERRIDE
        print(
            f"baseline_pin: no {key} configured "
            f"in {args.config} and no {env} set",
            file=sys.stderr,
        )
        return 1
    print(pin)
    return 0


if __name__ == "__main__":
    sys.exit(main())
