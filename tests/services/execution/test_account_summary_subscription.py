"""The account-summary subscription must be released, not left open — KAN-79.

WHAT WAS OBSERVED
-----------------
``paper_trading_20260909.log`` carries 69 of these::

    Error 1100, reqId -1: Connectivity between IBKR and Trader Workstation has
      been lost.
    Error 1102, reqId -1: Connectivity ... has been restored - data maintained.
    Error 322, reqId 51: Error processing request.-'b1' : cause - Maximum
      number of account summary requests exceeded; desubscribe to previous
      request first

alongside 38 Error 1102s. ``backtest_refresh_20260908.log`` has 52 more.

WHAT THE LOG ACTUALLY SHOWS, WHICH IS NOT QUITE WHAT THE TICKET ASSUMED
-----------------------------------------------------------------------
The ticket read the climbing reqIds (212, 213, 214 ...) as one subscription
being re-issued per reconnect. Two things falsify that reading:

1. ``IB.accountSummaryAsync()`` in ib_insync 0.9.86 only issues a request when
   ``wrapper.acctSummary`` is empty — "loaded on demand since it takes ca.
   250 ms". Within one connection it is a cache read, not a re-subscribe. And
   this repo takes exactly one snapshot per process, on a connection it opens
   and closes in a ``finally``.
2. ``Client.getReqId()`` counts from the ``nextValidId`` IB hands out, which is
   monotonic across connections for the gateway session. Climbing reqIds are
   therefore expected from *separate* processes and prove nothing.

The decisive detail is that the **same** reqId comes back refused more than
once — 51 at both lines 82 and 126, 79 at both 128 and 163 — each time
immediately after an Error 1102. So the subscriptions are held by the GATEWAY,
survive the disconnect of the process that made them, and are replayed to IBKR
on every reconnect; IBKR refuses each replay because the original registration
is still live. The set grows by one for every snapshot any process takes, and
each 1102 then produces one refusal per accumulated subscription. That is why
38 reconnects yielded 69 refusals and why later requests in a run fare worse
than earlier ones.

The conclusion the ticket reached is unchanged and correct: nothing ever
desubscribes. The fix is the same. What changes is where the leak lives — it is
per gateway session, not per process — so nothing inside a single run can
observe it, and only an explicit cancel before disconnect prevents it.

WHY THIS MATTERS BEYOND THE NOISE
---------------------------------
The summary is the money path: NetLiquidation, the FX rate and settled USD, i.e.

    Broker NAV: SGD 1,001,207.39 (USD 791,468.29); FX: 1.2650000 SGD/USD;
    settled USD: 90,875.59; deployable USD: 100,000.00

Once IB refuses the request the reader gets an EMPTY summary, because
ib_insync's ``Wrapper.error`` ends the pending request's future with no result
when ``RaiseRequestErrors`` is off (it is). Before this change that surfaced as
``expected exactly one SGD NetLiquidation value`` — technically a failure, but
one that names the wrong thing and reads like malformed data rather than a
refused subscription. A capital figure that silently stops refreshing is
precisely what the money-path tranche exists to prevent.

WHY THE READER TALKS TO client/wrapper AND NOT TO IB.accountSummaryAsync
------------------------------------------------------------------------
``IB`` exposes no ``cancelAccountSummary`` at all, and ``accountSummaryAsync``
discards the reqId it allocated — so there is no way to release what it opened.
``Client.cancelAccountSummary(reqId)`` does exist. Issuing the request at that
level is the only way to hold the reqId needed to cancel it.
"""

from __future__ import annotations

import pytest

from services.execution.ib_account import (
    ACCOUNT_SUMMARY_TAGS,
    AccountSummaryRefusedError,
    AccountValidationError,
    read_account_summary,
)
from tests.services.execution.fake_ib_gateway import REFUSAL_322, FakeGateway


@pytest.mark.asyncio
async def test_a_snapshot_releases_the_subscription_it_opened():
    """AC1. The subscription outlives the process that opened it unless this
    cancel is sent, so 'the connection is closed anyway' is not a defence."""
    gateway = FakeGateway()
    rows = await read_account_summary(gateway)

    assert {r.tag for r in rows} == {
        "NetLiquidation",
        "ExchangeRate",
        "TotalCashBalance",
    }
    assert gateway.cancelled == gateway.requested, (
        f"requested {gateway.requested}, cancelled {gateway.cancelled}"
    )
    assert gateway.open_subscriptions == set()


@pytest.mark.asyncio
async def test_the_cancel_still_happens_when_the_request_times_out():
    """AC1, the failure path. A request IB never answers may still have
    registered, and abandoning it is exactly how the leak accumulates."""
    gateway = FakeGateway(answer=False)
    with pytest.raises(AccountValidationError) as exc:
        await read_account_summary(gateway, timeout=0.05)

    assert "account summary" in str(exc.value).lower()
    assert gateway.cancelled == gateway.requested
    assert gateway.open_subscriptions == set()


@pytest.mark.asyncio
async def test_repeated_snapshots_never_hold_more_than_one_subscription():
    """AC2. The 2026-09-09 shape: enough snapshots without a cancel and IB
    starts refusing. With the cancel, an unbounded number of snapshots holds at
    most one subscription at a time — so the cap is never approached.
    """
    gateway = FakeGateway(cap=2)
    peak = 0
    for _ in range(10):
        await read_account_summary(gateway)
        peak = max(peak, len(gateway.open_subscriptions))

    assert peak <= 1, f"held {peak} concurrent subscriptions"
    assert len(gateway.requested) == 10
    assert gateway.cancelled == gateway.requested


@pytest.mark.asyncio
async def test_repeated_snapshots_without_the_cancel_would_hit_the_cap():
    """The fake must be able to fail, or the test above proves nothing.

    This drives the gateway directly, skipping the reader, to show that the cap
    is genuinely enforced and that the refusal the reader defends against is
    reachable.
    """
    gateway = FakeGateway(cap=2)
    for _ in range(2):
        gateway.client.reqAccountSummary(gateway.client.getReqId(), "All", "x")
    refusals: list[int] = []
    gateway.errorEvent += lambda req_id, code, msg, _c: refusals.append(code)
    gateway.client.reqAccountSummary(gateway.client.getReqId(), "All", "x")

    assert refusals == [322]


@pytest.mark.asyncio
async def test_a_refusal_is_a_named_failure_not_an_empty_summary():
    """AC3. ib_insync ends the refused request's future with NO rows, so
    without this the caller sees 'expected exactly one SGD NetLiquidation
    value' — a message that blames the data for a refused subscription and
    sends whoever reads it looking in the wrong place.
    """
    gateway = FakeGateway(cap=0)
    with pytest.raises(AccountSummaryRefusedError) as exc:
        await read_account_summary(gateway)

    message = str(exc.value)
    assert "322" in message, message
    assert "desubscribe" in message or "subscription" in message.lower(), message


@pytest.mark.asyncio
async def test_a_refused_request_is_not_cancelled():
    """Nothing was registered, so a cancel would draw an Error 300 ("can't find
    EId") and add a second misleading line to the log for the same event."""
    gateway = FakeGateway(cap=0)
    with pytest.raises(AccountSummaryRefusedError):
        await read_account_summary(gateway)

    assert gateway.cancelled == []


@pytest.mark.asyncio
async def test_the_error_handler_is_removed_after_the_request():
    """The reader subscribes to errorEvent for the life of one request. Leaving
    the handler attached would make the NEXT request raise on the PREVIOUS
    one's refusal, and on a long-lived IB object they would pile up."""
    gateway = FakeGateway()
    await read_account_summary(gateway)
    assert gateway.errorEvent.handlers == []


@pytest.mark.asyncio
async def test_an_unrelated_reqids_322_does_not_fail_this_request():
    """The refusals in the log arrive out of band, against reqIds belonging to
    processes that have long since exited. Only our own reqId is ours to fail
    on."""
    gateway = FakeGateway()

    original = gateway.client.reqAccountSummary

    def _also_refuse_someone_else(req_id, group, tags):
        gateway.errorEvent.emit(4242, 322, REFUSAL_322, None)
        original(req_id, group, tags)

    gateway.client.reqAccountSummary = _also_refuse_someone_else
    rows = await read_account_summary(gateway)
    assert len(rows) == 3


@pytest.mark.asyncio
async def test_the_request_asks_for_the_tags_the_snapshot_reads():
    """The snapshot needs NetLiquidation in the base currency and the $LEDGER
    per-currency rows that carry ExchangeRate and TotalCashBalance. ib_insync
    asks for all 33 tags; this asks for what is read."""
    gateway = FakeGateway()
    await read_account_summary(gateway)

    assert gateway.last_group == "All"
    assert gateway.last_tags == ACCOUNT_SUMMARY_TAGS
    assert "NetLiquidation" in ACCOUNT_SUMMARY_TAGS
    assert "$LEDGER:ALL" in ACCOUNT_SUMMARY_TAGS
