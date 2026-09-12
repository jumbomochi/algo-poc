"""A paper run that fetched no bars must not report success.

On 2026-09-10 the 04:15 run logged, in this order:

    Fetched data for 0 of 140 tickers; 140 returned NO bars
    No signals generated today
    0 recommendations published to stream:recommendations
    Paper trading run completed (exit code: 0)
    dead-man switch: pinged https://hc-ping.com/...

Every external observer saw a healthy day. The healthchecks check-in arrived,
launchd recorded exit 0, the Telegram summary said the run completed. The whole
"nothing outside this host can tell the run happened" layer reported that it
had — correctly — while the run accomplished nothing.

**The detection already existed and was right.** ``summarise_fetch`` /
``FetchSummary`` were written after a 2026-08-19 baseline run printed
"Successfully fetched data for 826 tickers" while 169 of them had zero bars, and
``fetch_bars_from_ib`` calls it, which is why the first line above is in the log
at all. It was reporting-only: nothing turned it into an exit decision. That is
the KAN-64 lesson restated — a failure whose nature is silence cannot be
reported into a file nobody reads.

**Why the old guard missed it.** ``if not bars_by_ticker`` tested the container,
not the contents. ``run_backtest.py:372`` assigns ``bars_by_ticker[ticker] =
unique_bars`` unconditionally and only an *exception* routes a ticker to
``failed``; a 60-second timeout returns an empty list without raising. So the
dict was ``{140 tickers: []}`` — 140 keys, every value empty, comfortably truthy.

A failing run is recoverable: someone is told, and re-runs it. A run that
reports success while doing nothing is not, because nobody is told.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from scripts.run_backtest import summarise_fetch
from scripts.run_paper import (
    EXIT_INSUFFICIENT_BAR_COVERAGE,
    bar_coverage_failure,
)

REPO = Path(__file__).resolve().parents[2]
RUN_PAPER = REPO / "scripts/run_paper.py"

TICKERS = [f"T{i:03d}" for i in range(140)]


def _bars(n_with_data: int) -> dict[str, list[dict]]:
    """A fetch result where `n_with_data` tickers came back with bars.

    Empty lists rather than missing keys, because that is what the real fetcher
    produces on a timeout and it is the shape the old guard could not see.
    """
    return {
        t: ([{"date": "2026-09-09", "close": 1.0}] if i < n_with_data else [])
        for i, t in enumerate(TICKERS)
    }


def test_zero_bars_for_every_ticker_is_a_failure():
    """The 2026-09-10 run exactly."""
    summary = summarise_fetch(requested=TICKERS, bars_by_ticker=_bars(0))
    reason = bar_coverage_failure(summary)
    assert reason is not None, "a run that fetched nothing reported success"
    assert "0 of 140" in reason, reason


def test_the_failure_does_not_blame_a_gateway_that_is_connected():
    """The old message was "No data fetched. Is IB Gateway running?". On
    2026-09-10 the gateway WAS running and connected — it had simply been up
    three days without its 2:00 PM restart and had stopped serving historical
    data. A message that sends the operator to check the wrong thing costs more
    than no message."""
    summary = summarise_fetch(requested=TICKERS, bars_by_ticker=_bars(0))
    reason = bar_coverage_failure(summary)
    assert "Is IB Gateway running?" not in reason, reason


def test_full_coverage_is_not_a_failure():
    summary = summarise_fetch(requested=TICKERS, bars_by_ticker=_bars(140))
    assert bar_coverage_failure(summary) is None


def test_a_few_empty_tickers_still_succeeds():
    """The FetchSummary docstring is explicit that a ticker returning no bars
    can be a real answer — IB's Error 200 for an unresolved contract, or a name
    with no history that deep. Aborting on those would fire on ordinary days and
    teach an operator to ignore the guard, and each abort is a self-inflicted
    evidence gap."""
    summary = summarise_fetch(requested=TICKERS, bars_by_ticker=_bars(137))
    assert bar_coverage_failure(summary) is None


def test_coverage_below_the_floor_is_a_failure_and_names_the_numbers():
    """Signals computed on a fraction of the universe are not comparable to the
    baseline the divergence monitor grades against, so committing them corrupts
    the evidence rather than merely thinning it."""
    summary = summarise_fetch(requested=TICKERS, bars_by_ticker=_bars(40))
    reason = bar_coverage_failure(summary)
    assert reason is not None
    assert "40 of 140" in reason, reason


def test_the_floor_is_configurable_and_inclusive():
    summary = summarise_fetch(requested=TICKERS, bars_by_ticker=_bars(70))
    assert bar_coverage_failure(summary, floor=0.5) is None       # exactly 50%
    assert bar_coverage_failure(summary, floor=0.51) is not None


def test_an_empty_request_is_not_reported_as_zero_coverage():
    """Nothing asked for is not the same fact as nothing returned, and a
    division by zero here would crash the run it exists to protect."""
    summary = summarise_fetch(requested=[], bars_by_ticker={})
    assert bar_coverage_failure(summary) is None


# ---------------------------------------------------------------------------
# The wiring: the guard has to decide the exit, not just exist
# ---------------------------------------------------------------------------


def _main_source() -> str:
    tree = ast.parse(RUN_PAPER.read_text())
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "main"
    )
    return ast.unparse(fn)


def test_the_container_test_is_gone():
    """`if not bars_by_ticker` is truthy for {140 tickers: []} and is exactly
    what let the 2026-09-10 run through."""
    body = _main_source()
    assert "if not bars_by_ticker" not in body, (
        "the container test is still the guard; it cannot see empty values"
    )


def test_main_decides_the_exit_from_the_coverage_summary():
    body = _main_source()
    assert "bar_coverage_failure" in body, "main never consults the coverage guard"
    assert "EXIT_INSUFFICIENT_BAR_COVERAGE" in body, (
        "main does not exit with the documented coverage code"
    )


def test_the_exit_code_is_distinct_from_the_existing_ones():
    """1 is generic failure and 2 is validation. "fetched nothing" must not be
    confused with "could not connect", because they send the operator to
    different places."""
    assert EXIT_INSUFFICIENT_BAR_COVERAGE not in (0, 1, 2)


def test_the_wrapper_withholds_the_dead_man_ping_on_any_nonzero_exit():
    """No wrapper change is needed, but that is a property worth pinning: the
    whole fix rests on a non-zero exit being enough to suppress the ping."""
    wrapper = (REPO / "deploy/launchd/run_paper.sh").read_text()
    assert 'algo_deadman_ping "$EXIT_CODE"' in wrapper, (
        "run_paper.sh no longer keys the dead-man ping on the exit code"
    )
