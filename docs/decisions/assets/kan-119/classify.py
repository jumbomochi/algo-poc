#!/usr/bin/env python3
"""KAN-119: build the per-ticker evidence table from the probe outputs.

Inputs (all in this directory): probe_results.json (contract details + head
timestamps), probe2_results.json (per-year chunk counts), probe3_results.json
(IB symbol search), verify.json (alias candidates fetched through the real
path). Writes classification.json and classification.md.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
from prior import ACQUIRED, ENTITY, FAILED, GAP, RENAMED, SAME_TICKER  # noqa: E402

probe = {r["ticker"]: r for r in json.loads((HERE / "probe_results.json").read_text())}
chunks = json.loads((HERE / "probe2_results.json").read_text())
search = json.loads((HERE / "probe3_results.json").read_text())
verify = json.loads((HERE / "verify.json").read_text()) if (HERE / "verify.json").exists() else {}
decided = json.loads((HERE / "decided.json").read_text()) if (HERE / "decided.json").exists() else {}


def own_text(r):
    own = r.get("own") or []
    if not own:
        return "Error 200"
    o = own[0]
    return f"{o['conId']} {o['longName']} (history from {o.get('head') or '?'})"


def first_served(t):
    c = chunks.get(t, {}).get("chunks") or []
    served = [x for x in c if x["bars"]]
    return min((x["first"] for x in served if x.get("first")), default=None)


rows = []
for t in sorted(probe):
    r = probe[t]
    base = {"ticker": t, "pinned_days": r["pinned_days"], "latest_days": r["latest_days"], "own": own_text(r)}
    if t in decided:
        base.update(decided[t])
    elif t in RENAMED:
        base.update(category="renamed, not recoverable", evidence=f"successor {RENAMED[t]}: see verify")
    elif t in SAME_TICKER:
        rows_s = search.get(t) or []
        on_value = [s for s in rows_s if s["primaryExchange"] == "VALUE" and s["currency"] == "USD"]
        if on_value:
            base.update(category="delisted per IB",
                        evidence=f"symbol search lists {t} only on VALUE (conId {on_value[0]['conId']})")
        elif first_served(t):
            base.update(category="history cut-off",
                        evidence=f"current conId served only from {first_served(t)}; earlier 1-year and 1-month requests return no data")
        else:
            base.update(category="same ticker, unresolved", evidence="see probe")
    elif t in ENTITY:
        base.update(category="different entity under the ticker", evidence=ENTITY[t])
    elif t in GAP:
        base.update(category="isolated missing session", evidence="priced; one membership session without a bar")
    elif t in FAILED:
        base.update(category="failed", evidence=f"bankruptcy / wind-down; IB: {own_text(r)}")
    elif t in ACQUIRED:
        base.update(category="acquired", evidence=f"IB: {own_text(r)}")
    rows.append(base)

(HERE / "classification.json").write_text(json.dumps(rows, indent=1))
cats = {}
for row in rows:
    c = cats.setdefault(row["category"], [0, 0, 0])
    c[0] += 1
    c[1] += row["pinned_days"]
    c[2] += row["latest_days"]
for k, v in sorted(cats.items(), key=lambda kv: -kv[1][2]):
    print(f"{k:40} {v[0]:4} pinned={v[1]:7} latest={v[2]:7}")

ORDER = [k for k, _ in sorted(cats.items(), key=lambda kv: -kv[1][2])]
md = ["| Ticker | Category | Days excluded (pinned / 10-13) | IB's own symbol, 2026-10-17 | Evidence |",
      "|---|---|---:|---|---|"]
for row in sorted(rows, key=lambda r: (ORDER.index(r["category"]), -r["latest_days"], r["ticker"])):
    ev = row["evidence"].replace("|", "/")
    md.append(f"| `{row['ticker']}` | {row['category']} | {row['pinned_days']:,} / {row['latest_days']:,} "
              f"| {row['own']} | {ev} |")
(HERE / "classification.md").write_text("\n".join(md) + "\n")
