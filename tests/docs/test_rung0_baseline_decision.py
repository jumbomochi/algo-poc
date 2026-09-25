"""KAN-60 / D21: the Rung-0 baseline of record and its accepted coverage bias.

Same guards as D20 (tests/docs/test_pit_coverage_decision.py): the registry is
what admits a run, the prose is what a human reads, and they must not drift.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from backtest.bias_acceptance import (
    ADMISSIBLE_WITH_ACCEPTED_BIAS,
    REQUIREMENT_COVERAGE_FLOOR,
    load_acceptances,
    resolve_admissibility,
)

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "research/bias_acceptances.json"
DIRECTION = ROOT / "docs/designs/project-direction.md"
BASELINE_DOC = ROOT / "docs/operations/backtest-baseline.md"
ECONOMICS = ROOT / "docs/operations/rung0-economics.md"

# Measured on the generated artifact (2026-09-25); output/ is gitignored, so CI
# cannot re-derive these and they are pinned here instead.
D21_ARTIFACT = "output/baselines/rung0_momentum_20260925.json"
D21_SHA256 = "f1f3a4ffec895c22d4baf827ca99c0aac488a0026e68c0b5b301ee8691e6d3a8"
D21_EXCLUDED_PCT = "11.21"


def _raw(decision: str) -> dict:
    rows = [e for e in json.loads(REGISTRY.read_text())["acceptances"]
            if e["decision"] == decision]
    assert len(rows) == 1, (decision, len(rows))
    return rows[0]


def _d21():
    rows = [a for a in load_acceptances(REGISTRY) if a.decision == "D21"]
    assert len(rows) == 1, "exactly one D21 acceptance, or its figures are ambiguous"
    return rows[0]


def test_d21_accepts_the_coverage_floor_for_one_artifact() -> None:
    entry = _d21()
    assert entry.requirement == REQUIREMENT_COVERAGE_FLOOR
    assert entry.source_sha256 == D21_SHA256
    assert _raw("D21")["source"] == D21_ARTIFACT
    assert entry.floor_pct == 5.0
    assert f"{entry.excluded_pct:.2f}" == D21_EXCLUDED_PCT


def test_d21_keeps_the_d18_re_evidence_date() -> None:
    assert "2029-08-18" in _d21().re_evidence


def test_d21_does_not_disturb_d18_or_d20() -> None:
    shas = {_raw(d)["source_sha256"] for d in ("D18", "D20", "D21")}
    assert len(shas) == 3


def test_the_rung0_pin_names_the_d21_artifact() -> None:
    config = (ROOT / "config/default.yaml").read_text()
    pinned = [ln.split(":", 1)[1].strip() for ln in config.splitlines()
              if ln.strip().startswith("rung0_baseline_pin:")]
    assert pinned == [D21_ARTIFACT]


def test_the_rung0_artifact_is_outside_every_refresh_glob() -> None:
    assert not Path(D21_ARTIFACT).name.startswith("backtest_multi_")


def test_the_prose_carries_the_decision() -> None:
    direction = DIRECTION.read_text()
    assert "(D21)" in direction and f"{D21_EXCLUDED_PCT}%" in direction
    assert "KAN-60" in direction
    assert Path(D21_ARTIFACT).name in BASELINE_DOC.read_text()
    assert Path(D21_ARTIFACT).name in ECONOMICS.read_text()


def test_the_on_disk_artifact_resolves_with_the_accepted_bias() -> None:
    """Skipped where output/ is absent (CI), like the D18/D20 on-disk checks."""
    import hashlib

    from backtest.divergence import execution_model_from_backtest_config

    path = ROOT / D21_ARTIFACT
    if not path.exists():
        pytest.skip(f"{D21_ARTIFACT} not present in this checkout")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    config = json.loads(path.read_text())["config"]
    assert config["portfolios"] == {"momentum": pytest.approx(3700.0)}
    assert config["whole_shares"] is True
    verdict = resolve_admissibility(
        execution_model_from_backtest_config(config),
        source_sha256=digest.hexdigest(),
        coverage=config["coverage"],
        acceptances=load_acceptances(REGISTRY),
    )
    assert verdict.state == ADMISSIBLE_WITH_ACCEPTED_BIAS, verdict.notes
    assert verdict.accepted_bias["decision"] == "D21"
