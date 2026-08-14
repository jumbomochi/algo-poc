# Broker-Stop Prototype Spike (DUN551088)

**Status: harness landed, observations NOT yet recorded.** Sections marked
`OBSERVATION PENDING` are the operator's to fill from a real run; nothing below
is inferred from IB documentation and presented as evidence.

The readiness design's decision D16 makes broker-native GTC stops the *primary*
stop-loss protection: an order resting at IB survives Redis, Postgres, Docker,
and the host, which no software stop can claim. KAN-19 (placement) and KAN-20
(verifier + kill-path interaction) are built on that claim. This spike tests the
claim on the paper account before either is written.

**Harness:** `scripts/ops/broker_stop_spike.py`
**Tests:** `tests/ops/test_broker_stop_spike.py` (planning guards + evidence
extraction; the IB behaviour itself is only observable live)
**Issue:** [KAN-18](https://huiliang.atlassian.net/browse/KAN-18)

---

## What the repo does today (verified 2026-08-14 against `origin/develop`)

These are code facts, not spike observations. They set what the spike has to
find out and what KAN-19/KAN-20 will have to change.

| Fact | Evidence |
|---|---|
| Nothing places a stop order. `StopOrder`/`auxPrice`/`STP`/`TrailStop` appear nowhere in service code | only TIF usages are `tif="DAY"` at `services/execution/ib_executor.py:610,640` |
| `submit_limit_order` hardcodes `"BUY"`, `submit_market_order` hardcodes `"SELL"`; neither takes an action | `ib_executor.py:610`, `:640` |
| A stop cannot be expressed in the stream schema | `ApprovedOrderMessage.order_type: Literal["limit", "market"]`, `shared/schemas/messages.py:139` |
| The ledger *can* hold one | `OrderIntent.order_type = String(20)`, no constraint, `shared/models/order_ledger.py:63` |
| `BrokerOpenOrder` captures no stop parameters | `shared/broker_state.py:19-31` — account, order id, con_id, symbol, action, total/filled quantity, status. No `orderType`, `auxPrice`, `tif`, or `orderRef` |
| The account reader is account-wide | `IBAccountReader.snapshot()` uses `reqAllOpenOrdersAsync()`, `services/execution/ib_account.py:168` |
| Stop-loss today is a 30-min in-process scan emitting `order_type="market"` exits | `services/risk_management/runner.py:1147,1222,1268` |
| The IPS stop rule is a 15% trailing stop | `stop_loss_trailing_pct`, `config/default.yaml:70`, IPS § 6 |

Two consequences fall straight out of the code and shape the go/no-go
regardless of what the live run shows:

**(a) The kill path cannot reach an order it did not place.**
`process_kill` calls `OrderManager.cancel_all_orders()`
(`services/execution/runner.py:656`), which iterates the in-process
`OrderManager.open_orders` dict (`order_manager.py:429-443`). Underneath,
`IBExecutor.cancel_order` looks the order up in `self._trades` and returns
`False` without calling IB when it is absent (`ib_executor.py:655-666`), and
`find_order_by_ref` scans only `self._ib.openTrades()` — this client's
session — never `reqAllOpenOrders` (`ib_executor.py:506-509`). There is no
`reqGlobalCancel` anywhere in the repo.

So the risk is the *opposite* of the one the design anticipated: not that
cancel-all strips stop coverage mid-liquidation, but that a GTC stop placed
outside the execution client survives the kill, and then rests against a
position the liquidation already flattened. **A resting SELL stop on a flat
book goes short when it triggers.** Question 4 below is written to test this
directly.

**(b) An unledgered broker stop blocks new entries.**
`reconcile_snapshot` passes every account-wide open order to the reconciler
(`scripts/reconcile_paper.py:328`) and keys the DB side on
`OrderIntent.ib_order_id` for `SUBMITTED`/`PARTIALLY_FILLED` intents only
(`:299-318`). An order with no matching intent yields an
`order_missing_in_db` discrepancy (`services/execution/reconciliation.py:158-163`),
severity `major`, and `entries_allowed` false
(`reconciliation.py:56-61`). A permanently-resting GTC stop is exactly that
shape. This is why "stops bypass the message path entirely" is not a free
option for KAN-19 — see the go/no-go.

---

## Running the spike

Prerequisites: run from the repo root with the Gateway logged into the **paper**
session on 7497. Every phase refuses a non-`DU` account and is dry-run until
`--apply`, which needs an interactive TTY and an exact typed confirmation. The
harness uses client ids 118/119/128, distinct from execution (1), data (2),
`reconcile_paper` (57), `run_paper` (58/59), `convert_paper_fx` (95) and
`flatten_paper_account` (105).

> **Timing.** Do not leave a spike stop resting across the 04:15 paper run or
> the 04:45 divergence monitor — per fact (b) it will read as an
> `order_missing_in_db` discrepancy and disable entries for that session. Either
> finish inside one sitting, or accept and note the blocked day.

```bash
# 1. What is held, and what each stop would cover (read-only).
python -m scripts.ops.broker_stop_spike positions

# 2. Dry-run the plan for one name, then place it. GTC STP at the IPS
#    15% trailing level; triggerMethod and outsideRth are left at IB's
#    defaults on purpose — what those defaults ARE is question 1.
python -m scripts.ops.broker_stop_spike place --symbol CSCO
python -m scripts.ops.broker_stop_spike place --symbol CSCO --apply

# 3. Full field dump: this client's openTrades(), the account-wide
#    reqAllOpenOrders(), and what a second client sees before and after
#    it asks for all open orders (questions 1, 3, 4).
python -m scripts.ops.broker_stop_spike observe --cross-client

# 4. RESTART THE GATEWAY, then re-run the dump (question 2). IBC also
#    auto-restarts nightly at 23:55, which works as a zero-touch variant.
launchctl kickstart -k "gui/$(id -u)/local.ibc-gateway"
nc -z -G 3 127.0.0.1 7497 && echo "API port up"
python -m scripts.ops.broker_stop_spike observe

# 5. Drive the real kill-path cancel against the resting stop (question 4).
python -m scripts.ops.broker_stop_spike cancel-probe            # report only
python -m scripts.ops.broker_stop_spike cancel-probe --apply    # runs cancel_all_orders()

# 6. Leave the account flat of spike artifacts (AC6).
python -m scripts.ops.broker_stop_spike cleanup --apply
python -m scripts.ops.broker_stop_spike cleanup   # expect "nothing to clean up"
```

Question 1's trigger half needs a stop that actually fires, which sells real
paper shares. It is refused by default; opt in deliberately and keep it to one
share:

```bash
python -m scripts.ops.broker_stop_spike place --symbol CSCO --quantity 1 \
  --stop-price <above the last trade> --last-price <last trade> \
  --allow-trigger --apply
```

---

## Findings

### Q1 — Trigger semantics

`OBSERVATION PENDING` — paste the `triggerMethod` and `outsideRth` values from
the `place`/`observe` dumps, and, if the trigger probe was run, the resulting
fill record.

- Trigger method IB applied by default (0 = default, 1 = double bid/ask, 2 = last, …):
- `outsideRth` as accepted by IB:
- Observed on an extended-hours print? (yes/no + the print):
- What happened when the stop price was crossed:

### Q2 — Persistence across a Gateway restart — **the go/no-go**

`OBSERVATION PENDING` — this is the property the whole design rests on. State it
unambiguously: either the stop was still resting in `reqAllOpenOrders()` after
the restart, or it was not.

- Restart method (`launchctl kickstart` / IBC 23:55 nightly):
- Order id + `permId` before:
- Still resting after? (yes/no):
- `status` after the restart:

### Q3 — Visibility

`OBSERVATION PENDING` — the harness prints a
`populated fields BrokerOpenOrder cannot carry today` block; paste it here.

- `orderType` populated?
- `auxPrice` populated?
- `tif` populated?
- `orderRef` populated?
- Fields to add to `BrokerOpenOrder` for KAN-20's verifier:

### Q4 — Interaction with cancel-all

`OBSERVATION PENDING` — the `cancel-probe` output. Code reading (fact (a)) says
`find_order_by_ref` will return `None` for a stop placed by another client and
`cancel_all_orders` will not touch it; confirm or refute that empirically.

- `find_order_by_ref` result for the spike ref:
- Did the second client's `openTrades()` show the stop before `reqAllOpenOrders`?
- After `reqAllOpenOrders`?
- Was the stop still resting after `cancel_all_orders()`?

### Q5 — Whole-share / fractional interaction

`OBSERVATION PENDING` — `stop_coverage` mirrors
`IBExecutor._effective_quantity` (`ib_executor.py:142-162`) and is unit-tested,
but whether IB accepts a stop for the *exact* held quantity is a live question.

- Held quantity used:
- Quantity IB accepted:
- Any residue left unprotected:

### Surprises that contradict the design

`OBSERVATION PENDING` for anything the run turns up. Two are already known from
code and are recorded above rather than smoothed over: the kill path cannot
reach a foreign-client stop (fact (a)), and an unledgered resting stop blocks
entries at the next reconciliation (fact (b)).

---

## Go/no-go for KAN-19

**Provisional — cannot be finalised until Q2 is recorded.** If the stop does not
survive a Gateway restart, the "primary protection" claim collapses and the
design needs revisiting before KAN-19 is built. The rest of the recommendation
does not depend on the live run:

| Decision | Recommendation | Why |
|---|---|---|
| Order type | `STP` (plain stop), not `STP LMT` | a stop-limit can go unfilled in exactly the gap-down the stop exists for |
| TIF | `GTC` | a `DAY` stop leaves every position unprotected overnight, which is the failure mode D16 exists to remove |
| Sizing hook | IPS § 6 `stop_loss_trailing_pct` off the high-water mark, whole shares, residue reported | matches the existing rule; `stop_coverage` already models the truncation |
| `ApprovedOrderMessage.order_type` | extend to include `"stop"` rather than bypassing the message path | bypassing leaves the order unledgered, and fact (b) shows an unledgered broker order fails reconciliation and disables entries — the ledger entry is not optional bookkeeping, it is what keeps the book reconcilable |
| Who places the stop | the execution service's own `IBExecutor` client | fact (a): an order placed by any other client is invisible to `find_order_by_ref`, uncancellable by the kill path, and unrecoverable across an execution restart |
| Kill-path handling (KAN-20) | cancel stops **explicitly and first**, before liquidating | otherwise a stop resting on a just-flattened position sells short on trigger |

---

## Rollback

Cancel the spike orders (`cleanup --apply`). Nothing else is committed but this
document and the harness, and the harness is not imported by any service. The
spike is paper-only by construction: the account guard refuses any non-`DU`
account and `_run` refuses any config mode other than `paper`.

## Out of scope

Production code (KAN-19 places stops, KAN-20 verifies them) and live-account
testing.
