"""KAN-120: what IB serves for CMCSA and RJF around their splits. clientId 77, read-only."""
from __future__ import annotations
import asyncio, json, sys
asyncio.set_event_loop(asyncio.new_event_loop())
from ib_insync import IB, Stock

CASES = {"CMCSA": ["20170301", "20171013"], "RJF": ["20211001", "20211013"]}
out = {}
ib = IB(); ib.connect("127.0.0.1", 7497, clientId=77, timeout=15)
try:
    for t, ends in CASES.items():
        c = Stock(t, "SMART", "USD"); ib.qualifyContracts(c)
        out[t] = {"conId": c.conId}
        for what in ("TRADES", "ADJUSTED_LAST"):
            for end in ends:
                dur = "1 M" if end.endswith("01") else "1 Y"
                if what == "ADJUSTED_LAST":
                    end_s = ""  # IB requires an empty end for ADJUSTED_LAST
                    dur = "10 Y"
                else:
                    end_s = end + " 23:59:59"
                key = f"{what}:{end_s or 'now'}:{dur}"
                if key in out[t]: continue
                bars = ib.reqHistoricalData(c, endDateTime=end_s, durationStr=dur, barSizeSetting="1 day",
                                            whatToShow=what, useRTH=True, formatDate=1)
                out[t][key] = [dict(date=str(b.date), open=b.open, high=b.high, low=b.low, close=b.close, volume=b.volume) for b in bars]
                print(t, key, len(bars), flush=True)
                ib.sleep(1)
finally:
    ib.disconnect()
    json.dump(out, open(sys.argv[1], "w"))
