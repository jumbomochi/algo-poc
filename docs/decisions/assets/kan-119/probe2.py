#!/usr/bin/env python3
"""KAN-119 probe 2: where a still-listed ticker's 10-year series has holes, and why.

For each ticker, requests the same ten 1-year chunks the backtest does and
records the bar count of each. A chunk that comes back empty is retried as
monthly chunks, to tell a real data hole from a 1-year request IB will not
serve (hypothesis: the chunk spanning a NYSE -> NASDAQ listing transfer).
clientId 77, read-only.
"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta

TICKERS = sys.argv[1].split(",")
OUT = sys.argv[2]


def main() -> int:
    import asyncio

    asyncio.set_event_loop(asyncio.new_event_loop())
    from ib_insync import IB, Stock

    ib = IB()
    ib.connect("127.0.0.1", 7497, clientId=77, timeout=15)
    end = date.today()
    out = {}
    try:
        for ticker in TICKERS:
            contract = Stock(ticker, "SMART", "USD")
            if not ib.qualifyContracts(contract):
                out[ticker] = {"error": "no contract"}
                continue
            chunks = []
            chunk_end = end
            for _ in range(10):
                bars = ib.reqHistoricalData(contract, endDateTime=chunk_end.strftime("%Y%m%d 23:59:59"),
                                            durationStr="1 Y", barSizeSetting="1 day", whatToShow="TRADES",
                                            useRTH=True, formatDate=1)
                entry = {"end": chunk_end.isoformat(), "bars": len(bars),
                         "first": str(bars[0].date) if bars else None}
                if not bars:
                    monthly = []
                    m_end = chunk_end
                    for _ in range(12):
                        mb = ib.reqHistoricalData(contract, endDateTime=m_end.strftime("%Y%m%d 23:59:59"),
                                                  durationStr="1 M", barSizeSetting="1 day",
                                                  whatToShow="TRADES", useRTH=True, formatDate=1)
                        monthly.append({"end": m_end.isoformat(), "bars": len(mb)})
                        m_end -= timedelta(days=30)
                        ib.sleep(1.0)
                    entry["monthly"] = monthly
                chunks.append(entry)
                chunk_end -= timedelta(days=365)
                ib.sleep(1.0)
            out[ticker] = {"conId": contract.conId, "primaryExchange": contract.primaryExchange,
                           "chunks": chunks}
            print(ticker, [(c["end"], c["bars"], sum(m["bars"] for m in c.get("monthly", [])) or None)
                           for c in chunks], flush=True)
    finally:
        ib.disconnect()
        json.dump(out, open(OUT, "w"), indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
