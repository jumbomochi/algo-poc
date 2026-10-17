"""KAN-119 probe 3: what IB's symbol search returns for the same-ticker names
that answer Error 200. Read-only, clientId 77."""
import asyncio
import json
import sys

asyncio.set_event_loop(asyncio.new_event_loop())
from ib_insync import IB  # noqa: E402  (needs the loop above)
QUERIES = {
    "AVB": "AvalonBay", "EQR": "Equity Residential", "LEG": "Leggett", "SEE": "Sealed Air",
    "K": "Kellanova", "EA": "Electronic Arts", "HOLX": "Hologic", "IPG": "Interpublic",
    "CTRA": "Coterra", "DAY": "Dayforce", "QRVO": "Qorvo", "WBD": "Warner Bros",
    "PSKY": "Paramount", "SATS": "EchoStar", "BK": "Bank of New York",
}
ib = IB()
ib.connect("127.0.0.1", 7497, clientId=77, timeout=15)
out = {}
try:
    for t, q in QUERIES.items():
        found = ib.reqMatchingSymbols(q) or []
        rows = [{"symbol": d.contract.symbol, "conId": d.contract.conId,
                 "primaryExchange": d.contract.primaryExchange, "secType": d.contract.secType,
                 "currency": d.contract.currency, "desc": d.contract.description if hasattr(d.contract, "description") else None}
                for d in found if d.contract.secType == "STK"]
        out[t] = rows
        print(t, q, [(r["symbol"], r["primaryExchange"], r["currency"], r["conId"]) for r in rows][:6], flush=True)
        ib.sleep(1.5)
finally:
    ib.disconnect()
    json.dump(out, open(sys.argv[1], "w"), indent=1)
