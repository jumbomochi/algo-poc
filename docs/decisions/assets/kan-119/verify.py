#!/usr/bin/env python3
"""KAN-119 alias verification: fetch every CANDIDATE alias's bars through the
real fetch path and measure, per alias, what the evidence rules need:

* conId / longName (from probe_results.json);
* sessions of the old ticker's membership (before ``until``) that the conId prices;
* the largest daily moves inside the old ticker's membership window, and the
  move at the ticker change (continuity);
* for splices, the move at ``until`` between the alias and the own series.

    PYTHONPATH=. python verify.py OUT.json [--client-id 78] [--bars cached.json]

The IR and Q rows of verify.json were measured with a splice mode the final
module does not have (old company's conId before until, own symbol from
it); the splice was refused, and those rows are kept as the evidence for that.
SATS -> ECHO was checked afterwards with a one-year fetch (decided.json).
"""
from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

HERE = Path(__file__).parent

#: (old, new, own_from_until)
CANDIDATES = [
    ("ABC", "COR", False), ("ADS", "BFH", False), ("ANTM", "ELV", False), ("ARNC", "HWM", False),
    ("AA", "HWM", False), ("BHGE", "BKR", False), ("BK", "BNY", False), ("BLL", "BALL", False),
    ("CBG", "CBRE", False), ("COH", "TPR", False), ("CTL", "LUMN", False), ("DLPH", "APTV", False),
    ("DPS", "KDP", False), ("FB", "META", False), ("FBHS", "FBIN", False), ("FLT", "CPAY", False),
    ("GPS", "GAP", False), ("HCN", "WELL", False), ("HCP", "DOC", False), ("PEAK", "DOC", False),
    ("HFC", "DINO", False), ("HRS", "LHX", False), ("JEC", "J", False), ("KORS", "CPRI", False),
    ("LB", "BBWI", False), ("LUK", "JEF", False), ("MYL", "VTRS", False), ("NLOK", "GEN", False),
    ("SYMC", "GEN", False), ("PCLN", "BKNG", False), ("PKI", "RVTY", False), ("RE", "EG", False),
    ("TMK", "GL", False), ("UA C", "UA", False), ("UTX", "RTX", False), ("WLTW", "WTW", False),
    ("WYN", "TNL", False), ("IR", "TT", True), ("Q", "IQV", True),
]


def main() -> int:
    from backtest.symbol_aliases import BacktestAlias
    from scripts.run_backtest import fetch_bars_from_ib
    from shared.universe import MEMBERSHIP_SNAPSHOT_PATH

    p = argparse.ArgumentParser()
    p.add_argument("out")
    p.add_argument("--client-id", type=int, default=78)
    p.add_argument("--bars", default=None)
    args = p.parse_args()

    raw = json.loads(Path(MEMBERSHIP_SNAPSHOT_PATH).read_text())["snapshots"]
    snaps = sorted((date.fromisoformat(d), set(m)) for d, m in raw.items())
    probe = {r["ticker"]: r for r in json.loads((HERE / "probe_results.json").read_text())}

    def details(old, new):
        r = probe.get(old, {})
        if old == "AA":  # not in CANDIDATES map of the probe; HWM came from ARNC's row
            r = probe["ARNC"]
        for d in r.get("candidate_details") or []:
            if d["symbol"] == new:
                return d
        return None

    aliases, info = {}, {}
    for old, new, splice in CANDIDATES:
        d = details(old, new)
        if d is None:
            info[old] = {"skip": f"no contract details for {new}"}
            continue
        until = next((s for s, m in snaps if new in m), None)
        aliases[old] = BacktestAlias(old, new, d["conId"], d["longName"], until, "candidate")
        info[old] = {"new": new, "conId": d["conId"], "longName": d["longName"], "head": d.get("head"),
                     "until": until and until.isoformat(), "splice": splice}

    if args.bars:
        bars = {t: [{**b, "date": date.fromisoformat(b["date"])} for b in v]
                for t, v in json.loads(Path(args.bars).read_text()).items()}
    else:
        bars = fetch_bars_from_ib(sorted(aliases), years=10, client_id=args.client_id, aliases=aliases)
        Path(args.out.replace(".json", "_bars.json")).write_text(json.dumps(
            {t: [{**b, "date": b["date"].isoformat()} for b in v] for t, v in bars.items()}))

    return report(aliases, info, bars, args.out)


ARTIFACT = Path.home() / "algo-poc-deploy/output/backtest_multi_20261013_114528.json"


def report(aliases, info, bars, out):
    from backtest.runner import collect_sorted_dates
    from scripts.run_backtest import load_membership_calendar
    from shared.universe import MEMBERSHIP_SNAPSHOT_PATH

    art = json.loads(ARTIFACT.read_text())
    art_bars = {t: [{**b, "date": date.fromisoformat(b["date"][:10])} for b in v]
                for t, v in art["bars"].items()}
    sessions = collect_sorted_dates(art_bars)
    cal = load_membership_calendar(str(MEMBERSHIP_SNAPSHOT_PATH))
    members_on = {s: cal.members_as_of(s) for s in sessions}
    for old, a in aliases.items():
        rec = info[old]
        splice = rec.get("splice")
        series = sorted(bars.get(old, []), key=lambda b: b["date"])
        by_day = {b["date"]: b["close"] for b in series}
        member = [s for s in sessions if old in members_on[s] and (a.until is None or s < a.until)]
        priced = [s for s in member if s in by_day]
        was = sum(1 for s in member if s in {b["date"] for b in art_bars.get(old, [])})
        moves = []
        prev = None
        for b in series:
            if prev is not None and member and member[0] <= b["date"] <= member[-1]:
                moves.append((abs(b["close"] / prev["close"] - 1), prev["date"].isoformat(),
                              b["date"].isoformat(), prev["close"], b["close"]))
            prev = b
        moves.sort(reverse=True)
        rec.update({
            "member_sessions": len(member), "priced": len(priced), "priced_before": was,
            "first_member": member[0].isoformat() if member else None,
            "last_member": member[-1].isoformat() if member else None,
            "first_bar": series[0]["date"].isoformat() if series else None,
            "top_moves": [(round(m[0], 3),) + m[1:] for m in moves[:3]],
        })
        if splice and a.until:
            before = [b for b in series if b["date"] < a.until][-1:]
            after = [b for b in series if b["date"] >= a.until][:1]
            rec["splice"] = [(b["date"].isoformat(), b["close"]) for b in before + after]
        print(f"{old:5}->{a.ib_symbol:5} {a.long_name[:28]:28} until={rec['until']} "
              f"member={len(member)} priced={len(priced)} (was {was}) "
              f"top={rec['top_moves'][:1]}")
    Path(out).write_text(json.dumps(info, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
