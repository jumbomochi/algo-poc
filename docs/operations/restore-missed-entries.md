# Rebuilding missed ENTRY fills from an IB statement

**Tool:** `scripts/ops/restore_missed_entries.py`
**Sibling:** [`restore_missed_exit.py`](../../scripts/ops/restore_missed_exit.py)
does the same for missed *exits*. Pick by direction: a position the book
does not know it **owns** is an entry; one it thinks it still holds and IB
does not is an exit.

This is an **operator** procedure. An agent may prepare the statement and
run the dry run; only a human runs `--apply`.

## When this is the right tool

All of the following:

1. `python scripts/reconcile_paper.py --report` shows `missing_in_db`
   discrepancies — IB holds shares the book has no position for.
2. The daily execution sweep cannot reach them. `reqExecutions` serves only
   about the current trading day, so anything older than the previous
   session is gone from the API and the sweep will report nothing.
3. `reconcile_paper.py --apply-plan` refuses: its only repair action is
   `set_position_quantity`, and a `missing_in_db` divergence generates 0
   actions with every entry `unresolved / sleeve_mapping_required`.

If instead the book holds a position IB does not, stop — that is
`restore_missed_exit.py`, and the residue rules there are different.

## Step 1 — export the statement

In IB Account Management: **Performance & Reports > Flex Queries**, an
**Activity Flex Query** with the **Trades** section.

Required fields, exactly these thirteen:

```
ClientAccountID  TradeID     IBOrderID  ConID    Symbol
Exchange         CurrencyPrimary        Buy/Sell Quantity
TradePrice       IBCommission           IBCommissionCurrency
DateTime
```

Settings that matter:

- **Date/Time format:** any of `yyyyMMdd;HHmmss`, `yyyy-MM-dd HH:mm:ss` or
  ISO. **Time zone must be UTC.** The parser reads `DateTime` as UTC and
  never guesses a zone; an export in the account's local time would file
  every fill four or five hours off and land it against the wrong session.
- **Date range:** the trade date(s) being recovered.
- **Format:** CSV.
- **One account per file.** A statement covering two accounts is refused.

Save it somewhere outside the repo — it is broker data, not source.

## Step 2 — dry run

```bash
python scripts/ops/restore_missed_entries.py \
    --statement ~/Downloads/DUN551088_trades_20260918.csv \
    --account DUN551088
```

It writes nothing. Read every line before going further.

```
  RECOVER  AMD    order 189        5.0000 @   161.4200  commission    1.00  =       807.10  (0000e0d5...)
  ...
  15 to recover, 0 already recorded, 0 untracked, 0 deferred
  cost basis to be restored: 25,600.00
```

- **RECOVER** — will be projected through `FillProjector`, the same path a
  live fill takes.
- **already recorded** — the execution is in `execution_fills` already.
  Skipped. This is what makes a re-run safe.
- **UNTRACKED** — no `order_intents` row the book can attribute the fill
  to, or an intent terminal for a reason the broker record does not
  refute. **Never projected.**
- **DEFERRED** — the commission is in a currency that cannot be translated
  to USD. Left completely alone so a later run can decide it again;
  publishing it would write an immutable audit row the projector then
  rejects, and the execution could never be recovered.

**Any UNTRACKED or DEFERRED row and `--apply` is refused** (exit 1). A
partial repair leaves the book half-right and entries still fail-closed,
which is the state you are trying to leave. Resolve them first.

Sanity-check the totals against the reconciliation report: the recovered
quantities should match `ib_qty` per ticker exactly.

## Step 3 — apply

```bash
python scripts/ops/restore_missed_entries.py \
    --statement ~/Downloads/DUN551088_trades_20260918.csv \
    --account DUN551088 --apply
```

Requires an interactive TTY and the exact confirmation
`RESTORE MISSED ENTRIES`. Before prompting it writes the rollback dump:

```
output/repairs/paper_state_pre_entry_restore_<stamp>.json
```

That dump covers `equity_snapshots`, `trades`, `positions`,
`portfolio_config`, **`order_intents` and `execution_fills`** — the last
two because this repair writes them and `dump_paper_state` alone does not
cover them.

On success it writes `output/repairs/entry_restore_<stamp>.json` recording
the statement's sha256, every execution applied with its economics, and
the equity note below.

## Step 4 — verify

```bash
python scripts/reconcile_paper.py --report
```

Required: `severity: ok` and `entries_allowed: true`. Anything else means
the repair was incomplete — do **not** re-run `--apply` blind; go back to
the dry run and read what it now reports.

Also confirm the next 04:52 digest counts the recovery: the pipeline
report's `fills:N (M recovered, 2-day window)` counts by *recovery* time,
not execution time, so a repair applied tonight shows tonight even though
the executions are days old.

## What is NOT repaired: the equity series

`equity_snapshots` is a **recorded** series — one row per day written from
the cash and market value as they stood that morning. For the span between
the executions and the repair, those rows overstate cash and understate
market value by the restored cost basis, because the book did not know it
held the positions.

Those rows are **not** rewritten. Correcting them would need per-position
daily marks that were never stored — the same limitation
[`backfill_restored_equity.py`](../../scripts/ops/backfill_restored_equity.py)
documents for the exit case, where the arithmetic happened to be
recoverable from a frozen cash column and here is not.

So the series is correct from the first snapshot after `applied_at`, and
the step between is **this repair, not a trade**. The applied artifact
dates and states that explicitly; quote it in any gate review covering the
affected dates.

## Rollback

Restore from `paper_state_pre_entry_restore_<stamp>.json`. Restoring
`order_intents` matters as much as the positions: leaving the intents
`FILLED` while removing the positions would hide the divergence from
reconciliation entirely.

## Refusals, and what each one means

| Refusal | Cause | Do this |
|---|---|---|
| `missing required column(s)` | Wrong Flex field set | Re-export with all thirteen |
| `unreadable DateTime` | Unsupported format | Re-export; UTC, one of the listed formats |
| `has no TradePrice` / `not usable` | Blank or zero economics | Re-export. Never fill in by hand |
| `not an IB paper account` | A `U*` account | Live ledgers are not reconstructed by script |
| `repeats TradeID` | Duplicate rows | De-duplicate the export |
| `no rows` | Empty export | Check the date range and section |
| `Refusing to apply` | UNTRACKED or DEFERRED present | Resolve those first |
| `--apply requires an interactive TTY` | Piped or automated | Run it by hand. This is deliberate |

## Background

The 2026-09-18 batch was lost to IB **Error 1101** (connectivity restored,
*data lost*), which `ib_executor` treated like 1102 (data maintained). A
1100/1101 leaves the API socket open, so `connect()` never re-runs,
`_reregister_open_trades()` never fires, and the per-`Trade` handlers stay
bound to objects IB has stopped pushing to. The executor went blind, then
repriced and cancelled orders IB had already filled. Fixed forward in
PR #184 — which stops the next occurrence and cannot reach back to these.
