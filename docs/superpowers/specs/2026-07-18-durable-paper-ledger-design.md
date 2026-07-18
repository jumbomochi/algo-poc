# Durable Paper Ledger and Broker-NAV Allocation Design

**Date:** 2026-07-18  
**Status:** Approved for implementation planning  
**Scope:** Paper-trading accounting, broker reconciliation, strategy-state
hydration, projected exposure, and portable paper/live capital sizing.

## 1. Problem statement

The IB paper account exposes roughly $1 million of simulated NetLiquidation,
but the internal six-sleeve ledger was initialized on a fixed $100,000 basis.
The daily runner sizes and gates entries against that independent ledger rather
than the broker account.

Three accounting defects make the apparent capacity problem worse:

1. Strategy factories keep positions in process-local `tracked` dictionaries.
   They are recreated on every daily run, so held positions emit repeated buys
   and restart-safe exits cannot be evaluated.
2. `scripts/run_paper.py` records a fill and changes sleeve cash immediately
   after its local risk check, before downstream risk approval and before IB
   reports an execution. Rejected and unfilled orders can therefore become
   phantom positions.
3. The risk engine checks current exposure rather than projected post-order
   exposure. A final order can push a sleeve beyond its configured ceiling.

The corrected system must use broker NetLiquidation for configurable sizing,
retain sleeve attribution in PostgreSQL, treat IB executions as the only source
of filled quantity, and stop safely when the broker and database disagree.

## 2. Goals

- Calculate deployable capital automatically from IB NetLiquidation.
- Configure deployment independently for paper and live modes.
- Persist sleeve attribution, order state, fills, positions, cash, and capital
  snapshots in PostgreSQL.
- Hydrate every strategy from filled PostgreSQL positions on each run.
- Prevent rejected, cancelled, expired, or unfilled orders from changing
  positions or cash.
- Reserve capacity for approved and submitted buy orders.
- Gate entries using projected filled plus reserved exposure.
- Make recommendation and fill replay idempotent.
- Compare PostgreSQL filled state with IB positions and open orders before new
  entries are published.
- Provide a report-only reconciliation workflow and a separately invoked,
  interactive repair workflow for the existing paper ledger.
- Preserve normal exits and emergency liquidation during a reconciliation
  mismatch.
- Validate material strategy-sizing or replacement changes in backtests before
  enabling them in scheduled paper trading.

## 3. Non-goals

- Replacing Redis Streams with a different message bus.
- Building a general-purpose event-sourcing platform.
- Automatically assigning an IB-only position to a sleeve.
- Automatically repairing or resetting the existing paper database.
- Enabling live capital during this implementation.
- Changing the trading surfaces or adding discretionary trade overrides.

## 4. Sources of truth

The system deliberately has two complementary sources of truth:

| State | Authority |
|---|---|
| Account identity, mode, NetLiquidation, broker positions, open orders, executions | IB |
| Sleeve attribution, strategy entry state, order intent, reservations, fill ledger, virtual sleeve cash | PostgreSQL |
| Transport and replay of recommendations, approvals, lifecycle events, and fills | Redis Streams |

PostgreSQL may describe how an aggregate IB position is divided among sleeves,
but the sum of filled sleeve quantities for a contract must match IB. A mismatch
blocks new entries.

## 5. Capital model

Each run reads a fresh IB account snapshot and calculates:

```text
fractional_budget = NetLiquidation * deployment_fraction
deployable_capital = min(fractional_budget, max_deployable_usd)
```

When `max_deployable_usd` is unset, only the fraction applies. The existing six
sleeve weights divide `deployable_capital` into current sleeve budgets.

Configuration is mode-specific:

- Paper defaults to `deployment_fraction: 1.0` and no dollar cap.
- Live defaults to `deployment_fraction: 0.0` and
  `max_deployable_usd: 0.0`.
- Live requires both a positive fraction and a positive cap before entries are
  permitted.
- Deployment fractions must be in `[0.0, 1.0]`; caps must be non-negative.
- Account identity and paper/live mode must match configuration.

Daily NetLiquidation movement changes the budget used for sizing and risk. It
does not create a PostgreSQL cash deposit. Sleeve cash changes only through
actual fills or an explicit capital-funding event. The one-time move from the
existing $100,000 ledger to the paper account budget is an explicit,
human-approved funding repair.

Every run stores a `capital_snapshot` containing account, mode,
NetLiquidation, fraction, cap, deployable capital, sleeve budgets, timestamp,
and reconciliation status.

## 6. PostgreSQL data model

All schema changes are additive.

### 6.1 `order_intents`

One row per deterministic recommendation ID:

- `recommendation_id` unique
- account ID and mode
- portfolio/sleeve
- broker contract identity (`con_id`, symbol, exchange, currency)
- action, requested quantity, limit price, and order type
- reserved notional and filled quantity
- lifecycle status
- IB order ID when assigned
- rejection/failure reason
- created, published, approved, submitted, terminal, and updated timestamps

Allowed states are:

```text
PROPOSED
RISK_REJECTED
APPROVED
SUBMISSION_FAILED
SUBMITTED
PARTIALLY_FILLED
FILLED
CANCELLED
EXPIRED
```

Transitions are monotonic. Terminal states cannot return to active states.

### 6.2 `execution_fills`

Immutable fill rows with:

- account ID
- IB execution ID
- IB order ID
- recommendation ID and sleeve
- broker contract identity
- side, quantity, price, commission, and timestamp
- cumulative filled quantity when supplied by IB

`(account_id, execution_id)` is unique. Duplicate Redis delivery must not
change position quantity, cash, or order status twice.

### 6.3 `capital_snapshots` and `capital_adjustments`

`capital_snapshots` records each run's sizing basis. `capital_adjustments`
records explicit funding and withdrawal events by sleeve, including the
operator-approved initial paper-account rebase.

### 6.4 Existing position state

`positions` gains durable broker contract identity. It remains the materialized
filled sleeve position state. `portfolio_config` retains virtual sleeve cash and
funded capital. Position current price and peak price may be marked after a
successful market-data cycle, but only the fill projector may create a
position, change filled quantity, or change cash because of trading.

## 7. Daily data flow

1. Connect to the configured IB account and read account summary, positions,
   open orders, and contract identities.
2. Compare IB aggregate filled quantities and open orders with PostgreSQL.
3. If reconciliation fails, persist the report and block new entries. Continue
   evaluating valid exits and leave kill liquidation available.
4. Calculate deployable capital and sleeve budgets; persist the capital
   snapshot.
5. Load filled sleeve positions and active order reservations from PostgreSQL.
6. Build a `PortfolioContext` for every strategy.
7. Generate proposed entries and exits without mutating position state.
8. Apply sleeve risk using projected exposure.
9. Persist deterministic `PROPOSED` order intents before publishing
   recommendations.
10. Publish any unpublished intents; replay uses the same recommendation ID.
11. Risk management records approval or rejection against the intent.
12. Execution records submission and the IB order ID.
13. Actual IB execution callbacks publish enriched fill messages.
14. The fill projector transactionally inserts the immutable fill, updates the
   materialized position and cash, and advances the order lifecycle.

Redis messages are acknowledged only after the relevant PostgreSQL transaction
commits.

## 8. Strategy hydration and signal semantics

Mutable per-process `tracked` dictionaries are removed from paper execution.
Strategies receive a read-only `PortfolioContext` containing:

- filled positions and quantities
- average entry prices and entry dates
- persisted peak prices
- current market prices
- pending buy and sell quantities
- active reservations
- sleeve budget and current regime

Signal evaluation is side-effect free:

- A held ticker that has no exit becomes `HOLD` and creates no order intent.
- A ticker with an active compatible intent creates no duplicate intent.
- Trailing-stop, max-loss, moving-average, and holding-period exits use the
  persisted position state.
- Sell quantity is the available filled quantity minus pending sell quantity.
- Partial fills remain active until filled or terminal.
- Emitting a signal never changes strategy or position state.

Ranked strategies compute scores for the complete eligible universe before
selection. This removes ticker-iteration bias.

## 9. Exposure and initial sizing alignment

Projected buy exposure is:

```text
filled market value + active buy reservations + proposed buy notional
---------------------------------------------------------------------
                         current sleeve budget
```

The gate scales quantity to remaining headroom when the result is tradeable;
otherwise it rejects the entry. It also evaluates projected sector and
per-position exposure where applicable.

The initial corrected target sizing is:

| Strategy | Selection | Target size | Planned exposure |
|---|---:|---:|---:|
| Quality value | Top 15 | 6% each | 90% |
| Thematic momentum | Top 8 | 13.5% each | 108% |
| Momentum | Top 5 | 12% each | 60% |
| Sector rotation | Top 3 | 20% each | 60% |
| Tail-risk hedge | Regime allocation | 25% each | Up to 100% |

These targets leave room for market movement and in-flight orders. Existing
hard exposure ceilings remain ceilings, not targets.

Candidate replacement is represented as target-position deltas, but rank-drop
exits are not enabled until validation. Backtests compare:

1. Existing technical exits only.
2. Replacement of the weakest holding when a new candidate enters the target
   set.
3. Replacement only when the incoming score exceeds the weakest holding by a
   configured margin.

Only a policy that improves walk-forward risk-adjusted results and remains
inside a defined turnover ceiling may be enabled in scheduled paper trading.

## 10. Order durability and crash recovery

Buy reservations begin at `APPROVED` and remain through `SUBMITTED` and
`PARTIALLY_FILLED`. Filled portions move from reserved to filled exposure.
Reservations are released on rejection, failure, cancellation, expiry, or full
fill.

The recommendation publisher scans `PROPOSED` intents that are not marked
published. A crash between Redis publication and the published timestamp may
produce a duplicate message, but conditional PostgreSQL transitions make the
duplicate harmless.

Execution persists the IB order ID before relying on its in-memory order map.
On restart it reloads active submitted intents and compares them with IB open
and completed orders. A submitted database order that is missing at IB becomes
a reconciliation error; it is not silently cancelled.

`FillMessage` carries account ID, execution ID, order ID, recommendation ID,
sleeve, contract identity, fill quantity, cumulative quantity, price,
commission, and timestamp. Unknown fills are persisted for audit and trigger a
reconciliation failure rather than receiving guessed sleeve attribution.

## 11. Reconciliation and repair

Normal startup reconciliation compares:

- IB positions against summed filled PostgreSQL sleeve positions by account and
  contract ID.
- IB open orders against active submitted or partially filled intents.
- Filled order quantities against immutable execution fills.
- Configured account and mode against the connected account.

Quantity comparison uses a small numerical tolerance only for representation;
actual IB fill quantities are persisted without strategy-side rounding.

On mismatch:

- new entries fail closed
- ordinary reduction exits remain permitted up to broker-held quantity
- emergency liquidation remains available
- the daily report and metrics identify every unmatched contract and order

The repair tool defaults to report-only. It writes a proposed repair plan and a
backup before any apply operation. Applying a plan requires a TTY and explicit
human confirmation. IB-only positions with unknown sleeve ownership require an
explicit mapping in the reviewed plan. The implementation process must not run
the apply operation or destructively edit the paper database.

## 12. Error handling and observability

Risk rejection, broker rejection, skipped sizing, submission failure,
cancellation, expiry, and partial fill are durable lifecycle outcomes rather
than log-only events.

Metrics and the daily report include:

- broker NetLiquidation and deployable capital
- current sleeve budgets and exposure
- reserved notional by sleeve
- intent counts by lifecycle state
- stale active reservations
- fills and duplicate-fill suppressions
- unmatched positions and orders
- last successful reconciliation timestamp and status

Invalid capital configuration, account mismatch, mode mismatch, missing IB
account data, and unexplained broker/database divergence block entries with an
explicit alert.

## 13. Validation

Required automated coverage includes:

- capital fraction and optional-cap calculation
- live fail-closed defaults
- projected total, sector, and position exposure
- reservation creation and release
- strategy hydration across process restart
- held-position `HOLD` behaviour
- restart-safe exit generation
- complete-universe ranking
- rejection, submission failure, cancellation, expiry, and partial fills
- duplicate recommendation and fill delivery
- execution restart with a pending IB order
- exact, DB-only, IB-only, ambiguous, and open-order reconciliation cases
- proof that no position or cash mutation occurs before an actual fill

The full repository suite must pass. Corrected sizing and all three replacement
policies are evaluated over the ten-year backtest and walk-forward windows.

## 14. Acceptance criteria

- Sleeve budgets sum to the configured deployable capital, within rounding.
- Paper may use 100% of current IB NetLiquidation when configured.
- Live submits no entry unless both its fraction and cap are positive.
- PostgreSQL aggregate filled quantity matches IB after successful
  reconciliation.
- Duplicate messages never change filled quantity or cash twice.
- Rejected and unfilled orders never become positions.
- A proposed order cannot push projected exposure above a hard limit.
- Filled positions can generate exits after a daily-process restart.
- Reconciliation mismatches block entries and produce an actionable report.
- Existing kill-switch behaviour remains available.

## 15. Rollout

1. Apply additive schema migrations.
2. Deploy with entry publication disabled.
3. Run reconciliation in report-only mode.
4. Generate a backed-up repair plan for the existing phantom paper ledger.
5. Have the operator review and run the interactive repair separately.
6. Verify PostgreSQL aggregates against IB.
7. Run one signal-only paper cycle.
8. Run one bounded published paper cycle and verify the complete lifecycle.
9. Restore the scheduled paper run.
10. Leave live deployment disabled until the existing live operational gates
    are satisfied.

