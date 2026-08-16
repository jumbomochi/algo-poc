# KAN-19 — Place GTC stops at IB, sized by the IPS stop rule

Implementation plan. Issue: https://huiliang.atlassian.net/browse/KAN-19

## What the spike (KAN-18) settled, and what each finding forces here

| Spike finding | Consequence for this story |
|---|---|
| Q2 GO — a GTC stop survived a Gateway process restart unchanged | Build it. GTC is the TIF. |
| Q1 — `outsideRth` false by default ⇒ the stop is dormant outside RTH | `outside_rth` is an explicit, configurable parameter, not an inherited default. |
| Q3 — `orderType`, `auxPrice`, `tif` are populated but `BrokerOpenOrder` drops them | Capture the three (AC5). `trailingPercent`/`trailStopPrice` deliberately not captured — both are traps on a plain STP. |
| Q4 — a foreign-client stop survives `cancel_all_orders()` and then sells short | Stops are placed **by the execution client** and tracked in `OrderManager.open_orders`, so the kill path's existing cancel-all reaches them *before* liquidation. |
| "stops must be ledgered" — an unledgered stop makes reconcile `major` and disables entries every day | Every stop gets an `OrderIntent` (AC6). |
| The placing client sees its own stop through plain `openTrades()` after a restart | No new recovery machinery needed. |

Superseded: the spike's earlier suggestion to extend `ApprovedOrderMessage.order_type`
with `"stop"`. Reconcile compares broker orders against the **ledger**, not the stream,
so an `OrderIntent` row satisfies it. KAN-19 decision 5 stands — stops bypass
`stream:approved_orders`.

## Steps (each is one TDD cycle)

1. `BrokerOpenOrder` gains `order_type`, `aux_price`, `tif` (defaulted, so no caller breaks);
   `ib_account.py` populates them from the trade. — AC5
2. `IBExecutor.submit_stop_order(ticker, quantity, stop_price, recommendation_id, *, tif="GTC",
   outside_rth=False)` + the protocol entry. New method, not a parameter on the existing two:
   both hardcode their action and `test_partial_fills.py` pins `submit_limit_order`'s
   positional signature.
3. `OrderLedger.open_stop_quantity(account_id, portfolio, con_id)` — unfilled quantity of
   nonterminal `order_type="stop"` intents. This is the coverage query AC3 is asserted on.
4. `OrderManager.submit_stop(...)` — submits and tracks in `open_orders` with
   `order_type="stop"`, so cancel-all reaches it.
5. `check_unfilled_orders` skips `order_type="stop"` as it already skips `"market"`. A GTC
   stop must not be cancelled 15 minutes before the close. Skipping by an explicit
   `("market", "stop")` set rather than `!= "limit"`, because `restore_submission` writes
   entries with no `order_type` key at all and those must keep being swept.
6. `services/execution/broker_stops.py` — `BrokerStopManager`: IPS stop price, shortfall
   sizing against step 3's coverage query, ledger row PROPOSED→APPROVED→SUBMITTED.
7. Runner wiring: placement on position open (buy fill, `order_done`) and
   `backfill_open_positions()` at startup. — AC1, AC2
8. Config: `execution.broker_stops_enabled: false` (+ `_tif`, `_outside_rth`). — AC4

## Sizing rule

Stop price = `reference_price * (1 - risk.stop_loss_trailing_pct / 100)`, rounded to a
penny — the same IPS rule `RiskEngine.check_stop_loss` applies, evaluated against the
same `highest_price_since_entry`. On open the reference is the fill price (the high so
far); on backfill it is `Position.highest_price_since_entry`.

Quantity = `target_quantity - already_covered`, so the sum over a
`{account, portfolio, con_id}` equals the held quantity and never exceeds it (AC3).
Target is the runner's live position view on open, and `Position.quantity` on backfill.

## Out of scope

Verification and adjustment of a resting stop (KAN-20). Retiring the software stop path —
it stays as the fallback and handles trim/kill exits a stop cannot express. Persisting
`permId` alongside `ib_order_id`: it needs a migration and belongs with the verification
reader that would consume it (noted on KAN-20).

## Rollback

Set `broker_stops_enabled: false`. **Resting stops already at IB are not removed by
turning the flag off, nor by reverting the code** — they must be cancelled explicitly at
the broker, or the account is left with orphan protective orders no code knows about.
