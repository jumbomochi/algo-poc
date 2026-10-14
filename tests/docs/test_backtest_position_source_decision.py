"""Guards on the KAN-116 decision: replays read their own fills.

docs/decisions/backtest-position-source-2026-10.md states what the code does —
the replay's position source, the fingerprint version, the new baseline_id —
and quotes the measurements in docs/decisions/assets/kan-116/results.json.
Each claim is pinned against the CODE or the results file, because a decision
record that drifts from the system, or from its own evidence, reads as
evidence anyway.
"""
from __future__ import annotations

import inspect
import json
import re
from pathlib import Path

import pytest

import scripts.run_backtest as run_backtest
from backtest.portfolio_context import RunnerBook
from backtest.runner import BacktestRunner
from backtest.shadow_artifact import SHADOW_POSITION_SOURCE_VERSION
from backtest.shadow_series import replay_window_detail

ROOT = Path(__file__).resolve().parents[2]
DECISION = ROOT / "docs/decisions/backtest-position-source-2026-10.md"
ASSETS = ROOT / "docs/decisions/assets/kan-116"
RESULTS = json.loads((ASSETS / "results.json").read_text())
SLEEVES = ["momentum", "sector_rotation", "thematic_momentum", "quality_value",
           "earnings_drift", "tail_risk_hedge"]


def _prose() -> str:
    """Text with whitespace runs collapsed, so a reflow cannot break a check."""
    return " ".join(DECISION.read_text().split())


def _row(sleeve: str, table_heading: str) -> str:
    """The markdown table row for ``sleeve`` in the table after ``table_heading``."""
    text = DECISION.read_text()
    section = text[text.index(table_heading):]
    pattern = re.compile(rf"^\| {sleeve}[ *]")
    for line in section.splitlines():
        if pattern.match(line):
            return line
    raise AssertionError(f"no row for {sleeve} after {table_heading!r}")


def _pct(x: float, places: int = 1, sign: bool = True) -> str:
    value = f"{x * 100:+.{places}f}%" if sign else f"{x * 100:.{places}f}%"
    return value.replace("-", "−")


def _num(x: float, places: int = 2) -> str:
    return f"{x:.{places}f}".replace("-", "−")


def test_the_decision_names_the_fingerprinted_position_source() -> None:
    assert SHADOW_POSITION_SOURCE_VERSION == "runner_book"
    assert f'`SHADOW_POSITION_SOURCE_VERSION = "{SHADOW_POSITION_SOURCE_VERSION}"`' in _prose()
    # The ids are pinned in tests/backtest/test_shadow_artifact.py against the code.
    assert "`shadow:89693c0029cad3e6` becomes `shadow:12d4b27466b26cd8`" in _prose()


def test_every_replay_path_takes_a_book() -> None:
    assert "book" in inspect.signature(BacktestRunner.run).parameters
    assert "book" in inspect.signature(replay_window_detail).parameters
    assert "book" in {f.name for f in run_backtest.PortfolioConfig.__dataclass_fields__.values()}
    for path in ("scripts/run_stability_sweep.py", "scripts/validate_replacement_policy.py"):
        assert "RunnerBook()" in (ROOT / path).read_text(), path


def test_no_active_sleeve_has_a_none_context_branch() -> None:
    for sleeve in SLEEVES:
        source = inspect.getsource(getattr(run_backtest, f"make_{sleeve}_signals_fn"))
        assert "tracked" not in source, sleeve
        assert "portfolio_context is None" not in source, sleeve
        assert "_require_position_source(portfolio_context" in source, sleeve


def test_an_unbound_book_raises_as_the_decision_says() -> None:
    assert "Reading a `RunnerBook` before a runner has bound it raises" in _prose()
    with pytest.raises(RuntimeError):
        RunnerBook().positions


def test_the_re_pin_is_left_to_the_operator() -> None:
    text = _prose()
    assert "Re-pinning is the operator's decision, and this change makes none." in text
    assert "That pin was already unreproducible by current code before this change" in text


@pytest.mark.parametrize("sleeve", SLEEVES)
def test_the_2y_table_matches_the_results(sleeve) -> None:
    r = RESULTS["two_year"]["sleeves"][sleeve]
    row = _row(sleeve, "### 2-year replay")
    b, a = r["before"], r["after"]
    assert f"{_pct(b['never_filled_pct'], sign=False)} → {_pct(a['never_filled_pct'], sign=False)}" in row
    assert f"{b['retries']}/{b['retries_filled']} → {a['retries']}/{a['retries_filled']}" in row
    assert f"{b['invested'] * 100:.1f} → {a['invested'] * 100:.1f}" in row
    assert f"{_pct(b['total_return'])} → {_pct(a['total_return'])}" in row
    assert f"{_num(b['sharpe'])} → {_num(a['sharpe'])}" in row


@pytest.mark.parametrize("bars,heading", [
    ("refresh_20261013", "**A: the weekly refresh"),
    ("d10_of_record_20260819", "**B: the D10-of-record bars"),
])
@pytest.mark.parametrize("sleeve", SLEEVES)
def test_the_10y_and_d10_tables_match_the_results(bars, heading, sleeve) -> None:
    r = RESULTS["ten_year"][bars]["sleeves"][sleeve]
    row = _row(sleeve, heading)
    b, a = r["before"], r["after"]
    assert f"{_num(b['dsr'], 3)} → {_num(a['dsr'], 3)} ({_num(a['dsr_at_before_sr_star'], 3)})" in row
    assert f"{b['verdict']} → {a['verdict']}" in row
    if b["signals_sized"]:
        assert f"{_pct(b['total_return'])} → {_pct(a['total_return'])}" in row
        assert f"{_num(b['sharpe'])} → {_num(a['sharpe'])}" in row
        assert f"{b['invested'] * 100:.1f} → {a['invested'] * 100:.1f}" in row
        assert f"({b['signals_sized'] - b['lots']}/{b['signals_sized']})" in row


def test_the_sr_star_and_verdicts_match_the_results() -> None:
    text = _prose()
    for bars in ("refresh_20261013", "d10_of_record_20260819"):
        sr = RESULTS["ten_year"][bars]["sr_star"]
        assert f"SR\\* per period: {sr['before']:.4f} → {sr['after']:.4f}" in text
        verdicts = {s["after"]["verdict"] for s in RESULTS["ten_year"][bars]["sleeves"].values()}
        assert verdicts == {"FAIL"}
    assert "Every sleeve still FAILs D10 on both bar sets" in text


def test_the_pin_tables_match_the_results() -> None:
    pin = RESULTS["pins"]["edge_pin_20260915"]
    for sleeve in SLEEVES:
        r = pin["sleeves"][sleeve]
        row = _row(sleeve, "### The pinned baselines")
        assert f"{_pct(r['before']['total_return'], 2)} → {_pct(r['after']['total_return'], 2)}" in row
    agg = pin["aggregate_total_return"]
    assert f"{_pct(agg['before'], 2)} → {_pct(agg['after'], 2)}" in DECISION.read_text()
    r0 = RESULTS["pins"]["rung0_pin_20260925"]["sleeves"]["momentum"]
    assert f"goes from {_pct(r0['before']['total_return'], 2)} to {_pct(r0['after']['total_return'], 2)}" in _prose()
    assert f"{r0['before']['unfilled_pct'] * 100:.0f}% of its buy signals never became a lot" in _prose()


@pytest.mark.parametrize("sleeve", SLEEVES)
def test_the_shadow_table_matches_the_results(sleeve) -> None:
    r = RESULTS["shadow_11_windows"]["sleeves"][sleeve]
    row = _row(sleeve, "### Rolling shadow")
    for side in ("before", "after"):
        for status, n in r[side]["statuses"].items():
            assert f"{n} {status}" in row, (side, status)
    assert f"{r['before']['invested_at_start'] * 100:.0f}% → {r['after']['invested_at_start'] * 100:.0f}%" in row


def test_option_2_figures_match_the_prototype_results() -> None:
    r = RESULTS["option2_prototype_two_year"]["sector_rotation"]
    assert f"**{_pct(r['total_return'])} /" in _row("sector_rotation", "**Option 2 was measured and rejected.**")


def test_the_reproduction_assets_ship_with_the_decision() -> None:
    for name in ("results.json", "remeasure.sh", "d10_rehearsal.py"):
        assert (ASSETS / name).is_file(), name
        assert f"assets/kan-116/{name}" in _prose()
