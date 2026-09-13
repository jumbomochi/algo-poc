"""A fake IB gateway that enforces the account-summary subscription cap.

Shared by the reader's tests and the subscription-lifecycle tests (KAN-79). The
cap is the point: a fake that accepts unlimited subscriptions cannot tell a
released one from a leaked one, and every assertion about the leak would pass
just as well against the code that leaked.

Modelled on ib_insync 0.9.86 — ``Client.reqAccountSummary(reqId, group, tags)``
opens a SUBSCRIPTION, rows land in ``Wrapper.acctSummary``, a refusal ends the
pending future with NO rows, and only ``Client.cancelAccountSummary(reqId)``
releases it. There is no ``IB.cancelAccountSummary``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

REFUSAL_322 = (
    "Error processing request.-'b1' : cause - Maximum number of account "
    "summary requests exceeded; desubscribe to previous request first"
)


def default_rows(account_id: str = "DUN551088"):
    return [
        SimpleNamespace(
            account=account_id, tag="NetLiquidation", value="1001757.23", currency="SGD"
        ),
        SimpleNamespace(
            account="All", tag="ExchangeRate", value="1.2928304", currency="USD"
        ),
        SimpleNamespace(
            account="All", tag="TotalCashBalance", value="-4711.26", currency="USD"
        ),
    ]


class _Event:
    """ib_insync's Event, to the extent the reader uses it."""

    def __init__(self) -> None:
        self.handlers: list = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def __isub__(self, handler):
        self.handlers.remove(handler)
        return self

    def emit(self, *args) -> None:
        for handler in list(self.handlers):
            handler(*args)


class FakeGateway:
    """A gateway that enforces IB's account-summary cap, like the real one.

    The cap is the whole point: a fake that accepts unlimited subscriptions
    cannot tell a released one from a leaked one, and every assertion about
    the leak would pass against the unfixed code.
    """

    def __init__(self, *, cap: int = 2, rows=None, answer: bool = True) -> None:
        self.cap = cap
        self.answer = answer
        self.rows = rows if rows is not None else default_rows()
        self.next_req_id = 51
        self.open_subscriptions: set[int] = set()
        self.requested: list[int] = []
        self.cancelled: list[int] = []
        self.acctSummary: dict = {}
        self.errorEvent = _Event()
        self._futures: dict[int, asyncio.Future] = {}

        gateway = self

        class _Client:
            def getReqId(self) -> int:
                req_id = gateway.next_req_id
                gateway.next_req_id += 1
                return req_id

            def reqAccountSummary(self, req_id, group, tags) -> None:
                gateway.requested.append(req_id)
                gateway.last_group = group
                gateway.last_tags = tags
                if len(gateway.open_subscriptions) >= gateway.cap:
                    # Exactly what IB does: refuse, and end the pending request
                    # with no rows (ib_insync's Wrapper.error path with
                    # RaiseRequestErrors off).
                    gateway.errorEvent.emit(req_id, 322, REFUSAL_322, None)
                    gateway._settle(req_id, [])
                    return
                gateway.open_subscriptions.add(req_id)
                if not gateway.answer:
                    return  # a request IB never answers
                gateway.acctSummary = {
                    (r.account, r.tag, r.currency): r for r in gateway.rows
                }
                gateway._settle(req_id, [])

            def cancelAccountSummary(self, req_id) -> None:
                gateway.cancelled.append(req_id)
                gateway.open_subscriptions.discard(req_id)

        class _Wrapper:
            def startReq(self, req_id):
                future: asyncio.Future = asyncio.get_event_loop().create_future()
                gateway._futures[req_id] = future
                return future

            @property
            def acctSummary(self):
                return gateway.acctSummary

        self.client = _Client()
        self.wrapper = _Wrapper()

    def _settle(self, req_id, result) -> None:
        future = self._futures.pop(req_id, None)
        if future is not None and not future.done():
            future.set_result(result)


def _rows(account_id: str = "DUN551088"):
    return [
        SimpleNamespace(
            account=account_id, tag="NetLiquidation", value="1001757.23", currency="SGD"
        ),
        SimpleNamespace(
            account="All", tag="ExchangeRate", value="1.2928304", currency="USD"
        ),
        SimpleNamespace(
            account="All", tag="TotalCashBalance", value="-4711.26", currency="USD"
        ),
    ]
