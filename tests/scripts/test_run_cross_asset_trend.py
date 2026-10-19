"""The cross-asset trend driver's protocol pins (docs/decisions/cross-asset-trend-2026-10.md)."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from scripts import run_cross_asset_trend as driver

INCUMBENTS = (
    "momentum", "sector_rotation", "thematic_momentum",
    "quality_value", "earnings_drift", "tail_risk_hedge",
)


def test_the_protocol_constants_are_the_pre_registered_ones():
    assert driver.N_TRIALS == 13
    assert driver.TRADE_START.isoformat() == "2016-10-17"
    assert driver.WINDOW_END.isoformat() == "2026-10-12"


def test_the_drawdown_profile_finds_depth_peak_and_recovery():
    dates = [f"2020-01-{d:02d}" for d in range(1, 8)]
    returns = [0.10, -0.20, 0.0, 0.10, 0.20, 0.0, -0.01]
    profile = driver.drawdown_profile(returns, dates)
    assert profile["max_drawdown"] == pytest.approx(-0.20)
    assert (profile["peak"], profile["trough"]) == ("2020-01-01", "2020-01-02")
    assert profile["recovered"] == "2020-01-05"
    assert profile["sessions_peak_to_recovery"] == 4
    assert profile["longest_underwater_sessions"] == 3


def _series(seed: int, mean: float, n: int = 300) -> list[float]:
    return list(np.random.default_rng(seed).normal(mean, 0.01, n))


def test_the_verdict_deflates_against_every_candidate_and_the_lenient_figure_drops_only_the_hedge(monkeypatch):
    dates = [d.date().isoformat() for d in pd.bdate_range("2020-01-01", periods=300)]
    incumbents = {"sleeves": {
        name: {"dates": dates, "returns": _series(i, 0.0005)}
        for i, name in enumerate(INCUMBENTS)
    }}
    replays = {
        rule: {"rule": rule, "dates": dates, "returns": _series(10 + i, 0.001)}
        for i, rule in enumerate(("sma", "tsmom"))
    }
    seen: list[tuple[list[str], int]] = []
    real_control = driver.control

    def spy(per_factor, **kwargs):
        seen.append((sorted(per_factor), kwargs["n_trials"]))
        return real_control(per_factor, **kwargs)

    monkeypatch.setattr(driver, "control", spy)
    result = driver.evaluate(replays, incumbents, market=None)

    everyone = sorted(INCUMBENTS + (driver.CANDIDATE, driver.SECOND_TRIAL))
    assert seen[0] == (everyone, 13)
    assert seen[1] == (sorted(set(everyone) - {"tail_risk_hedge"}), 13)
    assert set(result["primary"]) == set(everyone)
    assert result["primary"][driver.CANDIDATE]["verdict"] in {"PASS", "FAIL"}
