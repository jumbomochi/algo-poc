#!/usr/bin/env python3
"""KAN-120: sweep a backtest artifact's bars for split artefacts.

Usage: scan.py ARTIFACT.json

Two shapes, both at a split ratio (3:2, 2:1, 3:1, ... and their inverses, plus
4:3 and 5:4 for level shifts):

* ``shift``: the whole session moves by the ratio (open gap and close both
  within 2.5% of it) on a quiet day (intraday range under 6%). An unadjusted
  or doubly adjusted split looks like this; an earnings gap rarely does.
* ``bar``: one field of a bar (close, low, high, open) sits at the ratio from
  the bar's open (or close, for the open) while the rest of the bar does not.

Hits need reading: penny stocks and ticker reuse after a member left the index
trip ``bar`` routinely. On 2026-10-13's artifact the only split artefacts were
CMCSA 2017-02-17 and RJF 2021-09-22.
"""
from __future__ import annotations

import json
import sys

RATIOS = [1.5, 2, 3, 4, 5, 8, 10, 15, 20]
SHIFT_RATIOS = RATIOS + [1.25, 4 / 3]


def _near(x: float, ratios: list[float], tol: float) -> list[float]:
    every = ratios + [1 / r for r in ratios]
    return [round(r, 3) for r in every if abs(x / r - 1) < tol]


def main(path: str) -> int:
    bars = json.load(open(path))["bars"]
    for ticker, rows in sorted(bars.items()):
        for prev, cur in zip(rows, rows[1:]):
            if min(prev["close"], cur["open"], cur["close"], cur["low"]) <= 0:
                continue
            move, gap = cur["close"] / prev["close"], cur["open"] / prev["close"]
            quiet = cur["high"] / cur["low"] - 1 < 0.06
            if (abs(move - 1) > 0.2 and quiet and _near(move, SHIFT_RATIOS, 0.025)
                    and _near(gap, SHIFT_RATIOS, 0.025)):
                print("shift", ticker, prev["date"], cur["date"], round(move, 3), round(gap, 3))
            for field in ("close", "low", "high", "open"):
                ref = cur["close"] if field == "open" else cur["open"]
                x = cur[field] / ref
                if abs(x - 1) > 0.3 and _near(x, RATIOS, 0.03):
                    print("bar", ticker, cur["date"], field, round(x, 3))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
