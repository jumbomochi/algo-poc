"""KAN-103: re-grade live vs the rolling shadow with dates aligned. READ ONLY.

Reproduces docs/decisions/divergence-session-dating-2026-10.md, "Quantification".
Writes nothing but its own stdout (and regrade.json beside the TSV).

Inputs
------
1. A SELECT-only export of ``equity_snapshots`` as TSV, path in
   ``KAN103_SNAPSHOTS`` (default ``./snapshots.tsv``)::

     SELECT portfolio, date, equity, cash, market_value,
            coalesce(to_char(valuation_at AT TIME ZONE 'UTC',
                             'YYYY-MM-DD"T"HH24:MI:SS'), '') AS val_utc,
            to_char(created_at AT TIME ZONE 'UTC',
                    'YYYY-MM-DD"T"HH24:MI:SS') AS created_utc
     FROM equity_snapshots ORDER BY portfolio, date;

   (psql -A -F $'\\t' -P footer=off, in a session with
   default_transaction_read_only=on.)
2. The shadow artifacts ``shadow_YYYYMMDD.json``, globbed from
   ``KAN103_SHADOW_DIRS`` (colon-separated; default: the pre-KAN-72 working
   tree's output/ then the deploy clone's output/). Read, never moved.

Run from the repo root:  python docs/decisions/assets/kan-103/regrade.py
"""
from __future__ import annotations

import csv
import glob
import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timezone

import exchange_calendars as xcals
import pandas as pd

REPO = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", ".."))
sys.path.insert(0, REPO)
from backtest.divergence import (  # noqa: E402  pure functions, no I/O
    align_and_window,
    classify_status,
    compute_divergence,
    correlation,
    daily_returns,
    window_return,
)

SNAPSHOTS = os.environ.get("KAN103_SNAPSHOTS", "snapshots.tsv")
SHADOW_DIRS = os.environ.get(
    "KAN103_SHADOW_DIRS",
    os.path.expanduser("~/GitHub/algo-poc/output") + ":"
    + os.path.expanduser("~/algo-poc-deploy/output"),
).split(":")
BOUNDARY = date(2026, 8, 1)  # config/default.yaml divergence.live_history_from
WINDOW = 30
SLEEVES = [
    "earnings_drift", "momentum", "quality_value",
    "sector_rotation", "tail_risk_hedge", "thematic_momentum",
]
CAL = xcals.get_calendar("XNYS")
SESSIONS = [
    d.date() for d in CAL.sessions_in_range(pd.Timestamp("2026-06-01"), pd.Timestamp("2026-12-31"))
]


def session_close_utc(d: date) -> datetime:
    return CAL.session_close(pd.Timestamp(d)).to_pydatetime().astimezone(timezone.utc)


def valued_session(sgt_date: date, valuation_at: datetime | None) -> tuple[date, str]:
    """The US session a snapshot priced.

    The last NYSE session that had closed by ``valuation_at``; for rows written
    before ``valuation_at`` existed (KAN-44), the last session strictly before
    the SGT run date. The two rules agree on every row that has both.
    """
    if valuation_at is not None:
        return [s for s in SESSIONS if session_close_utc(s) <= valuation_at][-1], "valuation_at"
    return [s for s in SESSIONS if s < sgt_date][-1], "fallback"


def load_snapshots() -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    with open(SNAPSHOTS) as fh:
        for r in csv.DictReader(fh, delimiter="\t"):
            val = (
                datetime.fromisoformat(r["val_utc"]).replace(tzinfo=timezone.utc)
                if r["val_utc"] else None
            )
            out[r["portfolio"]].append({
                "date": date.fromisoformat(r["date"]),
                "equity": float(r["equity"]),
                "val": val,
                "created": datetime.fromisoformat(r["created_utc"]).replace(tzinfo=timezone.utc),
            })
    return out


SNAPS = load_snapshots()


def live_series(sleeve: str, *, as_of_run: date, aligned: bool) -> dict[date, float]:
    """Live equity as the monitor saw it on SGT run date ``as_of_run``.

    ``aligned=False`` is today's keying (the SGT run date); ``aligned=True``
    keys each row by the US session it valued. Where two run dates valued one
    session (a Tuesday after a US Monday holiday, a Sunday catch-up) the
    later-created row wins; the values are identical in every observed case.
    """
    out: dict[date, tuple[datetime, float]] = {}
    for r in SNAPS[sleeve]:
        if r["date"] > as_of_run or r["date"] < BOUNDARY:
            continue
        key = valued_session(r["date"], r["val"])[0] if aligned else r["date"]
        prev = out.get(key)
        if prev is None or r["created"] > prev[0]:
            out[key] = (r["created"], r["equity"])
    return {k: v for k, (_, v) in out.items()}


def shadow_files() -> list[str]:
    by_name: dict[str, str] = {}
    for d in SHADOW_DIRS:
        for f in sorted(glob.glob(os.path.join(d, "shadow_2026*.json"))):
            by_name[os.path.basename(f)] = f  # later dirs win a name clash
    return [by_name[k] for k in sorted(by_name)]


def grade(live: dict, model: dict) -> dict:
    dates, lv, bv = align_and_window(live, model, WINDOW)
    if len(dates) < 2:
        return {"n": len(dates), "status": "NO_DATA", "rel": None, "abs": None,
                "corr": None, "start": None, "end": dates[-1] if dates else None,
                "lr": None, "br": None}
    lr, br = window_return(lv), window_return(bv)
    a, rel = compute_divergence(lr, br)
    return {"n": len(dates), "status": classify_status(rel, a), "rel": rel, "abs": a,
            "corr": correlation(daily_returns(lv), daily_returns(bv)),
            "start": dates[0], "end": dates[-1], "lr": lr, "br": br}


def aggregate(series_by_sleeve: dict[str, dict]) -> dict:
    if not series_by_sleeve:
        return {}
    common = set.intersection(*(set(s) for s in series_by_sleeve.values()))
    return {d: sum(s[d] for s in series_by_sleeve.values()) for d in sorted(common)}


def regrade_all() -> list[dict]:
    results = []
    for path in shadow_files():
        art = json.load(open(path))
        stamp = os.path.basename(path)[len("shadow_"):-len(".json")]
        run = date.fromisoformat(
            art.get("produced_on") or f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:]}"
        )
        shadow = {
            k: {date.fromisoformat(d): float(v) for d, v in curve.items()}
            for k, curve in art["series"].items() if k in SLEEVES
        }
        row: dict = {"run": run, "file": os.path.basename(path)}
        for mode in ("mis", "ali"):
            lives = {s: live_series(s, as_of_run=run, aligned=(mode == "ali")) for s in SLEEVES}
            per = {
                s: grade(lives[s], shadow[s]) if s in shadow else grade({}, {})
                for s in SLEEVES
            }
            in_both = {s: lives[s] for s in SLEEVES if s in shadow}
            per["AGGREGATE"] = grade(aggregate(in_both), aggregate({s: shadow[s] for s in in_both}))
            row[mode] = per
        results.append(row)
    return results


def _pct(x, nd=1):
    return "—" if x is None else f"{x * 100:+.{nd}f}%"


def _num(x):
    return "—" if x is None else f"{x:+.2f}"


def main() -> None:
    print("== snapshot run date (SGT) -> US session valued ==")
    for r in SNAPS["_aggregate"]:
        s, how = valued_session(r["date"], r["val"])
        fb, _ = valued_session(r["date"], None)
        flag = "" if s == fb else "  <-- rules disagree"
        print(f"  {r['date']} {r['date']:%a} -> {s} {s:%a} [{how}]{flag}")
    results = regrade_all()
    with open(os.path.join(os.path.dirname(os.path.abspath(SNAPSHOTS)), "regrade.json"), "w") as fh:
        json.dump(results, fh, default=str, indent=1)
    for sleeve in SLEEVES + ["AGGREGATE"]:
        print(f"\n== {sleeve} ==   MISALIGNED (today) | ALIGNED")
        for r in results:
            m, a = r["mis"][sleeve], r["ali"][sleeve]
            print(
                f"  {r['run']} | {m['n']:>2} {m['status']:<8} {_pct(m['rel'], 0):>7} "
                f"{_pct(m['abs'], 2):>7} {_num(m['corr']):>6} | {a['n']:>2} {a['status']:<8} "
                f"{_pct(a['rel'], 0):>7} {_pct(a['abs'], 2):>7} {_num(a['corr']):>6}"
                f"   end {m['end']} / {a['end']}"
            )


if __name__ == "__main__":
    main()
