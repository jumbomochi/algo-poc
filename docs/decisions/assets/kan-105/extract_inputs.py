"""KAN-105: extract the re-grade's inputs. READ ONLY.

Reads
-----
1. The weekly refresh artifact (``backtest_multi_*.json``, default the
   2026-10-06 one in the deploy clone's ``output/``): the IB TRADES/RTH bars
   for the paper run's union universe. Opened for reading, never moved.
2. The paper database, through ``ALGO_DATABASE_URL``, in a session opened with
   ``default_transaction_read_only=on`` and ``SET TRANSACTION READ ONLY``:
   ``ohlcv_daily`` rows after the artifact's last bar (to extend the bars to
   the latest reproducible session), ``equity_snapshots`` and
   ``capital_adjustments``. SELECT only.

Writes ``bars.pkl``, ``snapshots.json`` and ``capital_adjustments.json`` into
``--out``, which must not be under any ``output/`` directory (that tree is the
book's evidence and is never written by an investigation).

Run from the repo root, with the password loaded from the keychain mirror in
the same shell and never echoed::

    source deploy/launchd/secrets.sh; algo_load_secrets POSTGRES_PASSWORD; \\
    ALGO_DATABASE_URL="postgresql://algo:${POSTGRES_PASSWORD}@localhost:55432/algo_poc" \\
    PYTHONPATH=. python docs/decisions/assets/kan-105/extract_inputs.py --out /tmp/kan105
"""
from __future__ import annotations

import argparse
import json
import os
import pickle
import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO))

DEFAULT_ARTIFACT = os.path.expanduser(
    "~/algo-poc-deploy/output/backtest_multi_20261006_124013.json"
)


def _query(sql: str) -> list[dict]:
    from sqlalchemy import create_engine, text

    engine = create_engine(
        os.environ["ALGO_DATABASE_URL"],
        connect_args={"options": "-c default_transaction_read_only=on"},
    )
    with engine.connect() as conn:
        conn.execute(text("SET TRANSACTION READ ONLY"))
        return [dict(r._mapping) for r in conn.execute(text(sql))]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--artifact", default=DEFAULT_ARTIFACT)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    out = Path(args.out).resolve()
    if "output" in out.parts:
        sys.exit(f"refusing to write under an output/ tree: {out}")
    out.mkdir(parents=True, exist_ok=True)

    from scripts.run_backtest import get_union_universe
    from scripts.run_paper import CAPITAL_ALLOCATIONS

    with open(args.artifact) as fh:
        artifact = json.load(fh)
    tickers = get_union_universe(list(CAPITAL_ALLOCATIONS))
    bars: dict[str, list[dict]] = {}
    for ticker in tickers:
        raw = artifact["bars"].get(ticker) or []
        bars[ticker] = [
            {**b, "date": date.fromisoformat(str(b["date"])[:10])} for b in raw
        ]
    last = max(b[-1]["date"] for b in bars.values() if b)

    # Sessions after the artifact's last bar, from the book's own bar store.
    tail = _query(
        "SELECT ticker, date, open, high, low, close, volume FROM ohlcv_daily "
        f"WHERE date > '{last.isoformat()}' ORDER BY ticker, date"
    )
    extended = 0
    for row in tail:
        rows = bars.get(row["ticker"])
        if rows is None:
            continue
        if rows and row["date"] <= rows[-1]["date"]:
            continue
        rows.append({k: row[k] for k in ("date", "open", "high", "low",
                                         "close", "volume")})
        extended += 1
    with open(out / "bars.pkl", "wb") as fh:
        pickle.dump(bars, fh)

    snapshots = _query(
        "SELECT portfolio, date, session_date, equity, cash, market_value, "
        "created_at, valuation_at FROM equity_snapshots ORDER BY portfolio, date"
    )
    (out / "snapshots.json").write_text(json.dumps(snapshots, default=str))
    flows = _query("SELECT * FROM capital_adjustments ORDER BY created_at, id")
    (out / "capital_adjustments.json").write_text(json.dumps(flows, default=str))

    newest = max(b[-1]["date"] for b in bars.values() if b)
    print(f"bars: {len(bars)} tickers, artifact through {last}, "
          f"+{extended} ohlcv_daily bars, newest {newest}")
    print(f"equity_snapshots: {len(snapshots)} rows; "
          f"capital_adjustments: {len(flows)} rows")


if __name__ == "__main__":
    main()
