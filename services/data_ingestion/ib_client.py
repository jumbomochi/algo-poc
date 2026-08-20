from __future__ import annotations

import asyncio
from datetime import date
from typing import Any, Protocol


# A connect that never returns wedges the poll loop, which is the failure the
# heartbeat exists to expose — better not to have it. Matches the bound
# scripts/ops/broker_stop_spike.py uses.
CONNECT_TIMEOUT_SECONDS = 20


class IBClientProtocol(Protocol):
    async def get_daily_bars(self, ticker: str, start: date, end: date) -> list[dict[str, Any]]: ...
    async def get_fundamentals(self, ticker: str) -> dict[str, Any]: ...
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    def is_connected(self) -> bool: ...


class IBClient:
    """Wrapper around ib_insync for market data.

    Connects **read-only**: this service reads bars and fundamentals and has no
    business placing, modifying or cancelling anything. A read-only session
    means the gateway itself refuses order operations from this client, rather
    than the guarantee resting on this code never attempting one. Order flow
    goes through ``services/execution/ib_executor.py`` on a different client id.
    """

    def __init__(self, host: str, port: int, client_id: int):
        self._host = host
        self._port = port
        self._client_id = client_id
        self._ib = None

    async def connect(self) -> None:
        from ib_insync import IB
        ib = IB()
        try:
            await ib.connectAsync(
                self._host,
                self._port,
                clientId=self._client_id,
                readonly=True,
                timeout=CONNECT_TIMEOUT_SECONDS,
            )
        except BaseException:
            # Do not keep a half-built handle: `is_connected()` must report
            # False so the caller retries, rather than a later request raising
            # on a socket that never came up.
            self._ib = None
            raise
        self._ib = ib

    def is_connected(self) -> bool:
        return self._ib is not None and bool(self._ib.isConnected())

    async def disconnect(self) -> None:
        if self._ib:
            self._ib.disconnect()
            self._ib = None

    async def get_daily_bars(self, ticker: str, start: date, end: date) -> list[dict[str, Any]]:
        from shared.universe import make_stock_contract
        contract = make_stock_contract(ticker)
        bars = await self._ib.reqHistoricalDataAsync(
            contract, endDateTime=end.strftime("%Y%m%d 23:59:59"),
            durationStr=f"{(end - start).days + 1} D",
            barSizeSetting="1 day", whatToShow="TRADES", useRTH=True,
        )
        return [
            {"date": b.date, "open": b.open, "high": b.high, "low": b.low, "close": b.close, "volume": b.volume}
            for b in bars
        ]

    async def get_fundamentals(self, ticker: str) -> dict[str, Any]:
        from shared.universe import make_stock_contract
        contract = make_stock_contract(ticker)
        data = await self._ib.reqFundamentalDataAsync(contract, reportType="ReportSnapshot")
        return {"raw": data}
