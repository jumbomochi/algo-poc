"""Repair split artefacts in IB's daily history (KAN-120).

BACKTEST BAR FETCHES ONLY, like ``backtest.symbol_aliases``: live trading and
the paper run never read this table, and ``fetch_bars_from_ib`` applies it only
when its caller passes it.

IB serves two split artefacts in the ten-year window, both still served on
2026-10-17 under ``TRADES`` and ``ADJUSTED_LAST`` alike, so neither is a cache
fault:

* ``CMCSA`` 2017-02-17: the bar's low and close are half the session's level
  (open 35.57, high 35.62, low = close = 17.64), the day before the 2:1 split
  went ex on 2017-02-21. The backtest booked -50% then +101%.
* ``RJF`` before 2021-09-22: every bar is on a 1/2.25 basis instead of 1/1.5,
  so the series jumps by exactly 1.5 at the 3:2 split's ex-date and never
  comes back. Volumes say which side is wrong: before the split they are
  integer multiples of 0.25 (the integer count x 2.25), after it integers.

A sweep of every ticker in ``output/backtest_multi_20261013_114528.json``
found no others (``docs/decisions/backtest-split-artefacts-2026-10.md``).

Each correction checks that its glitch is present before applying, so it is
idempotent: a cache written after this change, or a series IB has since
fixed, passes through unchanged rather than being corrected twice.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping

from backtest.symbol_aliases import _day

#: A glitch is present when the observed ratio is within this of the expected.
_TOLERANCE = 0.02


@dataclass(frozen=True)
class ScaleBefore:
    """Every bar before ``before`` is ``factor`` too low (volume ``factor``
    too high): one split adjustment was applied twice."""

    ticker: str
    before: date
    factor: float
    evidence: str


@dataclass(frozen=True)
class RepairBar:
    """One bar's ``fields`` are divided by ``factor``; the rest of the bar and
    its neighbours are on the right basis."""

    ticker: str
    day: date
    fields: tuple[str, ...]
    factor: float
    evidence: str


Correction = ScaleBefore | RepairBar

BACKTEST_BAR_CORRECTIONS: dict[str, Correction] = {
    "CMCSA": RepairBar(
        "CMCSA", date(2017, 2, 17), ("low", "close"), 2.0,
        "IB conId 267748: 2017-02-17 open 35.5703 high 35.6171 low 17.6399 close "
        "17.6399 between 02-16 close 35.5937 and 02-21 open 35.5328; the 2:1 split "
        "went ex 2017-02-21. The true low is lost (IB's equals its halved close); "
        "the repair sets it to the repaired close",
    ),
    "RJF": ScaleBefore(
        "RJF", date(2021, 9, 22), 1.5,
        "IB conId 11656: 2021-09-21 close 57.2667 -> 09-22 open 86.55 (x1.511) on "
        "the 3:2 split's ex-date, intraday range 1.4%, never reversed; volumes "
        "before 09-22 are multiples of 0.25 (x2.25), after it integers",
    ),
}


def correct_bars(
    ticker: str,
    bars: list[dict[str, Any]],
    corrections: Mapping[str, Correction] | None,
) -> list[dict[str, Any]]:
    """``bars`` with ``ticker``'s correction applied, if its glitch is present.

    Returns new bar dicts for the bars it changes; never mutates ``bars``.
    """
    entry = (corrections or {}).get(ticker)
    if entry is None or not bars:
        return bars
    if isinstance(entry, RepairBar):
        return _repair_bar(entry, bars)
    return _scale_before(entry, bars)


def _near(observed: float, expected: float) -> bool:
    return abs(observed / expected - 1) <= _TOLERANCE


def _repair_bar(entry: RepairBar, bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    days = [_day(b["date"]) for b in bars]
    if entry.day not in days:
        return bars
    i = days.index(entry.day)
    bar = bars[i]
    # Present only while every named field is ``factor`` below the bar's open.
    if bar["open"] <= 0 or not all(
        bar[f] > 0 and _near(bar["open"] / bar[f], entry.factor) for f in entry.fields
    ):
        return bars
    fixed = {**bar, **{f: bar[f] * entry.factor for f in entry.fields}}
    fixed["high"] = max(fixed["high"], fixed["open"], fixed["close"])
    fixed["low"] = min(fixed["low"], fixed["open"], fixed["close"])
    return [*bars[:i], fixed, *bars[i + 1:]]


def _scale_before(entry: ScaleBefore, bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    days = [_day(b["date"]) for b in bars]
    split = next((i for i, d in enumerate(days) if d >= entry.before), None)
    if split is None or split == 0 or days[split] != entry.before:
        return bars
    last, first = bars[split - 1], bars[split]
    # Present only while the series still jumps by ``factor`` at the seam.
    if last["close"] <= 0 or not _near(first["open"] / last["close"], entry.factor):
        return bars
    scaled = [
        {
            **b,
            **{k: b[k] * entry.factor for k in ("open", "high", "low", "close")},
            "volume": int(round(b["volume"] / entry.factor)),
        }
        for b in bars[:split]
    ]
    return [*scaled, *bars[split:]]
