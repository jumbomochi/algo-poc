"""KAN-120 — backtest-only repair of the split artefacts IB serves for CMCSA and
RJF.

Pinned:

* each repair, on the bars IB actually served (copied from the 2026-10-17
  probe), leaves no split-sized move behind and leaves the rest untouched;
* every repair is idempotent, and a no-op where its glitch is absent (a cache
  written after this change, or a series IB has since fixed);
* live isolation: nothing outside the backtest can reach the table.
"""

from __future__ import annotations

import sys
import types
from datetime import date
from pathlib import Path

from backtest import bar_corrections as bc
from shared.universe import MEMBERSHIP_SNAPSHOT_PATH, MembershipCalendar

REPO = Path(__file__).resolve().parents[2]


def _bar(day, o, h, low, c, v):
    return {"date": date.fromisoformat(day), "open": o, "high": h, "low": low, "close": c, "volume": v}


# IB TRADES, conId 267748, as served on 2026-10-17.
CMCSA = [
    _bar("2017-02-15", 35.4813, 35.9075, 35.4532, 35.8748, 12043738),
    _bar("2017-02-16", 35.8092, 35.8092, 35.4485, 35.5937, 14011133),
    _bar("2017-02-17", 35.5703, 35.6171, 17.6399, 17.6399, 16212745),
    _bar("2017-02-21", 35.5328, 35.6265, 35.0457, 35.4954, 9806162),
    _bar("2017-02-22", 35.4766, 35.8045, 35.4298, 35.5422, 11253922),
]

# IB TRADES, conId 11656, as served on 2026-10-17.
RJF = [
    _bar("2021-09-17", 59.5511, 60.3244, 58.9244, 59.4267, 1264970),
    _bar("2021-09-20", 57.9244, 58.5822, 57.2800, 58.1600, 623517),
    _bar("2021-09-21", 58.2933, 58.7511, 56.9733, 57.2667, 469257),
    _bar("2021-09-22", 86.5500, 87.7500, 86.5100, 86.9200, 696912),
    _bar("2021-09-23", 88.1300, 91.7800, 87.9800, 91.6000, 680192),
]


def _largest_move(bars) -> float:
    """Largest close-to-close or close-to-open move, as a fraction."""
    moves = []
    for prev, cur in zip(bars, bars[1:]):
        moves += [abs(cur["close"] / prev["close"] - 1), abs(cur["open"] / prev["close"] - 1)]
    return max(moves)


# --- the repairs, on IB's own bars ---------------------------------------------


def test_cmcsa_repair_restores_the_halved_low_and_close():
    assert _largest_move(CMCSA) > 0.45
    fixed = bc.correct_bars("CMCSA", CMCSA, bc.BACKTEST_BAR_CORRECTIONS)
    bar = fixed[2]
    assert bar["close"] == bar["low"] == 17.6399 * 2
    assert (bar["open"], bar["high"], bar["volume"]) == (35.5703, 35.6171, 16212745)
    assert _largest_move(fixed) < 0.02
    assert fixed[:2] == CMCSA[:2] and fixed[3:] == CMCSA[3:]
    assert CMCSA[2]["close"] == 17.6399  # the input is not mutated


def test_rjf_repair_puts_the_pre_split_bars_on_the_post_split_basis():
    assert _largest_move(RJF) > 0.45
    fixed = bc.correct_bars("RJF", RJF, bc.BACKTEST_BAR_CORRECTIONS)
    assert _largest_move(fixed) < 0.06
    for before, after in zip(RJF[:3], fixed[:3]):
        for k in ("open", "high", "low", "close"):
            assert after[k] == before[k] * 1.5
        assert after["volume"] == round(before["volume"] / 1.5)
    assert fixed[3:] == RJF[3:]
    assert RJF[2]["close"] == 57.2667  # the input is not mutated


def test_every_repair_is_idempotent():
    for ticker, bars in (("CMCSA", CMCSA), ("RJF", RJF)):
        once = bc.correct_bars(ticker, bars, bc.BACKTEST_BAR_CORRECTIONS)
        assert bc.correct_bars(ticker, once, bc.BACKTEST_BAR_CORRECTIONS) == once


def test_a_repair_is_a_noop_where_its_glitch_is_absent():
    table = bc.BACKTEST_BAR_CORRECTIONS
    # A clean series (IB fixed it, or another ticker's) passes through.
    clean = [{**b, "low": 35.4, "close": 35.5} if b["date"] == date(2017, 2, 17) else b for b in CMCSA]
    assert bc.correct_bars("CMCSA", clean, table) == clean
    # A window that does not reach the seam cannot be checked, so is left alone.
    assert bc.correct_bars("RJF", RJF[:3], table) == RJF[:3]
    assert bc.correct_bars("RJF", RJF[3:], table) == RJF[3:]
    assert bc.correct_bars("CMCSA", CMCSA[:2], table) == CMCSA[:2]
    assert bc.correct_bars("AAPL", RJF, table) == RJF
    assert bc.correct_bars("RJF", [], table) == []
    assert bc.correct_bars("RJF", RJF, None) == RJF


# --- the table ------------------------------------------------------------------


def test_each_entry_is_keyed_by_its_ticker_and_names_its_evidence():
    for ticker, entry in bc.BACKTEST_BAR_CORRECTIONS.items():
        assert entry.ticker == ticker
        assert entry.factor > 1
        assert len(entry.evidence) > 40


def test_each_corrected_ticker_is_an_index_member_on_its_date():
    membership = MembershipCalendar.from_json_file(str(MEMBERSHIP_SNAPSHOT_PATH))
    for ticker, entry in bc.BACKTEST_BAR_CORRECTIONS.items():
        day = entry.day if isinstance(entry, bc.RepairBar) else entry.before
        assert ticker in membership.members_as_of(day), ticker


# --- wiring and live isolation ----------------------------------------------------


def test_no_live_module_reads_the_corrections():
    offenders = []
    for root in ("shared", "services"):
        for path in (REPO / root).rglob("*.py"):
            if "bar_corrections" in path.read_text():
                offenders.append(str(path.relative_to(REPO)))
    for script in ("scripts/run_paper.py", "scripts/reconcile_paper.py"):
        text = (REPO / script).read_text()
        if any(n in text for n in ("bar_corrections", "BACKTEST_BAR_CORRECTIONS", "correct_bars")):
            offenders.append(script)
    assert offenders == []


def test_the_paper_run_fetches_without_corrections():
    source = (REPO / "scripts" / "run_paper.py").read_text()
    call = source[source.index("bars_by_ticker = fetch_bars_from_ib("):]
    call = call[: call.index(")") + 1]
    assert "corrections" not in call


def test_the_backtest_entry_point_corrects_fetched_and_cached_bars():
    source = (REPO / "scripts" / "run_backtest.py").read_text()
    assert "corrections=BACKTEST_BAR_CORRECTIONS" in source
    cache_call = source[source.index("stale = apply_symbol_tables_to_cache("):]
    assert "BACKTEST_BAR_CORRECTIONS" in cache_call[: cache_call.index(")")]


def test_the_fetch_applies_the_corrections_it_is_given(monkeypatch):
    from scripts import run_backtest

    from tests.backtest.test_symbol_aliases import _FakeIB

    fake = types.ModuleType("ib_insync")
    fake.IB = _FakeIB
    import ib_insync

    fake.Stock = ib_insync.Stock
    monkeypatch.setitem(sys.modules, "ib_insync", fake)
    seen = []

    def spy(ticker, bars, corrections):
        seen.append((ticker, corrections))
        return bars

    monkeypatch.setattr(run_backtest, "correct_bars", spy)
    table = {"RJF": bc.BACKTEST_BAR_CORRECTIONS["RJF"]}
    run_backtest.fetch_bars_from_ib(["RJF", "AAPL"], years=1, corrections=table)
    assert seen == [("RJF", table), ("AAPL", table)]


def test_a_cached_run_applies_the_corrections_once():
    from scripts.run_backtest import apply_symbol_tables_to_cache

    cache = {"RJF": list(RJF), "CMCSA": list(CMCSA)}
    apply_symbol_tables_to_cache(cache, {}, {}, bc.BACKTEST_BAR_CORRECTIONS)
    once = {k: list(v) for k, v in cache.items()}
    assert _largest_move(once["RJF"]) < 0.06 and _largest_move(once["CMCSA"]) < 0.02
    apply_symbol_tables_to_cache(cache, {}, {}, bc.BACKTEST_BAR_CORRECTIONS)
    assert cache == once
