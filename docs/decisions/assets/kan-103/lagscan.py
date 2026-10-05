"""KAN-103: at which session offset do live returns line up with a model? READ ONLY.

Live points are keyed by the US session they valued (see regrade.py). For each
pair of consecutive live points (a -> b) the model's return over the same span,
shifted by k NYSE sessions, is paired with live's. k = 0 is the aligned
hypothesis; k = +1 is what the monitor does today, because live's SGT label is
one session after the session it valued.

The model here is the 10-year baseline (``backtest_multi_*.json``): a warm book
with a value on every session, Mondays included, so it is free of both the
shadow's clipping to live's dates and the shadow's cold start from cash. The
260 MB artifact is too large to re-read per run, so ``--extract`` first writes
its last 90 sessions per sleeve beside the snapshots TSV.

  python docs/decisions/assets/kan-103/lagscan.py --extract ~/algo-poc-deploy/output/backtest_multi_20260929_102017.json
  python docs/decisions/assets/kan-103/lagscan.py bt_tail_backtest_multi_20260929_102017.json
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from regrade import SESSIONS, SLEEVES, correlation, live_series  # noqa: E402

IDX = {s: i for i, s in enumerate(SESSIONS)}


def extract(src: str) -> None:
    d = json.load(open(os.path.expanduser(src)))
    dates = d["aggregate"]["dates"]
    out = {"source": os.path.basename(src), "series": {}}
    for name, p in d["portfolios"].items():
        pv = p["portfolio_values"]
        eod = pv[1:] if len(pv) == len(dates) + 1 else pv  # pv[0] is pre-day-0 capital
        out["series"][name] = dict(list(zip(dates, eod))[-90:])
    with open("bt_tail_" + os.path.basename(src), "w") as fh:
        json.dump(out, fh)
    print(f"last bar {dates[-1]}; wrote bt_tail_{os.path.basename(src)}")


def scan(live: dict, model: dict, lo: date, hi: date) -> dict[int, tuple[float | None, int]]:
    pts = sorted(d for d in live if lo <= d <= hi)
    out = {}
    for k in (-2, -1, 0, 1, 2):
        xs, ys = [], []
        for a, b in zip(pts, pts[1:]):
            ma, mb = SESSIONS[IDX[a] + k], SESSIONS[IDX[b] + k]
            if ma in model and mb in model and live[a] > 0 and model[ma] > 0:
                xs.append(live[b] / live[a] - 1)
                ys.append(model[mb] / model[ma] - 1)
        out[k] = (correlation(xs, ys), len(xs))
    return out


def main(tail_path: str) -> None:
    bt = json.load(open(tail_path))
    model = {k: {date.fromisoformat(d): float(v) for d, v in s.items()} for k, s in bt["series"].items()}
    hi = max(model["momentum"])
    print(f"== live (aligned) vs {bt['source']}, 2026-08-18..{hi} ==")
    agg_live: dict[date, float] = {}
    agg_bt: dict[date, float] = {}
    for s in SLEEVES + ["AGGREGATE"]:
        if s == "AGGREGATE":
            live, m = agg_live, agg_bt
        else:
            live, m = live_series(s, as_of_run=date(2026, 10, 3), aligned=True), model[s]
            for d, v in live.items():
                agg_live[d] = agg_live.get(d, 0.0) + v
            for d, v in m.items():
                agg_bt[d] = agg_bt.get(d, 0.0) + v
        cells = "  ".join(
            f"k={k:+d}: {'  — ' if c is None else f'{c:+.2f}'} (n={n})"
            for k, (c, n) in scan(live, m, date(2026, 8, 18), hi).items()
        )
        print(f"  {s:<18} {cells}")


if __name__ == "__main__":
    if sys.argv[1] == "--extract":
        extract(sys.argv[2])
    else:
        main(sys.argv[1])
