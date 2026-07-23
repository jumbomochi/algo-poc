# Dual-Currency Capital, Risk, and Performance Design

**Date:** 2026-07-24

**Status:** Approved for implementation planning

**Scope:** Broker account snapshots, capital sizing, USD funding controls,
fill accounting, performance attribution, observability, and the staged paper
rollout for an SGD-base account trading US equities.

## 1. Problem statement

The IB paper account uses SGD as its base currency and reports
`NetLiquidation` in SGD. The trading system buys US equities whose prices,
notionals, and fills are denominated in USD.

The current implementation represents broker NAV as a currency-less float,
then passes it into capital fields and Prometheus metrics explicitly described
as USD. Using the raw SGD value as USD would overstate current USD-equivalent
capacity by roughly 29%. Converting the entire system to USD would instead hide
the FX translation effect that determines the operator's actual SGD wealth.

The paper run exposed this mismatch when the account reader rejected the valid
SGD `NetLiquidation` row. The correction must make currency a first-class part
of every monetary boundary rather than weakening the validation.

## 2. Decision

The system uses an explicit dual-currency model:

- **Base and reporting currency:** SGD
- **Trading and sleeve-ledger currency:** USD

SGD is the numeraire for broker NAV, deposits, withdrawals, total performance,
and account-level drawdown. USD is the numeraire for US-equity prices, order
notionals, reservations, sleeve budgets, fills, and virtual sleeve cash.

Live execution must not create a negative USD cash balance. Available margin
or SGD collateral cannot substitute for settled USD cash when approving a buy.
SGD-to-USD conversion is an explicit funding action and is not silently
combined with equity-order execution.

## 3. Considered approaches

### 3.1 Explicit dual-currency ledger — selected

Keep broker and investor accounting in SGD while keeping trading economics in
USD. Persist the conversion rate and both currency identities at every relevant
snapshot. This preserves dimensionally correct risk checks and exposes the FX
effect on investor returns.

### 3.2 SGD-only ledger

Translate every price, order, fill, commission, and position into SGD. This
simplifies the final investor report but complicates broker reconciliation,
order sizing, reservation accounting, and comparison with USD backtests.
Valuation also becomes sensitive to which FX timestamp each trading event uses.

### 3.3 USD-only ledger

Keep all internal accounting in USD and translate only final reports. This is
simple for trading logic but makes SGD deposits, drawdown, and actual investor
outcomes secondary. It can obscure material FX translation risk and allows
USD results to be mistaken for SGD wealth changes.

## 4. Goals

- Read the SGD broker NAV without treating it as USD.
- Obtain a contemporaneous, unambiguous SGD-per-USD conversion rate from IB.
- Calculate USD-equivalent deployable capital and USD sleeve budgets.
- Reject buys that would exceed settled USD cash after active reservations and
  estimated commission.
- Preserve original currencies on positions, fills, commissions, cash flows,
  and capital snapshots.
- Report strategy performance in USD and investor performance in SGD.
- Attribute the SGD result between security performance, trading costs, FX
  translation, and external cash flows.
- Fail closed on missing, stale, ambiguous, or contradictory currency data.
- Keep attributed exits and emergency liquidation available during an entry
  block.
- Migrate and roll out additively without resetting paper state.

## 5. Non-goals

- Automatically placing FX conversion orders.
- Hedging USD exposure back to SGD.
- Allowing a USD margin loan to fund equity purchases.
- Supporting multiple trading currencies in the first rollout.
- Rewriting all market-price arithmetic from floating point to decimal.
- Automatically assigning legacy or broker-only positions to sleeves.
- Enabling live entries as part of this change.

## 6. Currency conventions

All FX fields encode their direction in the name. The primary quote is:

```text
fx_base_per_trading = SGD per 1 USD
```

For example, `fx_base_per_trading = 1.2928304` means:

```text
1 USD = 1.2928304 SGD
```

Conversions are therefore:

```text
USD amount = SGD amount / fx_base_per_trading
SGD amount = USD amount * fx_base_per_trading
```

The system must reject non-positive or non-finite amounts and FX rates. It must
not infer direction from a generic field named `exchange_rate`.

Currency behavior is configured explicitly:

- `expected_base_currency: SGD`
- `trading_currency: USD`
- `max_fx_age_seconds: 300`
- `minimum_settled_usd_reserve`: non-negative in paper mode and required to be
  positive before live entries can be enabled

Changing either currency is an operator-reviewed configuration change, not an
automatic response to a broker value.

## 7. Broker account snapshot

`BrokerAccountSnapshot` becomes currency explicit. Its capital fields are:

- `base_currency`: configured and broker-validated as `SGD`
- `trading_currency`: configured as `USD`
- `net_liquidation_base`: broker `NetLiquidation` in SGD
- `fx_base_per_trading`: SGD per USD
- `net_liquidation_trading_equivalent`: derived USD-equivalent NAV
- `settled_cash_trading`: settled USD cash available before reservations
- `fx_source`: the IB account-value field used for the quote
- `fx_captured_at`: response time of the fresh FX observation
- `captured_at`: timestamp shared by the coherent account snapshot

The reader selects exactly one managed account, validates paper/live identity,
then selects exactly one `NetLiquidation` row for that account. It accepts the
row only when its currency equals the configured base currency.

The reader obtains the USD FX row and settled-USD-cash value from a fresh IB
account-values/ledger response on the same connection. It does not assume that
the ordinary account-summary tag set includes the currency ledger. Rows scoped
to the account are preferred; IB aggregate rows are accepted only when there
is one managed account and the requested currency is unambiguous. The snapshot
is assembled only after NAV, FX, and USD-ledger callbacks for that request have
completed. It never combines cached values captured by different runs. The
derived USD NAV is calculated by dividing SGD NAV by SGD per USD.

The snapshot fails before reconciliation or order generation if the base
currency, trading currency, NAV, FX quote, or settled trading cash is absent,
duplicated, stale, or non-finite. NAV and FX must be positive. Settled trading
cash may be positive, zero, or negative so the snapshot remains useful for
exits and diagnosis; zero or negative cash blocks all buys.

## 8. Capital model

Capital sizing begins with broker NAV in SGD and ends with a USD sleeve budget:

```text
fractional_base = net_liquidation_base * deployment_fraction
fractional_trading = fractional_base / fx_base_per_trading
deployable_trading = min(fractional_trading, max_deployable_usd)
```

When `max_deployable_usd` is unset, only the fraction applies. Sleeve weights
divide `deployable_trading`, so every sleeve budget remains USD-denominated.

The capital result stores both views:

- SGD NAV and fractional budget
- USD-equivalent NAV and deployable capital
- USD sleeve budgets
- the exact FX rate and timestamp used
- base and trading currency codes

NAV-derived capacity is a risk ceiling, not proof that cash is funded. A rise
in SGD NAV can increase the theoretical USD ceiling but cannot create USD
sleeve cash or permit a buy without settled USD funding.

## 9. USD funding and buy approval

SGD deposits and USD trading funds are separate state transitions:

1. An SGD deposit increases broker NAV and is recorded as an external SGD cash
   flow.
2. An SGD-to-USD conversion is recorded with both amounts, the executed rate,
   fees, timestamp, and operator/source.
3. Only resulting settled USD cash can fund new US-equity purchases.
4. An explicit allocation event assigns newly funded USD to virtual sleeves.

For each proposed buy:

```text
required_usd = order_notional_usd
             + estimated_commission_usd
             + active_buy_reservations_usd
             + minimum_settled_usd_reserve

required_usd <= settled_cash_usd
```

The risk service also applies the existing sleeve, position, sector, exposure,
reconciliation, and capital-snapshot gates. Passing available-funds or margin
checks does not bypass the settled-USD-cash gate.

The commission estimate must be conservative for the configured IB pricing
plan. Live entries cannot be enabled until both the commission estimator and a
positive settled-USD reserve are configured and validated.

Active reservations are account-scoped and USD-denominated. They are released
only when the associated order becomes terminal or reduced by confirmed fills.
Buy rejection due to insufficient USD cash is durable and observable.

Sells do not require USD funding. Attributed sells and emergency liquidation
remain available when entries are blocked.

## 10. Ledger and database model

Schema changes remain additive. Existing ambiguous fields are retained during
the compatibility period, but new writes populate explicit currency fields.

### 10.1 Capital snapshots

Add or persist explicit values for:

- base currency and trading currency
- NAV in SGD
- NAV equivalent in USD
- SGD-per-USD rate and FX timestamp
- fractional SGD budget
- deployable USD capital
- settled USD cash
- USD sleeve budgets
- reconciliation and entry-eligibility status

### 10.2 Portfolio and equity state

Virtual `portfolio_config.cash` is explicitly USD. Existing rows are migrated
with `USD` as their currency because the paper ledger was initialized and
marked using USD equity prices.

Daily sleeve equity snapshots persist:

- USD cash
- USD market value
- USD equity
- SGD-per-USD rate
- translated SGD equity
- both currency codes and the valuation timestamp

The broker's consolidated SGD NAV remains authoritative for account-level
capital and drawdown. Sleeve equity is attribution state and need not equal the
whole account when unallocated SGD or unrelated assets exist.

### 10.3 Fills, commissions, and cash flows

Execution fills preserve the contract and execution currency. Commission
amount and commission currency are stored separately. The original commission
is never overwritten by a converted value.

For USD strategy reporting, a non-USD commission is translated using the
event-time FX quote while preserving the original amount, currency, rate, and
timestamp. Funding and capital-adjustment rows similarly carry original and
translated amounts rather than an unqualified float.

## 11. Performance and FX attribution

Two return series are first-class outputs.

### 11.1 USD strategy return

The USD series measures trading decisions, security movement, commissions,
and slippage without SGD translation. It remains the comparison series for the
USD backtest and the live-versus-backtest divergence monitor.

### 11.2 SGD investor return

The SGD series translates USD sleeve equity using the captured SGD-per-USD
rate and combines it with any unallocated base-currency state in scope.
Deposits and withdrawals are external flows and are excluded from investment
return calculations.

Daily attribution reports:

- USD security P&L translated to SGD
- commissions and trading costs
- FX translation P&L
- external SGD cash flows
- total cash-flow-adjusted SGD P&L and return

For a period with no external flow, the decomposition must reconcile exactly:

```text
total_sgd_change = ending_usd_equity * ending_fx
                 - starting_usd_equity * starting_fx
```

Security and FX contributions use one documented convention and the FX term is
the residual required to reconcile to total SGD change. Tests assert the
identity rather than allowing unexplained rounding drift.

## 12. Failure handling

New entries fail closed when any of these conditions holds:

- broker base currency differs from configured SGD
- trading currency differs from configured USD
- NAV, settled USD cash, or FX data is missing or ambiguous
- an amount or FX rate is invalid or stale
- the proposed buy would create negative USD cash
- account, contract, fill, or commission currency metadata conflicts
- broker/database reconciliation blocks entries
- capital, exposure, or other existing risk limits reject the order

Each rejection records a specific reason and the relevant currency context.
No fallback treats SGD and USD values as interchangeable.

Entry failures do not suppress correctly attributed exits or kill liquidation.
An exit whose contract or ownership cannot be attributed still fails closed
rather than guessing.

## 13. Observability and reporting

Currency is explicit in metric names or labels. Existing metrics ending in
`_usd` continue to accept USD values only. New base-currency metrics end in
`_sgd` or carry a bounded currency label.

Operational output prints currency codes instead of a generic dollar sign. A
daily summary includes:

- broker NAV in SGD and USD equivalent
- SGD-per-USD rate and age
- settled, reserved, and remaining USD cash
- USD deployable capital and sleeve budgets
- reconciliation and entry status
- USD strategy return and SGD investor return
- FX translation contribution

Alerts distinguish market-data, broker, reconciliation, currency, funding,
and risk failures.

## 14. Testing

Automated coverage includes:

- valid SGD NAV plus USD conversion
- USD-base compatibility and unexpected-base rejection
- missing, duplicate, non-finite, non-positive, and stale FX data
- exact FX direction and conversion arithmetic
- zero and insufficient settled USD cash
- buy rejection despite available margin or SGD collateral
- commission and reservation headroom
- reservation release on partial and terminal lifecycle transitions
- fill and commission currency preservation
- USD sleeve and SGD investor valuation
- cash-flow-adjusted return and FX-attribution reconciliation
- explicit metric and report currency units
- additive migration and legacy-row compatibility
- entries-disabled integration execution with zero order submission

The regression test for the original failure uses the observed pattern: a
single account with SGD `NetLiquidation`, a USD exchange-rate row, and USD
settled cash. It must fail before implementation and pass afterward.

## 15. Rollout

1. Implement the currency-explicit snapshot and capital interfaces using TDD.
2. Add and verify the additive database migration.
3. Run focused currency, capital, risk, accounting, and reporting tests.
4. Run the complete test suite and lint the touched files.
5. Rebuild all project images.
6. Run the paper job with entries disabled.
7. Verify SGD NAV, USD equivalent, FX rate, settled USD cash, capital snapshot,
   and zero unexpected orders.
8. Generate the report-only legacy-position reconciliation plan.
9. Have the human operator apply any required ownership repair interactively.
10. Rerun with entries disabled and verify broker/database reconciliation.
11. Keep live deployment fraction and cap at zero until a separate explicit
    enablement decision.

No rollout step resets paper state, automatically repairs position ownership,
places FX orders, or enables live entries.

## 16. Acceptance criteria

- The SGD-base paper account produces a valid currency-explicit snapshot.
- No raw SGD value enters a USD capital, notional, reservation, or risk field.
- A buy cannot be approved if it could create negative settled USD cash.
- USD strategy results remain comparable with the USD backtest.
- SGD investor results include and reconcile FX translation effects.
- All persisted monetary values have an explicit currency through field name,
  adjacent currency code, or both.
- Missing or contradictory currency data blocks entries with a precise reason.
- Entries-disabled paper execution completes without unexpected orders after
  human-owned legacy reconciliation is complete.
