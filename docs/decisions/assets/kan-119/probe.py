#!/usr/bin/env python3
"""KAN-119 evidence probe: what IB can price for each excluded membership ticker.

Read-only. Connects to the paper Gateway as clientId 77 (unused by every job)
and, for each ticker the backtest's coverage measure excluded, records:

* ``reqContractDetails`` for the bare symbol on SMART/USD: conId, longName,
  primaryExchange — or nothing (IB's Error 200);
* ``reqHeadTimeStamp`` (TRADES, RTH) for whatever resolved: the first date IB
  serves history for that conId;
* the same two for a CANDIDATE successor symbol, where the ticker is believed
  to have been renamed (``CANDIDATES``; a belief to check, not a fact).

Run it outside every job's IB window (not 05:10-06:30 SGT, not 13:50-14:15):

    PYTHONPATH=. python docs/decisions/assets/kan-119/probe.py \
        --excluded excluded.json --out probe_results.json

``excluded.json`` is a list of ``{"ticker": ..., "first": ..., "last": ...}``
(membership snapshot dates), as written by the KAN-119 working notes.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime

#: Old membership ticker -> the symbol the same company is believed to trade
#: under now. Every entry is verified by the probe's output, not by this list.
CANDIDATES = {
    "FB": "META", "ANTM": "ELV", "PCLN": "BKNG", "ABC": "COR", "PKI": "RVTY",
    "RE": "EG", "FLT": "CPAY", "WLTW": "WTW", "HCN": "WELL", "HCP": "DOC",
    "PEAK": "DOC", "CBG": "CBRE", "COG": "CTRA", "JEC": "J", "TMK": "GL",
    "HRS": "LHX", "SYMC": "GEN", "NLOK": "GEN", "KORS": "CPRI", "COH": "TPR",
    "DPS": "KDP", "BLL": "BALL", "DISCA": "WBD", "DISCK": "WBD", "CTL": "LUMN",
    "HFC": "DINO", "CDAY": "DAY", "UTX": "RTX", "ARNC": "HWM", "BHGE": "BKR",
    "DWDP": "DD", "CBS": "PARA", "VIAC": "PARA", "LUK": "JEF", "DLPH": "APTV",
    "GPS": "GAP", "LB": "BBWI", "ADS": "BFH", "FBHS": "FBIN", "WYN": "TNL",
    "TSO": "ANDV", "BK": "BNY", "Q": "IQV", "UA C": "UA", "MYL": "VTRS",
    "BHI": "BKR", "YHOO": "AABA", "DOW": "DOW", "DD": "DD", "IR": "TT",
    "PARA": "PSKY", "XEC": "CTRA", "STI": "TFC", "PX": "LIN", "CHK": "EXE",
}


def _details(ib, Stock, symbol: str) -> list[dict]:
    try:
        found = ib.reqContractDetails(Stock(symbol, "SMART", "USD"))
    except Exception as exc:  # noqa: BLE001 — recorded, the probe carries on
        return [{"error": str(exc)}]
    return [
        {
            "conId": d.contract.conId,
            "symbol": d.contract.symbol,
            "primaryExchange": d.contract.primaryExchange,
            "longName": d.longName,
            "stockType": getattr(d, "stockType", None),
        }
        for d in found
    ]


def _head(ib, Stock, con_id: int) -> str | None:
    try:
        contract = Stock(conId=con_id, exchange="SMART", currency="USD")
        ib.qualifyContracts(contract)
        stamp = ib.reqHeadTimeStamp(contract, whatToShow="TRADES", useRTH=True, formatDate=1)
    except Exception as exc:  # noqa: BLE001
        return f"error: {exc}"
    if not stamp:
        return None
    return stamp.date().isoformat() if isinstance(stamp, datetime) else str(stamp)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--excluded", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--port", type=int, default=7497)
    parser.add_argument("--client-id", type=int, default=77)
    args = parser.parse_args()

    import asyncio

    asyncio.set_event_loop(asyncio.new_event_loop())
    from ib_insync import IB, Stock

    rows = json.load(open(args.excluded))
    ib = IB()
    ib.connect("127.0.0.1", args.port, clientId=args.client_id, timeout=15)
    results = []
    try:
        for i, row in enumerate(rows, 1):
            ticker = row["ticker"]
            symbol = ticker
            entry = {**row, "own": _details(ib, Stock, symbol)}
            for d in entry["own"]:
                if "conId" in d:
                    d["head"] = _head(ib, Stock, d["conId"])
            successor = CANDIDATES.get(ticker)
            if successor and successor != ticker:
                entry["candidate"] = successor
                entry["candidate_details"] = _details(ib, Stock, successor)
                for d in entry["candidate_details"]:
                    if "conId" in d:
                        d["head"] = _head(ib, Stock, d["conId"])
            results.append(entry)
            own = entry["own"][0] if entry["own"] else {}
            print(f"[{i}/{len(rows)}] {ticker}: own={own.get('conId')} {own.get('longName')} "
                  f"head={own.get('head')} cand={successor} "
                  f"{[(d.get('conId'), d.get('head')) for d in entry.get('candidate_details', [])]}",
                  flush=True)
            ib.sleep(0.5)
    finally:
        ib.disconnect()
        json.dump(results, open(args.out, "w"), indent=1, default=str)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
