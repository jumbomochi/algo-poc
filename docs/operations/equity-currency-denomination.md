# Equity Snapshot Currency Denomination — the cutover contract

Direction doc **D16** bounds the ladder at "max drawdown ≤12% at current size,
**measured on USD NAV**", and makes it one of the five clean-epoch criteria.
The paper account has been **SGD-base since 2026-07-25**. Until KAN-44 the only
populated equity series carried no currency at all, so "measured on USD NAV"
was an assumption about a column, not a checkable claim.

A drawdown is a ratio of two points in one series, so it is FX-neutral **only
if no FX move occurred between the peak and the trough**. Over a 30-session
window that assumption is not free: a 10% move in SGD/USD between a peak and a
trough turns a 5% USD drawdown into a 14.5% base-currency one — one side of the
12% bound, then the other. `tests/backtest/test_paper_state_dual_currency.py`
pins exactly that fixture.

## What is recorded

`equity_snapshots` has carried eight currency-qualified columns since
`f6c2d9a84b31_add_dual_currency_accounting.py`; every one was NULL on every row
until KAN-44. `PaperTradingState.record_equity_snapshot` now populates them
when — and only when — an FX rate is supplied:

| Column | Meaning |
|---|---|
| `base_currency` | Account base currency (`SGD`) |
| `trading_currency` | Currency the book trades and is valued in (`USD`) |
| `equity_trading` | `equity`, restated as an explicitly USD figure |
| `cash_trading` | `cash`, ditto |
| `market_value_trading` | `market_value`, ditto |
| `fx_base_per_trading` | Base per one unit of trading currency (SGD per USD) |
| `equity_base` | `equity_trading × fx_base_per_trading` (an SGD figure) |
| `valuation_at` | The instant IB quoted that rate |

`equity` / `cash` / `market_value` are unchanged and remain the columns every
existing reader uses. This story records the currency context of a number that
was already computed; it does not change how anything is valued.

**`equity_trading` is the USD series and `equity_base` is the SGD one.** D16's
bound is on USD NAV, so a D16-faithful drawdown is computed on `equity_trading`
(equivalently, on `equity` — they are the same number). `equity_base` exists so
the base-currency view is available and the two can be compared, which is what
makes the FX exposure visible at all.

## The rate's provenance

`scripts/run_paper.py` passes the `CapitalBudget` the funding check already
consumes, so the rate is the same one the day's sizing decisions were made
against. No extra IB call, no new failure mode. A run without a budget (a bare
test harness) writes the columns NULL, exactly as a pre-cutover row looks.

Two rules keep a stamped row honest:

- **A malformed rate raises.** Non-finite, zero, or negative rates, and a rate
  supplied without its currency pair, are refused rather than silently written
  as NULL — an unstamped row is indistinguishable from a legacy one, and the
  gap would surface on gate day.
- **An upsert rewrites all eight columns, including clearing them.** A catch-up
  run that rewrites the day's equity with no rate must not leave yesterday's
  rate beside today's number.

## The cutover date

**Rows written before this change landed carry no currency context and no rate
will ever be added to them.** IB's historical SGD/USD quotes for those dates
were never recorded, and inventing them would fabricate evidence — the one
thing this project's evidence discipline exists to prevent. Backfilling is
explicitly out of scope for KAN-44.

Therefore:

- **Pre-cutover history is base-currency-ambiguous.** Treat drawdown computed
  over it as computed on the legacy `equity` column, denomination unproven.
- **Post-cutover history is denominated**, and a reader can tell the two apart
  by whether `fx_base_per_trading` is NULL — never by the date, which would
  hard-code a fact that belongs in the data.
- A window spanning the cutover is **mixed**. A consumer that cares (the
  epoch-scoring helper, the go-live gate) must report which series backed its
  number rather than quietly averaging the two regimes.

To find the actual cutover, ask the data rather than this document:

```sql
SELECT MIN(date) FROM equity_snapshots WHERE fx_base_per_trading IS NOT NULL;
```

## Open follow-up

`shared/evidence_store.py` (KAN-26, not yet implemented) is where
`epoch_progress` computes the drawdown the ladder is graded on. KAN-44's AC4
asks it to prefer the currency-qualified series and to report which series
backed the number. That belongs to KAN-26's diff — the module does not exist
yet — and is recorded on that issue. Note the discrepancy flagged above when
implementing it: **D16 says USD, and USD is `equity_trading`, not
`equity_base`.**
