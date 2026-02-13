from __future__ import annotations

import asyncio
from datetime import date
from typing import Any, Protocol


class IBClientProtocol(Protocol):
    async def get_daily_bars(self, ticker: str, start: date, end: date) -> list[dict[str, Any]]: ...
    async def get_fundamentals(self, ticker: str) -> dict[str, Any]: ...
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...


class IBClient:
    """Wrapper around ib_insync for market data."""

    def __init__(self, host: str, port: int, client_id: int):
        self._host = host
        self._port = port
        self._client_id = client_id
        self._ib = None

    async def connect(self) -> None:
        from ib_insync import IB
        self._ib = IB()
        await self._ib.connectAsync(self._host, self._port, clientId=self._client_id)

    async def disconnect(self) -> None:
        if self._ib:
            self._ib.disconnect()

    async def get_daily_bars(self, ticker: str, start: date, end: date) -> list[dict[str, Any]]:
        from ib_insync import Stock
        contract = Stock(ticker, "SMART", "USD")
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
        from ib_insync import Stock
        contract = Stock(ticker, "SMART", "USD")
        data = await self._ib.reqFundamentalDataAsync(contract, reportType="ReportSnapshot")
        return {"raw": data}
