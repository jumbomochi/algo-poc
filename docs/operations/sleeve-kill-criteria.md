# Per-Sleeve Kill Criteria

**Status:** Adopted 2026-08-17 (KAN-37)
**Owner:** Huiliang Lui (operator)
**Governs:** the six sleeves in `shared/universe.py::ACTIVE_SLEEVES`
**Rule this implements:** [project-direction.md](../designs/project-direction.md)
D3.3 — *"no written kill criteria → no live promotion"*, universal, no exemptions.

The six incumbent sleeves were grandfathered into the promotion pipeline via
epoch v2 (direction D9). Grandfathered is not exempt: each needs written kill
criteria before Rung 0 arms, and this document is them.

**These rules are mechanical on purpose.** The test of this document is not
whether it reads well — it is whether a reader can take any sleeve on any day,
walk the four triggers against the evidence store, and reach the same verdict
the operator would, without a judgement call the document does not specify. The
[worked example](#worked-example--a-dry-run-demotion-review) at the end is that
test, run on real verdicts.

Related: [capital ladder and promotion pipeline](../designs/project-direction.md) ·
[go-live checklist](go-live-checklist.md) ·
[IPS](investment-policy-statement.md) ·
[divergence monitor](divergence-monitor.md) ·
[backtest baseline](backtest-baseline.md) ·
[edge-validation framework](edge-validation-framework.md)

---

## The four triggers

A live or paper sleeve is **demoted one stage** when **any** of these fires.
One stage means `live → paper` or `paper → shadow` — never two at once, and
never straight to deletion.

| # | Trigger | Fires when | Evaluated by |
|---|---|---|---|
| 1 | **Divergence** | the sleeve's divergence status is a persisting `BREACH` for **10 consecutive sessions** against the baseline pinned in the epoch manifest | `shared.evidence_store.breach_streak(...).fires` |
| 2 | **Drawdown** | the sleeve's own peak-to-trough equity decline exceeds its written budget below | `shared.evidence_store.max_drawdown_pct` over that sleeve's rows |
| 3 | **Signal staleness** | the sleeve's named data source is degraded or deprecated for **more than 5 sessions** | operator check, source named per sleeve below |
| 4 | **Safety incident** | any safety incident attributable to the sleeve | incident record |

Four properties of these triggers that are easy to get wrong, so they are
stated rather than assumed:

- **`WARNING` is not a breach.** Only `BREACH` counts toward the streak
  (direction D11). A run of ten `WARNING` sessions demotes nothing.
- **The streak is per baseline.** `divergence_daily` is unique on
  `(sleeve, session_date, baseline_id)`, and `breach_streak` refuses to count
  across baseline ids. A rebaseline mid-window resets the count — deliberately.
  Verdicts scored against a superseded artifact are history, not evidence for
  the current epoch.
- **A blind monitor demotes nothing, and pauses the clock.** `NO_DATA`
  sessions and sessions with no verdict row at all count neither way. But
  blindness running for **more than 5 consecutive** sessions is itself trigger
  4 — a safety incident (`shared.evidence_store.blindness(...).is_safety_incident`,
  strictly greater, so five is not an incident and six is).
- **One event, one response.** Per direction D16 the precedence chain is
  safety halt → sleeve demotion → rung de-scale, and each level subsumes the
  ones below it. An incident that halts the system, demotes a sleeve and
  breaches the epoch produces the *highest* applicable response plus its
  subsumed records — never three separate punishments.

### After a demotion

- The sleeve's weight is **not** redistributed automatically; the rebalance is
  an operator action logged at the next review, under IPS §9.
- A demoted sleeve may re-earn its stage through the promotion pipeline's
  gates (S → P → L). Nothing skips a gate on the grounds of having held that
  stage before.
- **Retirement (deleting the sleeve) requires a logged decision.** Demotion is
  mechanical; retirement is not, and must not be.

### Where the numbers come from

`breach_streak` and `blindness` read `divergence_daily` in the evidence store.
**That table has no writer yet** — the monitor emits its verdicts to
`output/divergence_<YYYYMMDD>.json` and KAN-27 is the story that persists them.
Until KAN-27 lands, walk the identical rule over those JSON artifacts: each
file's `reports[]` carries one `status` per sleeve, and a missing file for an
NYSE trading day is a blind session exactly as an absent row would be. The
verdict is the same; only the reader changes.

There is no per-sleeve drawdown helper. `equity_series` sums across sleeves
because the go-live gate grades the account, not a sleeve. For trigger 2, read
`equity_snapshots` filtered to one `portfolio` and pass those rows to
`max_drawdown_pct`, which takes the series directly:

```sql
SELECT date, equity, market_value
FROM equity_snapshots
WHERE portfolio = 'momentum'          -- one sleeve, never the '_'-prefixed rows
ORDER BY date;
```

---

## Drawdown budgets

The default budget is **the sleeve's backtest max drawdown × 1.5**, per
direction D3.3, rounded to two decimals. Written as a number, not a formula,
so that reading it requires no arithmetic under pressure.

> ### ⚠ These budgets are PROVISIONAL and **do not authorize Rung 0**
>
> They are derived from `output/backtest_multi_20260804_075705.json`, whose
> `config` block has no `fill_model`, no `point_in_time_universe` and no
> `coverage` — it is the same-bar, survivorship-biased artifact that
> [backtest-baseline.md](backtest-baseline.md) exists to replace, and the
> divergence monitor already refuses it as not like-for-like.
>
> KAN-23 shipped the rebaseline machinery (membership snapshot, historical
> sectors, refresh guards); **the regeneration run itself has not happened.**
> Until it does and this table is recomputed against the point-in-time
> artifact, these budgets serve to make the rule executable in paper — they do
> not authorize arming the live account at Rung 0.
>
> **To lift this block:** regenerate per
> [backtest-baseline.md § Regenerating the headline baseline](backtest-baseline.md#regenerating-the-headline-baseline),
> confirm `config.coverage.state` is `OK`, then recompute every row below from
> that artifact's per-portfolio `metrics.max_drawdown`, change the Source line
> to its filename, and set each Status to `FINAL`. Expect the budgets to
> **widen**: next-open fills, point-in-time membership and the commission floor
> all make the backtest drawdowns worse, and a budget derived from an
> optimistic backtest is tighter than the strategy it governs — it fires on
> normal behaviour, and a trigger that cries wolf is a trigger that gets
> overridden.

**Source:** `output/backtest_multi_20260804_075705.json` (10-year run,
2026-08-04) — *not like-for-like; see the block above.*

| Sleeve | Backtest max-DD | Budget (×1.5) | Status |
|---|---:|---:|---|
| `momentum` | 16.22% | 24.33% | PROVISIONAL |
| `sector_rotation` | 15.62% | 23.43% | PROVISIONAL |
| `thematic_momentum` | 10.23% | 15.35% | PROVISIONAL |
| `quality_value` | 11.13% | 16.70% | PROVISIONAL |
| `earnings_drift` | 6.46% | 9.69% | PROVISIONAL |
| `tail_risk_hedge` | 14.63% | 21.95% | PROVISIONAL |

These are **sleeve-level** budgets measured on that sleeve's own equity. They
sit below the account-level bounds and never replace them: the IPS §6 10%
drawdown pause and 20% circuit breaker act on the whole book, and the ladder's
12% Gate-3 bound grades the epoch. A sleeve can breach its budget while the
account is fine — that is the point of having both.

---

## Per-sleeve criteria

Weights are the `CAPITAL_ALLOCATIONS` in `scripts/run_paper.py`, which
`tests/shared/test_universe.py` holds equal to `ACTIVE_SLEEVES`.

Every sleeve's price data comes from **IB Gateway daily bars** via the paper
runner; where a sleeve has an *additional* dependency, that is the one named
under trigger 3, because it is the one that can rot without the whole system
noticing.

### `momentum`

**Weight:** 23.08% · **Universe:** `SP500_TOP50` + `SH`, `PSQ` (inverse ETFs)

| Trigger | Concrete form |
|---|---|
| Divergence | `BREACH` for 10 consecutive sessions against the epoch manifest's baseline id |
| Drawdown | sleeve equity decline exceeds **24.33%** |
| Signal staleness | IB daily bars unavailable for **more than 5 consecutive** sessions for ≥20% of `SP500_TOP50`. This sleeve is price-only, so bars *are* the signal — a gateway outage that survives the watchdog is signal death, not just an ops problem |
| Safety incident | any halt, unattributed order, or silent failure traced to this sleeve |

**Demotion:** one stage. Re-promotion via the pipeline.
**Note:** its backtest ranking is gated by `data/universe/sp500_membership.json`;
live ranking uses the static `SP500_TOP50`. A stale membership file corrupts the
*baseline*, which surfaces as trigger 1 going `NO_DATA`, not as staleness here.

### `sector_rotation`

**Weight:** 15.38% · **Universe:** the 11 SPDR sector ETFs

| Trigger | Concrete form |
|---|---|
| Divergence | `BREACH` for 10 consecutive sessions against the epoch manifest's baseline id |
| Drawdown | sleeve equity decline exceeds **23.43%** |
| Signal staleness | IB daily bars unavailable for **more than 5 consecutive** sessions for any of the 11 sector ETFs, **or** the breadth regime (`compute_regime_by_date`, 200-day MA over the bar universe) unavailable for the same span — the sleeve rotates defensive on regime, so a missing regime silently pins it to `bull` |
| Safety incident | any halt, unattributed order, or silent failure traced to this sleeve |

**Demotion:** one stage. Re-promotion via the pipeline.

### `thematic_momentum`

**Weight:** 14.10% · **Universe:** the 25 thematic ETFs in `THEMATIC_ETFS`

| Trigger | Concrete form |
|---|---|
| Divergence | `BREACH` for 10 consecutive sessions against the epoch manifest's baseline id |
| Drawdown | sleeve equity decline exceeds **15.35%** |
| Signal staleness | IB daily bars unavailable for **more than 5 consecutive** sessions for ≥20% of `THEMATIC_ETFS`. This is the sleeve most exposed to **fund closure**: a thematic ETF that liquidates stops printing permanently, and 5 sessions of no bars in a name is the same observation whether the cause is IB or delisting. A confirmed closure is a universe amendment under IPS §9, not a wait |
| Safety incident | any halt, unattributed order, or silent failure traced to this sleeve |

**Demotion:** one stage. Re-promotion via the pipeline.
**Note:** thematic ETFs carry the widest slippage assumption in the cost model
(×2.5); a divergence breach here is more likely to be execution cost than
signal decay. That changes the remedy, not the trigger.

### `quality_value`

**Weight:** 15.38% · **Universe:** `SP500_TOP100`

| Trigger | Concrete form |
|---|---|
| Divergence | `BREACH` for 10 consecutive sessions against the epoch manifest's baseline id |
| Drawdown | sleeve equity decline exceeds **16.70%** |
| Signal staleness | **the yfinance quarterly-fundamentals feed** (`scripts/fetch_fundamentals.py` → `data/cache/fundamentals.json`) failing to refresh, or returning no ROE / D/E / margin for ≥20% of `SP500_TOP100`, for **more than 5 consecutive** sessions. yfinance is an unofficial scraper with no contract; this and `earnings_drift` are the only sleeves whose signal can die while every price still prints |
| Safety incident | any halt, unattributed order, or silent failure traced to this sleeve |

**Demotion:** one stage. Re-promotion via the pipeline.
**Note:** a stale cache degrades quietly — the composite score is computed from
whatever the cache holds, so old fundamentals produce confident, wrong rankings
rather than an error. Check the cache's freshness, not just its existence.

### `earnings_drift`

**Weight:** 19.23% · **Universe:** `SP500_TOP100`

| Trigger | Concrete form |
|---|---|
| Divergence | `BREACH` for 10 consecutive sessions against the epoch manifest's baseline id |
| Drawdown | sleeve equity decline exceeds **9.69%** |
| Signal staleness | **the yfinance earnings feed** (`scripts/fetch_earnings.py` → `data/cache/earnings.json`) failing to refresh, or carrying no announcement within the last 5 sessions during a reporting season, for **more than 5 consecutive** sessions. The sleeve enters within 2 days of an announcement, so a missing calendar produces *no trades* rather than bad trades — silence is the failure mode |
| Safety incident | any halt, unattributed order, or silent failure traced to this sleeve |

**Demotion:** one stage. Re-promotion via the pipeline.
**Note:** this sleeve carries the tightest budget (9.69%) because its backtest
drawdown was the smallest — a 20-day max hold and a 6% trailing stop. Tight
budget plus the largest weight (19.23%) means it is the likeliest of the six to
trip trigger 2 first. That is expected, not a defect in the budget.

### `tail_risk_hedge`

**Weight:** 12.83% · **Universe:** `SH`, `PSQ`, `SDS`, `TLT`, `GLD`

| Trigger | Concrete form |
|---|---|
| Divergence | `BREACH` for 10 consecutive sessions against the epoch manifest's baseline id |
| Drawdown | sleeve equity decline exceeds **21.95%** |
| Signal staleness | IB daily bars unavailable for **more than 5 consecutive** sessions for any of the five instruments, **or** the breadth regime unavailable for the same span — the sleeve's entire allocation is a function of regime, so a missing regime defaults it to the `bull` basket (GLD/TLT) and silently removes the hedge |
| Safety incident | any halt, unattributed order, or silent failure traced to this sleeve |

**Demotion:** one stage. Re-promotion via the pipeline.
**Note:** this sleeve is *expected* to lose money in a bull regime — its
backtest total return is negative. Trigger 2 is therefore the only performance
trigger that applies to it; do **not** read a losing quarter as evidence of
decay. Its job is the account's drawdown profile, which is the IPS §1 thesis.

---

## Promotion funding rule

Per direction D9, when a new sleeve is promoted to live:

- It takes weight **pro-rata from the incumbents** — every incumbent gives up
  the same *fraction* of its weight, so the relative shape of the book is
  unchanged.
- The new sleeve is capped at **10% of portfolio per promotion**. A sleeve that
  should hold more earns it across later promotions, never in one step.
- The rebalance is **logged** as a decision, with the resulting weights, and
  `CAPITAL_ALLOCATIONS` / `ACTIVE_SLEEVES` are updated together — the test in
  `tests/shared/test_universe.py` fails if they disagree.
- A promotion is a weight change, so IPS §9 applies: monthly review only.

The mirror case — a demotion — does not auto-redistribute. The vacated weight
sits in cash until an operator rebalances at a review.

---

## Worked example — a dry-run demotion review

Real verdicts, `momentum`, the sessions ending 2026-08-14. This is the proof
that the rules above are executable: every step resolves against data, and the
answer it produces is **not** the answer eyeballing the log would give.

**The raw record** (from `output/divergence_*.json`; a report generated at
04:45 SGT scores the previous US session):

| US session | `momentum` status | Baseline artifact |
|---|---|---|
| 2026-07-21 → 2026-07-24 | `BREACH` ×4 | `backtest_multi_20260721_053247.json` |
| 2026-07-27 → 2026-07-31 | `BREACH` ×5 | `backtest_multi_20260728_053111.json` |
| 2026-08-03 → 2026-08-07 | `BREACH` ×5 | `backtest_multi_20260804_075705.json` |
| 2026-08-10, 08-11 | `NO_DATA` | `backtest_multi_20260804_075705.json` |
| 2026-08-12, 08-13 | *no report* | — |
| 2026-08-14 | `NO_DATA` | `backtest_multi_20260804_075705.json` |

**Step 1 — trigger 1, divergence.** Fourteen consecutive `BREACH` sessions is
well past the 10-session threshold, and reading the log would demote the sleeve
on the spot. The rule does not. `breach_streak` counts only within one
`baseline_id`, and the weekly refresh rebaselined twice inside that run. The
longest single-baseline streak is **5** (2026-08-03 → 2026-08-07), and the
2026-08-10 `NO_DATA` ends it. 5 < 10 → **does not fire.**

That is the correct answer, not a technicality: three different baselines mean
the "14-session breach" is three unrelated measurements laid end to end, and the
2026-08-11 comparability check retroactively showed all three baselines were
same-bar and survivorship-biased — the monitor was scoring against artifacts it
now refuses. There is no epoch v2 manifest for this period either, so there is
no pinned `baseline_id` to score against at all.

**Step 2 — trigger 3, staleness.** IB bars printed throughout (the sleeve had
live equity rows every session). yfinance is not one of this sleeve's sources.
**Does not fire.**

**Step 3 — trigger 2, drawdown.** Read `equity_snapshots` for
`portfolio = 'momentum'` over the window and pass it to `max_drawdown_pct`;
compare to 24.33%. *(Provisional budget — per the block above, a fire here is
actionable in paper but does not by itself authorize a live decision.)*

**Step 4 — trigger 4, safety incident.** Now the one that matters. Sessions
2026-08-10 through 2026-08-14 are, in order: `NO_DATA`, `NO_DATA`, absent,
absent, `NO_DATA`. `blindness()` increments its run on both an all-`NO_DATA`
session and an absent one, so `longest_consecutive = 5`. The rule is blindness
*exceeding* 5 consecutive sessions, and `is_safety_incident` is strictly
greater — so **5 is not an incident, and the next blind session is.** The
monitor is still refusing the baseline as not like-for-like, so the next
session will be `NO_DATA` too.

**Verdict: no demotion — and the account is one session from a safety
incident.** Nobody reading the divergence verdicts would have found that; the
breach that looked alarming is noise, and the real finding is the silence
behind it. The two absent reports are the 2026-08-13/14 secrets outage
(KAN-16), which is also why 2026-08-13 is a permanent hole in the gate
evidence.

**Every step resolved without a judgement call.** That is the AC6 test, passed.

---

## Amendments

Changes to this document follow IPS §9: dated, with a written rationale, and
logged here. Kill criteria may be **tightened** at any time; **loosening** a
budget or a threshold while the sleeve is below its prior equity peak is
forbidden, exactly as IPS §9 forbids loosening a risk limit during a drawdown.

| Date | Change | Rationale |
|---|---|---|
| 2026-08-17 | Initial adoption (KAN-37) | Direction D3.3/D9. Six incumbent sleeves given written criteria before Rung 0; drawdown budgets provisional pending the KAN-23 point-in-time regeneration. |
