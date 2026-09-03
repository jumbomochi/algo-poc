"""A fetch that returns nothing for a ticker must not report success.

The 2026-08-19 baseline run printed

    Successfully fetched data for 826 tickers

while **169 of those 826 had zero bars**. The cause is that
``ib.qualifyContracts`` logs IB's Error 200 ("No security definition has been
found") without raising, so ``reqHistoricalData`` returns empty and the ticker
never lands in the ``failed`` list the summary reports on. From the log:

    [76/826] Fetching AVB... Error 200, reqId 828: No security definition has
    been found for the request, contract: Stock(symbol='AVB', ...)

A 5.4-hour run that reports complete success while a fifth of the universe
returned nothing is the same shape as the coverage gate this workstream started
with: a guard whose failure is invisible.

Distinct from IB's Error 162 ("HMDS query returned no data"), which is a real
data-availability answer for a resolved contract — PEP resolved fine with
``conId=11017`` and simply has no bars that deep. A ticker with SOME bars is not
a fetch failure; a ticker with NONE is.
"""
from __future__ import annotations

from datetime import date

import pytest

from scripts.run_backtest import summarise_fetch


def test_a_complete_fetch_reports_every_ticker() -> None:
    summary = summarise_fetch(
        requested=["AAPL", "MSFT"],
        bars_by_ticker={"AAPL": [{"date": date(2026, 1, 5)}], "MSFT": [{"date": date(2026, 1, 5)}]},
    )

    assert summary.fetched == 2
    assert summary.requested == 2
    assert summary.empty == []
    assert summary.is_complete is True


def test_a_ticker_with_no_bars_is_counted_as_empty() -> None:
    summary = summarise_fetch(
        requested=["AAPL", "AVB"],
        bars_by_ticker={"AAPL": [{"date": date(2026, 1, 5)}], "AVB": []},
    )

    assert summary.empty == ["AVB"]
    assert summary.fetched == 1
    assert summary.is_complete is False


def test_a_ticker_absent_from_the_result_is_also_empty() -> None:
    """An exception during its fetch drops the key entirely. Same outcome for
    the caller: no bars."""
    summary = summarise_fetch(
        requested=["AAPL", "EQR"],
        bars_by_ticker={"AAPL": [{"date": date(2026, 1, 5)}]},
    )

    assert summary.empty == ["EQR"]


def test_partial_history_is_not_a_fetch_failure() -> None:
    """Error 162 on a deep chunk leaves a short series, not an empty one. PEP
    resolved and returned 2,175 of 2,511 sessions; calling that a failure would
    make the alarm fire on every run and teach an operator to ignore it."""
    summary = summarise_fetch(
        requested=["PEP"],
        bars_by_ticker={"PEP": [{"date": date(2026, 1, 5)}]},
    )

    assert summary.empty == []
    assert summary.is_complete is True


def test_the_empty_list_is_sorted_for_a_stable_report() -> None:
    summary = summarise_fetch(
        requested=["EQR", "AVB", "ABC"],
        bars_by_ticker={},
    )

    assert summary.empty == ["ABC", "AVB", "EQR"]


def test_the_summary_line_states_both_numbers() -> None:
    """The old line said only 826. A reader has to be able to see the gap
    without cross-checking the artifact."""
    line = summarise_fetch(
        requested=["AAPL", "AVB"],
        bars_by_ticker={"AAPL": [{"date": date(2026, 1, 5)}], "AVB": []},
    ).summary_line()

    assert "1" in line and "2" in line


def test_the_summary_line_names_the_empty_tickers() -> None:
    line = summarise_fetch(
        requested=["AAPL", "AVB"],
        bars_by_ticker={"AAPL": [{"date": date(2026, 1, 5)}], "AVB": []},
    ).summary_line()

    assert "AVB" in line


def test_a_complete_fetch_says_so_without_a_scary_line() -> None:
    line = summarise_fetch(
        requested=["AAPL"],
        bars_by_ticker={"AAPL": [{"date": date(2026, 1, 5)}]},
    ).summary_line()

    assert "AAPL" not in line
    assert "NO bars" not in line


def test_the_report_is_serialisable_for_the_artifact() -> None:
    """output/ is gitignored and a 5.4h run is not repeated to answer 'which
    tickers were missing' — the artifact has to carry it."""
    payload = summarise_fetch(
        requested=["AAPL", "AVB"],
        bars_by_ticker={"AAPL": [{"date": date(2026, 1, 5)}], "AVB": []},
    ).to_dict()

    assert payload["requested"] == 2
    assert payload["fetched"] == 1
    assert payload["empty"] == ["AVB"]


# ---------------------------------------------------------------------------
# reaching the artifact
# ---------------------------------------------------------------------------


def _envelope(**overrides):
    from backtest.costs import CostModel
    from scripts.run_backtest import build_base_config

    kwargs = dict(
        all_tickers=["AAPL", "AVB"], years=10, capital=100_000.0,
        cost_model=CostModel(), replacement_policy="TECHNICAL_ONLY",
        replacement_score_margin=0.0, portfolio_capitals={"momentum": 100.0},
        point_in_time_universe=True,
    )
    kwargs.update(overrides)
    return build_base_config(**kwargs)


def test_the_artifact_records_which_tickers_returned_nothing() -> None:
    """A reader asking 'was this baseline built on complete data?' must not
    have to re-run 5.4 hours of fetching to find out."""
    summary = summarise_fetch(
        requested=["AAPL", "AVB"],
        bars_by_ticker={"AAPL": [{"date": date(2026, 1, 5)}], "AVB": []},
    )

    config = _envelope(fetch=summary)

    assert config["fetch"]["empty"] == ["AVB"]
    assert config["fetch"]["fetched"] == 1


def test_a_run_with_no_fetch_summary_omits_the_block() -> None:
    """Same contract as ``coverage``: absence stays visible rather than being
    written as a zeroed block a reader would mistake for a clean fetch."""
    assert "fetch" not in _envelope()
