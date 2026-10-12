"""Guards on the KAN-105 decision: the shadow's window opens on the warm book.

docs/decisions/shadow-seeding-2026-10.md states what the replay does: one
global warm-up of 160 sessions, signals gated before it, the curve (not the
book) normalised, no epoch clamp, a truncated warm-up raised, and a seeding
version in the fingerprint. Each claim is pinned against the CODE, because a
decision record that drifts from what the system does reads as evidence.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import backtest.shadow_series as shadow_series
from backtest.shadow_artifact import SHADOW_SEEDING_VERSION
from backtest.shadow_series import (
    SHADOW_LOOKBACK_SESSIONS,
    SHADOW_WARMUP_SESSIONS,
    ShadowWarmupTruncated,
    replay_window_detail,
)
from shared.config import load_config

ROOT = Path(__file__).resolve().parents[2]
DECISION = ROOT / "docs/decisions/shadow-seeding-2026-10.md"
ASSETS = ROOT / "docs/decisions/assets/kan-105"


def _prose(path: Path) -> str:
    """Text with whitespace runs collapsed, so a reflow cannot break a check."""
    return " ".join(path.read_text().split())


def test_the_decision_names_the_warm_up_the_code_and_config_use() -> None:
    text = _prose(DECISION)
    assert SHADOW_WARMUP_SESSIONS == 160
    assert "N = 160" in text
    assert "divergence.shadow_warmup_sessions: 160" in text
    config = load_config(str(ROOT / "config/default.yaml"))
    assert config.divergence.shadow_warmup_sessions == SHADOW_WARMUP_SESSIONS


def test_the_decision_names_the_fingerprinted_seeding_version() -> None:
    assert f"`{SHADOW_SEEDING_VERSION}`" in _prose(DECISION)


def test_the_fetch_arithmetic_matches_the_lookback() -> None:
    assert SHADOW_LOOKBACK_SESSIONS == 200
    assert "30 + N + 200" in _prose(DECISION)


def test_there_is_no_epoch_clamp_in_the_replay() -> None:
    """The operator declined it; the replay must not take an epoch or a
    live-history boundary that could clamp ``warm_from``."""
    assert "No epoch clamp" in _prose(DECISION)
    params = set(inspect.signature(replay_window_detail).parameters)
    assert not {p for p in params if "epoch" in p or "history_from" in p}


def test_a_truncated_warm_up_raises_as_the_decision_says() -> None:
    assert "raise `ShadowWarmupTruncated`" in _prose(DECISION)
    assert issubclass(ShadowWarmupTruncated, ValueError)


def test_the_module_no_longer_claims_warm_up_bars_never_trade() -> None:
    """The pre-KAN-105 docstring said bars before the window "never trade";
    under the warm-up they do, before the window."""
    doc = " ".join((shadow_series.__doc__ or "").split())
    assert "but never trade" not in doc
    assert "never called" in doc


def test_the_re_grade_harness_ships_with_the_decision() -> None:
    for name in ("extract_inputs.py", "regrade.py"):
        assert (ASSETS / name).is_file(), name
        assert f"assets/kan-105/{name}" in _prose(DECISION)
