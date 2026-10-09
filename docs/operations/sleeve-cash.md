# Sleeve cash: what caps a buy, and what deploying capital now requires

**Since KAN-111** every buy is capped at its sleeve's **ledger cash** —
`portfolio_configs.cash`, the number the fill projector refuses to take below
zero — net of the sleeve's open buy orders, the buys already accepted in the
same run, the order's estimated commission and a 25 bps buffer
(`currency.sleeve_cash_buffer_bps`). A buy that does not fit is downsized to
whole shares, or skipped:

```
CAP    ARKW   11.1691 -> 5.0000  [thematic_momentum] (downsized to sleeve cash: have $1,000.00, need $1,871.35)
SKIP   ARKW   11.1691 @ $  167.04  [thematic_momentum] (insufficient sleeve cash: have $24.83, need $1,871.35; 1 share needs $168.46)
```

The NAV budget (`capital.sleeve_budgets`, derived daily from broker NAV and
`max_deployable_usd`) still sets the risk engine's exposure headroom. It no
longer decides what a sleeve may *spend*: the ledger cash does. Same-day sell
proceeds do not count — the projector books a sale only when it fills — so a
rotation into a fully invested sleeve completes one run later.

## Deploying new capital: `scripts/ops/topup_sleeve_cash.py` (KAN-113)

A deposit, a raised `capital.<mode>.max_deployable_usd` or a higher
`deployment_fraction` grows the sleeves' NAV budgets, **but adds no buying
power** until each sleeve's `portfolio_config.cash` is raised to match.
Before KAN-111 such a buy was approved and then burned at the projector
(filled at IB, refused by the book, reconciliation `major`, entries disabled —
the 2026-10-07 ARKW incident). Now it is downsized or skipped instead, which
is safe but silent unless you read the report. Reconciliation compares
positions, not sleeve cash, so it will not flag an un-topped-up deposit.

The top-up is a write to the paper database, so under CLAUDE.md's
destructive-action rule **the operator runs `--apply`; an agent runs the dry
run at most.** Paper accounts (`DU*`) only.

### Which mode

| Situation | Mode |
|---|---|
| New money for the whole book (a deposit, a converted FX amount, a raised cap) | `--allocate N` — split by `CAPITAL_ALLOCATIONS` in `scripts/run_paper.py`, to the cent, remainder to the largest weight |
| New money for one sleeve (a deliberate re-weighting, a starved sleeve) | `--portfolio X --amount-usd N` |
| No new money; one sleeve has idle cash another needs (option B of the ARKW repair) | `--from A --to B --amount-usd N` |

A credit or allocation raises each sleeve's `cash` **and** `capital` (its
contributed capital, so the report's cash/capital ratio stays meaningful). A
transfer moves both from source to destination.

### Procedure

From the deploy clone, outside 04:00–07:00 SGT and not within 15 minutes of
a paper run's snapshot write (the run reads cash and then writes snapshots;
`--apply` refuses in either case, and re-checks both at the moment of the
write). `python` alone
imports another checkout, so set `PYTHONPATH`; the database comes from
`ALGO_DATABASE_URL`.

```bash
cd ~/algo-poc-deploy
# The password comes from the login keychain; never type it or put it in argv.
export ALGO_DATABASE_URL="postgresql://algo:$(security find-generic-password -s algo-poc -a POSTGRES_PASSWORD -w)@localhost:55432/algo_poc"

# 1. Dry run: every sleeve's cash before/after, totals, every pre-check.
PYTHONPATH=$PWD .venv/bin/python scripts/ops/topup_sleeve_cash.py \
    --account DUN551088 --allocate 20000 --note "deposit 2026-10-09"

# 2. If every check reads ok, IMMEDIATELY re-run with --apply and type
#    TOP UP SLEEVE CASH at the prompt.
PYTHONPATH=$PWD .venv/bin/python scripts/ops/topup_sleeve_cash.py \
    --account DUN551088 --allocate 20000 --note "deposit 2026-10-09" --apply

unset ALGO_DATABASE_URL
```

The dry run runs in a `READ ONLY` transaction on Postgres and writes nothing.
It refuses (exit 1, nothing written) when:

- the account is not a paper account (`DU*`);
- a sleeve does not exist, or is synthetic (`_*`, e.g. `__drill__`);
- the amount is not positive, finite and in whole cents;
- (credit/allocate) graded ledger capital after the credit — every graded
  sleeve's cash plus open positions at their last marks — exceeds
  `capital.paper.max_deployable_usd`, or today's deployable capital as
  `shared.capital.calculate_capital_budget` computes it from the broker
  snapshot (the paper run's own definition). The remedy is printed: raise
  `capital.paper.max_deployable_usd` first, or move existing cash with
  `--from/--to`. The dry run prints **contributed capital** (the sum of the
  `capital` column) beside the marked figure, so the gap between them — the
  book's profit and loss — is visible. On 2026-10-09 the book was already
  marked above $100,000 by profit alone, so any credit needed the cap raised
  first;
- (credit/allocate) IB does not back it: the account's USD cash
  (`TotalCashBalance`) less `currency.minimum_settled_usd_reserve` must cover
  every sleeve's ledger cash after the credit. The snapshot is read-only, on
  its own client id (61). `--skip-broker-check` passes this check as
  SKIPPED and the audit records that the credit was **not** verified — use it
  only when the Gateway is down and you have checked the balance yourself;
- (transfer) the source would hold less than its open buy reservations
  (`active_buy_reservations_for_account(portfolio=...)`, KAN-111's number);
- an identical flow (same mode, sleeves and amounts) was recorded in the last
  24 hours — pass `--allow-repeat` only if a second one is really meant.

Two timing checks — inside 04:00–07:00 SGT, or an equity snapshot written in
the last 15 minutes (a late or manual paper run) — are `[WARN]` in a plain dry
run, so you can preview at any hour, and refusals with `--apply`. A credit
that takes a sleeve above its share of deployable capital (deployable ×
`CAPITAL_ALLOCATIONS` weight) is a `[WARN]`, not a refusal: re-weighting a
sleeve on purpose is what `--portfolio` is for.

`--apply` additionally needs `--account`, an interactive TTY, a writable
artifact directory (checked before anything is written) and the exact phrase.
It is one transaction: `SELECT … FOR UPDATE` on the affected
`portfolio_config` rows (the projector's lock), a refusal if any sleeve's cash
moved since the dry run (a fill landed — re-run the dry run), the timing
checks again and the broker snapshot's age (at most 5 minutes) judged at the
moment of the write — so typing the phrase late is caught — then the update
and one `capital_adjustments` row per leg, stamped with the database clock
(`clock_timestamp()`) just before the commit. Any failure rolls all of it
back. After commit the book is re-read and compared with the plan: `capital`
and the flow rows must match exactly; a `cash` difference is reported as
"may be a fill that landed after commit" (the projector moves cash too) rather
than as a failure. An audit artifact
`topup-sleeve-cash-<UTC stamp>.json` is written under `output/reconciliation`
(never overwriting an existing file) with the plan, every check, the broker
evidence or the skip, the result and the verification.

### The flow is not a return

A credit raises sleeve equity on the next snapshot by the amount credited.
Read raw, that is a return (a $5,000 credit to a $20,000 sleeve is "+25%"),
it lifts the drawdown peak, and it can close a drawdown the sleeve is in.
So the tool records every leg in `capital_adjustments` — the table the
durable-ledger design reserved for funding events, written in the same
transaction as the cash, so a flow is recorded if and only if it happened —
and the readers remove it (`shared/capital_flows.py`):

- **Divergence monitor**: live returns are time-weighted (flow-adjusted),
  anchored so the newest value is the real equity. It prints a line naming
  the sleeve's recorded flows.
- **Epoch report and go-live gate drawdown** (`evidence_store.equity_series`):
  flow-adjusted, so a credit cannot close a drawdown.
- **Weekly digest**: the balance is real; the week's change is flow-adjusted
  and the line says `excl. +5,000.00 capital flows`.
- **Risk service `peak_nav`** (circuit breaker): flow-adjusted against
  today's NAV. Needs a `risk-management` image rebuild to take effect
  (`docs/operations/container-deploy.md`).
- **Rolling shadow** (the KAN-105 seeding question): the shadow is seeded
  from the **flow-adjusted** live curve (`live_equity_by_sleeve`), the same
  curve the monitor grades against, so both start level in today's capital
  and a window that spans a flow is graded **in full**. The verdict carries a
  note: `capital flow(s) inside this window (KAN-113): momentum +5,000.00 USD
  valued from <session> …`, on the sleeve and on AGGREGATE. The weekly digest
  adds a `📥 CAPITAL FLOW (KAN-113)` line while a flow is inside the
  30-session divergence window.

**A flow does not pause or reset anything.** The window never shrinks, so
AGGREGATE (which intersects sessions across sleeves) does not collapse after
an `--allocate`; the credited sleeve is graded every session, so a credit
neither turns sessions into NO_DATA (which would pause the epoch's scored
session count and the BREACH streak) nor produces a short-window OK that
would clear a running BREACH streak. A sleeve in BREACH before a credit is
still in BREACH after it unless its flow-adjusted returns say otherwise.
(Starting the shadow window after the flow was rejected for exactly this: it
collapses the window, and AGGREGATE with it, to one session.)

`equity_snapshots` is never rewritten; the adjustment happens in the reader,
every time, from the recorded rows. A flow is attributed to a snapshot by
time: it is inside every row written after it committed.

## Where to see it

The daily pipeline report (`deploy/launchd/run_pipeline_report.sh` →
`scripts/ops/pipeline_report_summary.py`) carries:

- `sleeve cash/capital: momentum $9,876/$23,080, …` — each graded sleeve's
  ledger cash against its recorded capital, from `portfolio_configs`. A sleeve
  near $0 buys nothing however large the account is.
- `cash-capped buys: N skipped / M downsized` — counted from the paper log's
  `insufficient sleeve cash` and `CAP` lines. A run of skips with healthy NAV
  is the signature of capital that was deployed but never topped up.

The sleeve-cash field goes live when the deploy clone is pulled (the summary
script is sourced by path); the cash-capped counts need
`deploy/launchd/deploy.sh`, because the wrapper is a copy in `~/ibc`.

## Defence in depth

The risk-management service re-checks every buy against the same cash with the
same function and refuses one that does not fit, paging
`sleeve_cash_rejection` (high). It should never fire: it means the paper run's
cap was bypassed or the book moved underneath it.
