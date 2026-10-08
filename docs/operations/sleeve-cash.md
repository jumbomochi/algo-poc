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

## Deploying new capital requires a sleeve-cash top-up — no tool exists yet

A deposit, a raised `capital.<mode>.max_deployable_usd` or a higher
`deployment_fraction` grows the sleeves' NAV budgets, **but adds no buying
power** until each sleeve's `portfolio_configs.cash` is raised to match.
Before KAN-111 such a buy was approved and then burned at the projector
(filled at IB, refused by the book, reconciliation `major`, entries disabled —
the 2026-10-07 ARKW incident). Now it is downsized or skipped instead, which
is safe but silent unless you read the report.

- **No tool performs the top-up yet.** It is tracked as a follow-up ticket
  (raised from the KAN-111 review).
- It is a write to the paper/live database, so under CLAUDE.md's destructive-
  action rule **only the operator may do it**, by hand, after deciding how the
  new cash splits across sleeves (`CAPITAL_ALLOCATIONS` in
  `scripts/run_paper.py`). An agent must not run it.
- Reconciliation compares positions, not sleeve cash, so it will not flag an
  un-topped-up deposit.

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
