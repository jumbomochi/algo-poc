# Broker-Stop Prototype Spike (DUN551088)

**Status (2026-08-15): COMPLETE — the recommendation is GO for KAN-19.** All
five questions were exercised on DUN551088; the one deliberate gap is the
trigger half of Q1, deferred to KAN-20's drill for the reason recorded there.
The account was left flat of spike artifacts. Every finding quotes an observed
record; nothing here is inferred from IB documentation and presented as
evidence.

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

Prerequisites: run from the repo root — `load_config` resolves
`config/default.yaml` relatively, so any other working directory fails before
connecting — with the Gateway logged into the **paper**
session on 7497. Every phase refuses a non-`DU` account and is dry-run until
`--apply`, which needs an interactive TTY and an exact typed confirmation. The
harness uses client ids 118/119/128, distinct from execution (1), data (2),
`reconcile_paper` (57), `run_paper` (58/59), `convert_paper_fx` (95) and
`flatten_paper_account` (105).

**No market-data subscription is required.** The reference price for the stop
level comes from `reqHistoricalData` daily bars — the call `data_ingestion`
already makes on this account (`services/data_ingestion/ib_client.py:36`) —
not a streaming quote. A close is the right reference anyway: the IPS trailing
rule is close-based. `--stop-price` with `--last-price` bypasses the fetch
entirely if IB refuses even that.

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

Substitute real numbers for `STOP` and `LAST` — a literal `<placeholder>` is an
input redirect in zsh and the shell will reject it before the harness runs.
`STOP` must be above `LAST` for the stop to fire on arrival.

```bash
STOP=66.00 LAST=65.00   # read LAST off the `place` dry-run or the tape
python -m scripts.ops.broker_stop_spike place --symbol CSCO --quantity 1 \
  --stop-price "$STOP" --last-price "$LAST" --allow-trigger --apply
```

---

## Findings

The observed order, from `cancel-probe` at 2026-08-15 00:02:33 SGT. Every
finding below quotes this record:

```json
{
  "symbol": "CIBR", "con_id": 199295926, "orderId": 5, "permId": 1404141834,
  "clientId": 118, "account": "DUN551088", "orderRef": "kan18-spike-CIBR",
  "action": "SELL", "orderType": "STP", "totalQuantity": 21.0,
  "auxPrice": 84.87, "lmtPrice": 0.0, "tif": "GTC", "goodTillDate": "",
  "triggerMethod": 0, "outsideRth": false, "transmit": true,
  "trailStopPrice": 84.87, "trailingPercent": 1.7976931348623157e+308,
  "status": "PreSubmitted", "filled": 0.0, "remaining": 21.0,
  "whyHeld": "trigger"
}
```

### Q1 — Trigger semantics

**Partially answered.** `triggerMethod: 0` — IB applied its default method; the
harness sent no override. `outsideRth: false` — IB accepted the stop as
regular-hours-only.

**That last value matters to the design.** A GTC stop left at IB's defaults does
**not** arm on extended-hours prints. It protects against infrastructure
failure, which is what D16 claims, but it does *not* protect against an
overnight or pre-market gap — the stop only becomes live at the RTH open and
then triggers into whatever the opening print is. "Protection that survives
Redis, Postgres, Docker and the host" is true; "protection at all times" is not.
KAN-19 must decide deliberately whether to set `outsideRth=True`, and the IPS's
15% trailing rule should be read with the gap risk in mind.

`NOT ANSWERED — deliberately deferred.` What is observed when the stop price is
actually crossed. Firing a stop sells real paper shares through a client the
execution service does not track, so no `FillMessage` is emitted and the DB book
desynchronises (broker 20, DB 21 → `quantity_mismatch`, severity `major`,
entries disabled until an operator-gated repair). Operator decision 2026-08-15:
not worth a book desync here, because KAN-20 already carries an
`[OPERATOR-ASSISTED DRILL]` (success criterion 3c) where a stop firing can be
observed with the ledger wired up and no second repair. Recorded as an open gap
rather than smoothed over.

### Q2 — Persistence across a Gateway restart — **the go/no-go**

**GO. The GTC stop survives a full Gateway process restart.**

- Restart: `launchctl kickstart -k gui/$(id -u)/local.ibc-gateway` at
  **2026-08-15 10:08:04**. This was a process restart, not a reconnect — IBC
  opened a **new log file** (`ibc-3.23.0_GATEWAY-10.43_Saturday.txt`, login and
  config replayed at `10:08:04:446-869`), so the previous Gateway process and
  everything in its memory are gone.
- Observed ~90 seconds later, unchanged in every field: `permId 1404141834`,
  `orderId 5`, `orderType STP`, `auxPrice 84.87`, `tif GTC`,
  `status PreSubmitted`, `whyHeld trigger`. Present in **both**
  `openTrades()` and `reqAllOpenOrders()`.

This settles the load-bearing question. The order rests
`PreSubmitted`/`whyHeld: trigger` — held rather than working at the exchange —
and surviving a process restart is what proves it is held **at IB**, not in
Gateway memory. D16's claim stands: the stop survives Redis, Postgres, Docker
and the host.

**A second result falls out of the same dump, and it is the one that makes
KAN-19 buildable.** After the restart, client 118 saw its own stop through
plain `openTrades()` — the call `IBExecutor.find_order_by_ref` uses
(`ib_executor.py:509`) — while client 128 could not see it at all (Q4). Open
orders are bound to the **placing** `clientId` and that binding survives the
restart. So the execution service, reconnecting as `config.ib.client_id`, will
recover its own stops through the existing recovery path with no new machinery.
The cross-client blindness in Q4 is therefore not a blocker; it is a constraint:
place stops from the execution client and everything works, place them from
anywhere else and nothing does.

### Q3 — Visibility

**Answered — and the reader gap is bigger than the issue assumed.** All four
fields are populated and readable through `reqAllOpenOrders`: `orderType: "STP"`,
`auxPrice: 84.87`, `tif: "GTC"`, `orderRef: "kan18-spike-CIBR"`. None of them
are carried by `BrokerOpenOrder` (`shared/broker_state.py:19-31`), so a verifier
built on today's reader cannot tell a stop from a working limit order.

Two traps for KAN-20's verifier, both visible in the record above and neither
anticipated by the issue:

1. **`trailStopPrice` is populated on a plain `STP` order** — it mirrors
   `auxPrice` (both `84.87`). Presence of `trailStopPrice` is therefore *not* a
   test for "this is a trailing stop"; only `orderType` distinguishes them.
2. **`trailingPercent` comes back as `1.7976931348623157e+308`** — `DBL_MAX`,
   IB's sentinel for "unset", not a real percentage. A verifier that reads it
   without a sentinel check computes a nonsense trail level.

The harness's own gap report, verbatim — nine populated fields the reader drops:

```json
{"kan18-spike-CIBR": ["permId", "clientId", "orderRef", "orderType",
                      "auxPrice", "tif", "transmit", "trailStopPrice",
                      "trailingPercent"]}
```

Two of those are more than verifier inputs:

- **`permId`** is the durable identity. `orderId` is client-scoped and reused;
  `permId 1404141834` was stable across the restart and is unique account-wide.
  KAN-19 should persist `permId` on the `OrderIntent`, not just `ib_order_id`.
- **`clientId`** tells you whether the kill path can reach the order at all
  (Q4). A verifier that reads it can alert on a stop the execution service does
  not own, instead of silently trusting coverage it cannot cancel.

Fields `BrokerOpenOrder` needs for KAN-20: `order_type`, `aux_price`, `tif`,
`order_ref`, `perm_id`, `client_id`, and `why_held` (to distinguish
held-pending-trigger from working).

### Q4 — Interaction with cancel-all

**Answered — the code-derived prediction is confirmed empirically.** The stop was
placed by client 118; the probe connected an `IBExecutor` as client 128 and
called the production `find_order_by_ref`:

```json
{"kan18-spike-CIBR": {"ib_order_id": "5", "find_order_by_ref": null,
                      "reachable_by_kill_path": false}}
```

`find_order_by_ref` returns `null` because it scans only its own session's
`openTrades()` (`ib_executor.py:506-509`), so `IBExecutor.cancel_order` would
no-op (`:655-666`) and `OrderManager.cancel_all_orders` never reaches the order.

**Driven end-to-end through the production objects, the kill path's cancel-all
is a complete no-op against the stop:**

```
OrderManager tracks 0 spike order(s): []
cancel_all_orders() returned  ->  []
resting spike stops AFTER cancel_all_orders()  ->  still resting, unchanged
```

Not a partial failure, not a logged error — the order is simply invisible, so
the kill completes "successfully" with stop coverage fully intact on positions
it has just flattened.

The consequence stands as recorded in fact (a) and is now observed, not
inferred: **a stop placed by any client other than the execution service's own
survives a kill, and then rests against a position the liquidation has already
flattened — selling short on trigger.** KAN-19 must place stops from the
execution client itself; KAN-20 must cancel them explicitly and first.

### Q5 — Whole-share / fractional interaction

`OBSERVATION PENDING` — `stop_coverage` mirrors
`IBExecutor._effective_quantity` (`ib_executor.py:142-162`) and is unit-tested,
but whether IB accepts a stop for the *exact* held quantity is a live question.

**Answered for the whole-share case.** Held 21 CIBR, stop placed and accepted for
`totalQuantity: 21.0` — IB takes the exact held quantity, no residue.

The fractional case was **not exercised, because it does not currently arise**:
all 12 long positions on DUN551088 on 2026-08-14 are whole-share
(`positions` reported `uncovered=0` for every one), which is consistent with
`_effective_quantity` truncating at entry. `stop_coverage` models the truncation
and is unit-tested, so KAN-19 has the sizing hook; whether IB accepts a
*fractional* stop quantity remains untested and should be treated as unknown
rather than assumed.

### Surprises that contradict the design

Three, all recorded above rather than smoothed over:

1. **Extended-hours gaps are not covered** (Q1). `outsideRth: false` by default,
   so the stop is dormant outside RTH. D16's claim is about surviving
   infrastructure failure, and that part holds — but the doc and the IPS should
   not be read as "protected at all times".
2. **The kill path cannot reach a foreign-client stop** (Q4, fact (a)) — and the
   danger is the reverse of what the design anticipated: not stop coverage
   stripped mid-liquidation, but an orphaned stop selling short into a
   flattened book.
3. **An unledgered resting stop disables new entries** (fact (b)) — which
   removes "stops bypass the message path" as an option for KAN-19.
   **Confirmed on the live pipeline, unintentionally.** The spike stop was left
   resting across the 2026-08-15 04:15 paper run, and the before/after is clean:

   | Run | Result |
   |---|---|
   | `paper_trading_20260814.log` (no spike stop) | `reconciliation: ok; entries: enabled` |
   | `paper_trading_20260815.log` (spike stop resting) | `reconciliation: major; entries: disabled` |

   The only change between the two runs is the resting stop. The 08-15 run
   still completed (04:21) and wrote its snapshot, so this cost no gate
   evidence — but no entries were permitted that morning. This is the strongest
   single argument that KAN-19 must ledger its stops: shipping broker stops
   without a matching `OrderIntent` would disable entries permanently, every
   day, for every covered position.

A fourth, smaller: the resting stop's `auxPrice` is `84.87` where the dry-run
planned `84.88`. The reference close is taken from an intraday partial daily
bar while the market is open, so the close moved between the dry-run and the
apply. Worth confirming against the `place --apply` output; if it was not that,
it means IB adjusted the price and KAN-20's drift check must tolerate it.

---

## Go/no-go for KAN-19

**GO.** The load-bearing property holds: a GTC stop survived a full Gateway
process restart unchanged (Q2), so it is held at IB and protects through
Redis, Postgres, Docker and host failure exactly as D16 claims. Two conditions
attach — stops must be placed by the execution client, and they must be
ledgered — and both are consequences of observed behaviour, not preferences.

| Decision | Recommendation | Why |
|---|---|---|
| Order type | `STP` (plain stop), not `STP LMT` | a stop-limit can go unfilled in exactly the gap-down the stop exists for |
| TIF | `GTC` | a `DAY` stop leaves every position unprotected overnight, which is the failure mode D16 exists to remove |
| `outsideRth` | decide explicitly; default `false` observed | Q1: at the default the stop is dormant outside RTH, so an overnight gap is unprotected until the open. Setting `true` buys gap coverage at the cost of triggering on thin extended-hours prints |
| Reading stops back | extend `BrokerOpenOrder` with `order_type`, `aux_price`, `tif`, `order_ref`, `why_held` | Q3: today's reader cannot distinguish a stop from a working limit. Guard `trailingPercent` against IB's `DBL_MAX` sentinel and do not treat `trailStopPrice` as proof of a trailing stop |
| Sizing hook | IPS § 6 `stop_loss_trailing_pct` off the high-water mark, whole shares, residue reported | matches the existing rule; `stop_coverage` already models the truncation |
| `ApprovedOrderMessage.order_type` | extend to include `"stop"` rather than bypassing the message path | bypassing leaves the order unledgered, and fact (b) shows an unledgered broker order fails reconciliation and disables entries — the ledger entry is not optional bookkeeping, it is what keeps the book reconcilable |
| Who places the stop | the execution service's own `IBExecutor` client — **mandatory, not stylistic** | Q4: from any other client the order is invisible to `find_order_by_ref` and `cancel_all_orders` is a proven no-op against it. Q2: from its own client, recovery through the existing `openTrades()` path works unchanged across a Gateway restart |
| Durable identity | persist `permId`, not just `ib_order_id` | Q3: `orderId` is client-scoped and reused; `permId` was stable across the restart and is unique account-wide |
| Kill-path handling (KAN-20) | cancel stops **explicitly and first**, before liquidating | Q4: otherwise the kill reports success with stop coverage intact on positions it just flattened, and the orphaned stop sells short on trigger |

---

## What KAN-19 did with each recommendation (added 2026-08-16)

Every row above is implemented as recommended, with two deliberate departures:

**`ApprovedOrderMessage.order_type` was not extended.** The recommendation and
the reason behind it come apart on inspection: what reconciliation compares a
broker order against is the **ledger** (`reconcile_paper.py:299-318` reads
`OrderIntent` rows), not the stream. KAN-19 therefore writes the `OrderIntent`
directly — `order_type="stop"`, which `String(20)` already permits — and stops
never touch `stream:approved_orders`. The property the recommendation was
protecting (an unledgered stop disables entries) is fully preserved; what is
avoided is putting a protective order that rests for weeks through a pipeline
whose lifecycle (`SUBMITTED → FILLED`) describes a one-shot trade intent.

**`permId` is not persisted yet.** It needs a migration and a column on
`order_intents`, and its consumer is the verification reader KAN-20 builds.
Recorded there rather than done here. Until then a resting stop is keyed by
`ib_order_id`, which is client-scoped and reused — safe while the placing
client is also the only reader, which is the case today.

### Kill-path and exit-path interaction (KAN-19 AC7)

**The kill path is handled.** Stops are placed through `OrderManager.submit_stop`
and tracked in `open_orders`, so `cancel_all_orders()` — which `process_kill`
already runs *before* liquidating — cancels them along with everything else.
Q4's failure mode was a stop placed from a *foreign* client, which cancel-all
provably cannot see; placing from the execution client closes it. Pinned by
`test_the_kill_cancels_the_stop_before_it_liquidates`.

**The ordinary exit path is deferred to KAN-20, and this is the note.** A
full-coverage resting stop is, to `outstanding_sell_quantity`, a working sell
for the whole position. The KAN-10 oversell guard subtracts working sells from
the broker position, so a flattening exit sizes to zero and is **refused with
an alert** rather than submitted. That is the safe half of the behaviour — two
live sells against the same 21 shares is exactly how a long-only account ends
up short — but it means that **with `broker_stops_enabled: true`, a stopped
position cannot be exited by the software path**.

Making the exit cancel the stop first (then confirm it off the broker's book,
as `_cancel_working_buys` already does for working buys) is *adjustment*, which
KAN-20 owns. This is a hard precondition on KAN-33 flipping the flag: the flag
must not be turned on before KAN-20 lands. Pinned by
`test_a_resting_stop_makes_an_exit_refuse_rather_than_oversell`.

The same applies to the kill. `process_kill` cancels the stops and then
liquidates, but `cancelOrder` only *requests* a cancel. What makes that safe is
the same guard: until IB's `Cancelled` status lands and terminalises the intent,
the stop still counts as a working sell, so the liquidation is refused rather
than stacked on top of it. Fail-safe, but a kill can be refused — confirming the
cancel against IB's book first (the `cancel_working_orders` path, which already
does exactly this for working buys) is what turns that refusal back into a
liquidation, and it belongs with KAN-20's adjustment work.

Three more consequences for KAN-20's attention, all recorded in
`services/execution/broker_stops.py`'s module docstring:

1. `nonterminal_sell_exists` also sees the resting stop and suppresses the risk
   service's software stop-loss re-fires for that position. Arguably correct
   while the broker stop genuinely covers the position — but it is a behaviour
   change that arrives with the flag, not a no-op.
2. **The level is set once, not trailed.** Priced from the high known at
   placement and never revised, so it loosens relative to the IPS rule as the
   high rises. The broker stop and `check_stop_loss` agree on day one and drift
   after.
3. **Nothing reduces a stop when shares leave.** Coverage is only brought up.
   A position sold outside this service leaves its stop resting.

---

## Cleanup (AC6)

`cleanup --apply` cancelled the single spike order at 2026-08-15 10:10; the
re-read returned an empty list and the account is flat of spike artifacts. The
`order_missing_in_db` discrepancy that disabled entries on the 08-15 04:15 run
clears with it — the next run should report `reconciliation: ok; entries:
enabled` again, and that is worth confirming in
`~/ibc/logs/paper_trading_20260816.log` rather than assumed.

## Rollback

Cancel the spike orders (`cleanup --apply`). Nothing else is committed but this
document and the harness, and the harness is not imported by any service. The
spike is paper-only by construction: the account guard refuses any non-`DU`
account and `_run` refuses any config mode other than `paper`.

## Out of scope

Production code (KAN-19 places stops, KAN-20 verifies them) and live-account
testing.
