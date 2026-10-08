# Runbook: an order held because IB has one, or because IB is not answering (KAN-112)

Before it places anything, execution asks IB whether an order already exists
under the recommendation's `orderRef`. It asks for every client's open orders
(`reqAllOpenOrders`), then completed orders. The answer is one of four
outcomes, and three of them hold the order:

| Outcome | What execution does | Alert |
|---|---|---|
| Our own order (open, any status), or any client's completed order | adopts its id; nothing new is placed | none |
| **Another client** has one in a working state (`PendingSubmit`, `ApiPending`, `PreSubmitted`, `Submitted`) | holds ours: nothing placed, nothing bound, intent stays `APPROVED` | `order_submitted_elsewhere` (high), once per episode |
| IB does **not answer** (timeout, request error, or not connected and the reconnect failed) | holds ours: nothing placed, intent stays `APPROVED` | exits: `exit_submission_deferred` (high), once per episode. Entries: warning log only. Stops: `broker_stop_not_placed` (critical) |
| Nothing anywhere | places it | none |

A held approved order keeps its stream message unacked, in the PEL, so a
restart replays it. Execution retries it on its own:

- no answer: every 30 s (`DEFERRED_ORDER_RETRY_SECONDS`); a pass stops at the
  first order IB still doesn't answer;
- another client's order: every 5 min (`SUBMITTED_ELSEWHERE_RETRY_SECONDS`).

A held **BUY** gives up once the session it was sized for has closed. That is
the first NYSE close after its approval, so a BUY approved by the 04:15 SGT
run (after the US close) is sized for the next day. The intent then becomes
`SUBMISSION_FAILED` with reason `deferred past session`, and the message is
acked. A stale-priced entry is never placed hours late.

**Exits never expire.** A held stop stays `APPROVED`. It still counts as
coverage, and every verification scan re-drives it.

## `order_submitted_elsewhere`: another client has it working

The alert names the recommendation id, the other order's id, the other
client id, the ticker, the side and the submit path (`entry`, `exit`,
`kill_exit` or `stop`).

1. **Find who placed it.** The client id says who:
   - `58` / `59`: the paper run's snapshot and sweep clients (they normally place nothing);
   - `client_id + 10`: `scripts/ops/broker_stop_spike.py`;
   - `convert_paper_fx.py`: its own id;
   - `0`: someone in TWS by hand.

   Look at the order in TWS / Client Portal, using the order id and the
   `orderRef`.
2. **Decide which one should exist.**
   - **Theirs should not exist** (a leftover from a spike, a repair run that
     went wrong): cancel it from the client that placed it, or in TWS. Within
     5 minutes execution re-probes, sees nothing working, and places its own.
     It also adopts the cancelled order's id if IB still lists it in today's
     completed orders (see the caveat below).
   - **Theirs should stand** (a human deliberately placed the order for this
     recommendation): leave it. Execution keeps holding its own and re-probes
     every 5 min without paging again. Once that order is done, IB lists it
     as completed, and execution adopts its id as the intent's broker order.
     The intent becomes `SUBMITTED`, and the KAN-106 resolution reconciles its
     terminal status. Its **fills are not booked by execution**, because that
     order was never ours to attribute. Check the position with
     `scripts/ops/reconciliation_status.py` and repair with the
     `reconcile_paper.py` flow, as for any fill booked outside execution.
3. A held **BUY** also expires with its session (see above), so a blocked
   entry cannot hold a reservation forever.
4. **When a blocked BUY expires** (log line `Unsubmitted buy expired`,
   intent `SUBMISSION_FAILED` / `deferred past session`), execution stops
   watching it, but **the other client's order may still be working**. Our
   intent is finished; theirs is not. Look the order up at IB (order id and
   client id from the `order_submitted_elsewhere` page, or search by the
   `orderRef`):
   - still working and unwanted: cancel it at IB, from the client that
     placed it or in TWS. Nothing in execution will cancel it for you: it
     never tracked that order, and the halt sweep and cancel paths only
     reach execution's own client;
   - still working and wanted, or already filled: execution will never book
     its fills. Reconcile the position as in step 2 (`reconciliation_status.py`,
     then the `reconcile_paper.py` flow).

   The same applies to a BUY replayed from the PEL after a restart once its
   session has closed: it is failed and acked the same way, before any probe.

### Caveats
- **A changed `ib.client_id` makes our own orders look foreign.** "Ours"
  means `order.clientId == ib.client_id` (`IBExecutor._is_own`). After
  execution's client id changes, every order the previous id placed reads as
  "another client" and is held and paged here, not adopted. Change the id
  only with nothing working, or expect these pages for orders that are in
  fact ours. Resolve them as above: cancel and let execution re-place, or
  reconcile by hand.
- **Only working states block.** An `Inactive` order (rejected, held for a
  permission) or a `PendingCancel` one under our ref does not block, so
  execution places its own. The state is read from ib_insync's view of IB's
  answer; a foreign order is never updated live on this (non-master) client.
- **Completed history is any client's and is adopted** (unchanged since
  before KAN-112). Within the same IB day, a cancelled foreign order is
  adopted rather than replaced. The KAN-106 resolution then marks it
  cancelled, and for an exit, risk re-emits under the next sequence number.

## `exit_submission_deferred`: IB did not answer for an exit

This is not a duplicate; it is an outage. Check what the Gateway itself
reports (`ib_disconnected`, Error 1100/1101 pages, the watchdog). The exit
goes out on its own once IB answers. If IB stays silent and the position
must come off now, use the operator's tools on another session.

## The kill switch is different

`process_kill` sells every position once, each moments after cancelling that
position's stop, and never retries. So a kill's liquidation sell is
**submitted even when IB does not answer**:

- after the first unanswered probe, the rest of that kill's sells skip the
  probe, so a hung Gateway costs one 30 s bound per kill, not one per
  position;
- before any blind sell, execution checks its own state: what it is tracking
  and ib_insync's own-client open-order cache. A sell this session already
  holds is adopted, not doubled;
- a definite answer that another client holds a working order under the
  liquidation id still **blocks** that one sell (paged, path `kill_exit`).
  The kill's other sells and its critical alert go ahead.

**The remaining double-sell window:** a blind sell can double an order for
the same liquidation id when IB is not answering AND either:
- a previous execution process placed it, then crashed before its in-process
  map and the ledger recorded it, and connect's best-effort open-order sync
  did not cache it; or
- another client holds it.

The risk-side liquidation exit with the same id goes through the normal path:
it is held and retried, never sent blind.
