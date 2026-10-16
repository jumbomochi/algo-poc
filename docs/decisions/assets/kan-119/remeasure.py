#!/usr/bin/env python3
"""KAN-119 re-measure: the coverage a backtest artifact would have had with the aliases.

Reproduces the artifact's own coverage figure first (same function, same bars,
same membership file) so the comparison is like for like, then fetches ONLY
the aliased tickers from the paper Gateway (clientId 77), clipped to the
artifact's sessions, and measures again.

    PYTHONPATH=. python remeasure.py --artifact ~/algo-poc-deploy/output/backtest_multi_20261013_114528.json \
        --out remeasure.json [--alias-bars alias_bars.json]   # reuse a previous fetch
"""
from __future__ import annotations

import argparse
import json
from datetime import date


def _bars(raw: dict) -> dict[str, list[dict]]:
    return {t: [{**b, "date": date.fromisoformat(str(b["date"])[:10])} for b in bars]
            for t, bars in raw.items()}


def main() -> int:
    from backtest.membership import measure_coverage, priced_days_from_bars
    from backtest.runner import collect_sorted_dates
    from backtest.symbol_aliases import BACKTEST_ALIASES, BACKTEST_UNPRICED, drop_unpriced
    from scripts.run_backtest import fetch_bars_from_ib, load_membership_calendar
    from shared.universe import MEMBERSHIP_SNAPSHOT_PATH

    p = argparse.ArgumentParser()
    p.add_argument("--artifact", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--alias-bars", default=None)
    p.add_argument("--client-id", type=int, default=77)
    args = p.parse_args()

    art = json.load(open(args.artifact))
    recorded = art["config"]["coverage"]
    bars = _bars(art["bars"])
    sessions = collect_sorted_dates(bars)
    membership = load_membership_calendar(str(MEMBERSHIP_SNAPSHOT_PATH))
    before = measure_coverage(membership, sessions=sessions, priced_tickers=priced_days_from_bars(bars))
    assert abs(before.excluded_pct - recorded["excluded_pct"]) < 1e-9, (
        f"cannot reproduce the artifact's coverage: {before.excluded_pct} vs {recorded['excluded_pct']}"
    )

    if args.alias_bars:
        alias_bars = _bars(json.load(open(args.alias_bars)))
    else:
        alias_bars = fetch_bars_from_ib(sorted(BACKTEST_ALIASES), years=10,
                                        client_id=args.client_id, aliases=BACKTEST_ALIASES)
        json.dump({t: [{**b, "date": b["date"].isoformat()} for b in v] for t, v in alias_bars.items()},
                  open(args.out.replace(".json", "_alias_bars.json"), "w"))
    session_set = set(sessions)
    merged = dict(bars)
    for t, v in alias_bars.items():
        merged[t] = [b for b in v if b["date"] in session_set]
    for t in BACKTEST_UNPRICED:
        merged[t] = drop_unpriced(t, merged.get(t, []), BACKTEST_UNPRICED)

    after = measure_coverage(membership, sessions=sessions, priced_tickers=priced_days_from_bars(merged))
    result = {
        "artifact": args.artifact,
        "before": {"excluded_pct": before.excluded_pct, "excluded_days": before.excluded_membership_days,
                   "total_days": before.total_membership_days, "state": before.state},
        "after": {"excluded_pct": after.excluded_pct, "excluded_days": after.excluded_membership_days,
                  "total_days": after.total_membership_days, "state": after.state},
        "recovered": {t: before.excluded_tickers.get(t, 0) - after.excluded_tickers.get(t, 0)
                      for t in list(BACKTEST_ALIASES) + list(BACKTEST_UNPRICED)},
        "excluded_after": after.excluded_tickers,
    }
    json.dump(result, open(args.out, "w"), indent=1)
    print(json.dumps({k: result[k] for k in ("before", "after", "recovered")}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
