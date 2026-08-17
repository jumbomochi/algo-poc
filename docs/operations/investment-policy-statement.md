# Investment Policy Statement — algo-poc

**Status:** Adopted 2026-06-11
**Owner:** Huiliang Lui (operator)
**Review cadence:** Monthly (see § 8)
**Supersedes:** none

This Investment Policy Statement (IPS) governs the algo-poc automated US-equities
trading system. It is the **prerequisite document for phase-1 go-live** and sits
one level above the operational gates and triggers:

- [Go-Live Checklist](go-live-checklist.md) — the 8 promotion gates
- [Rollback Playbook](rollback-playbook.md) — live→paper rollback triggers + procedure
- [Divergence Monitor](divergence-monitor.md) — daily live-vs-backtest comparison
- [Active Portfolio Configuration](../strategies/portfolio-2026-05.md) — the running sleeves
- Risk constants: `config/default.yaml` § `risk`

Where this document states a number that also lives in code or another doc, the
**other source is authoritative** and this IPS links to it. Pre-committed
*personal* numbers (retirement trigger, review cadence, deployment philosophy)
originate here.

---

## 1. Purpose & role

The system is a **satellite**, not core capital. Core long-term savings remain in
**Amundi CW8 (MSCI World)**. The system exists to add a return/drawdown profile the
core cannot, while keeping skin in the game small enough that a total loss does not
impair core goals.

Its honest value is **three-fold**, in priority order:

1. **Drawdown protection** — backtested max DD ~11.6% vs ~30% for CW8 over the same
   horizon.
2. **Skill-building & optionality** — operating a live systematic book is a durable
   capability; the infrastructure has option value beyond this strategy.
3. **Return** — secondary. The headline edge is ~+6 pp CAGR vs CW8 (corrected
   2026-07-10 for the exposure-gate fix), of which only **~+1.8 pp is durable skill
   alpha**; the remainder is a US-vs-international concentration premium that is
   regime-dependent and may fade or invert. The IPS
   treats return as a *bonus on top of* drawdown protection, not the thesis.

The operator commits to not rationalizing the strategy upward into "core" status
on the strength of a good run. Promotion past satellite scale is governed by the
capital-scaling ladder (§ 5, superseded — see the banner there), not by
enthusiasm.

---

## 2. Investor profile

| Attribute | Value |
|---|---|
| Role of this capital | Satellite / experimental |
| Time horizon | Indefinite, but reviewed monthly and retired on the § 7 trigger |
| Risk tolerance (this sleeve) | Moderate-high — explicitly accepts regime risk, capped by drawdown limits |
| Core holding | Amundi CW8 (MSCI World) — unaffected by this system |
| Liquidity need from this capital | None — must not be relied on for spending |

---

## 3. Strategic asset allocation

Recommended household allocation between core and system:

- **70% Amundi CW8 / 30% system** as a target satellite weight *once the system is
  proven live*.
- During the smoke-test and early scaling phases (§ 5), the system's share is far
  below 30% by dollar — the 70/30 target is a *ceiling on satellite weight*, not a
  deployment instruction.

**Rebalancing rule:** the system's share of the combined (core + system) book is
checked at each monthly review. If the system's *value* drifts above its target
satellite weight purely from gains, gains may be left to run (the drawdown limits
in § 6 and the retirement trigger in § 7 are the real backstops). New *contributions*
of capital are governed by § 5, never by a mechanical rebalance into the system.

---

## 4. Strategy constraints

The system runs the **6-sleeve configuration** documented in
[portfolio-2026-05.md](../strategies/portfolio-2026-05.md). Weights (on the $100K
reference basis):

| Sleeve | Weight | Universe |
|---|---:|---|
| `momentum` | 23.08% | SP500_TOP50 + BEAR_TICKERS |
| `earnings_drift` | 19.23% | SP500_TOP100 |
| `sector_rotation` | 15.38% | SECTOR_ETFS |
| `quality_value` | 15.38% | SP500_TOP100 |
| `thematic_momentum` | 14.10% | THEMATIC_ETFS |
| `tail_risk_hedge` | 12.83% | DEFENSIVE_TICKERS |

> **⚠ Rung-0 allocation overlay, 2026-08-17 (amendment below, KAN-34 / direction
> D8).** The weights above remain the configuration of record and are unchanged.
> **At Rung 0 only (5,000 SGD ≈ USD 3,700), the live account runs `momentum`
> alone at 100%**; the other five sleeves are **suspended, not retired**, and
> return at higher rungs by between-epoch amendment. Reason: at Rung 0 the
> six-way split sizes positions at $34–119, below one whole share of most of the
> universe — `quality_value` filled 0 of 7,395 entry signals over ten years.
> Two consequences govern here rather than in the memo:
>
> - **No standing crash hedge at Rung 0.** `tail_risk_hedge` is suspended.
> - **The crash entry freeze becomes a total trading freeze.** Every sleeve
>   except `tail_risk_hedge` is wrapped by the freeze, so in a crash regime a
>   one-sleeve book opens no positions at all — including the `SH`/`PSQ`
>   rotation inside `momentum`. Exits are never blocked and the 10% trailing
>   stop continues to fire. This is accepted as a conservative posture at
>   smoke-test size, not overlooked.
>
> The overlay is conditional on `momentum`'s edge verdict of record (direction
> D10); if it fails, the decision reopens and Rung 0 does not arm on it. Full
> decision: [rung0-economics.md §9](rung0-economics.md).

**Hard constraints:**

- **No leverage** beyond what the sleeve definitions already imply; `total_exposure_limit_pct`
  (150%) in config is a ceiling, not a target.
- **No discretionary overrides** of system signals while live. The operator may
  halt (§ 7) but may not hand-pick trades.
- **No new sleeves or weight changes** except at a monthly review (§ 9 governance).
- **Dropped sleeves stay dropped** until their pre-committed revival conditions
  hold — see [mean-reversion-failure-analysis.md](../strategies/mean-reversion-failure-analysis.md).
- Universe stays US-listed equities/ETFs as defined per sleeve. No new asset classes
  without a documented IPS amendment.

---

## 5. Capital deployment — SUPERSEDED by the capital-scaling ladder

> **⚠ Superseded in full, 2026-08-17 (amendment below, KAN-37).** The
> deployment path in this section — the illustrative 25K → 50K → 100K steps
> and their monthly-review conditions — is **superseded in full** by the
> **capital-scaling ladder** in
> [project-direction.md](../designs/project-direction.md). Where the two
> disagree, the ladder governs. This section is retained for the record and
> for the parts the ladder does not touch (the funding-currency/FX rules
> below, which remain in force).
>
> **What the ladder changes.** Deployment moves from operator-discretion steps
> to four written rungs (5,000 / 10,000 / 20,000 / 40,000 SGD of *cumulative
> funding deployed*), each entered only on a **clean epoch** at the rung
> below. The clean-epoch criteria replace §5's per-step conditions: the full
> 8-gate re-clear is retained only at **Rung 0 entry** and after any Rung-0
> disarm, and the capacity review at **Rung 3** — not at every step. The
> ≥20-day divergence-OK window is replaced by the epoch-matched
> 30-session rule. Retained gains do not raise a rung; losses do not lower one
> — only the written breach criteria act.
>
> **The divergence monitor is now a capital-decision input by rule.**
> [divergence-monitor.md](divergence-monitor.md) states the monitor "is not a
> kill switch", and as an *automated* control that remains true — it still
> halts nothing on its own. But the ladder deliberately makes its verdicts
> binding on capital: a persisting `BREACH` run of 10 consecutive sessions
> demotes a sleeve ([sleeve-kill-criteria.md](sleeve-kill-criteria.md)), and an
> out-of-band run breaks the epoch and de-scales a rung. This resolves the
> contradiction in writing rather than leaving it to be discovered on gate day:
> **the monitor does not act, and its output decides.**
>
> **Standing constraints the ladder does NOT override.** Every rung remains
> subject to both:
> - the **30% household satellite ceiling by dollar** (§ 3) — the ladder sizes
>   the system, it does not raise the satellite's share of the household book;
> - **tax / W-8BEN administrative closure before any scale-up** (§ 10) — the
>   open item there is a precondition on scaling, not a footnote.
>
> **Amendment procedure.** Ladder edits are permitted only *between* epochs,
> never during one, and are logged as decisions — the same discipline § 9
> applies to this document.

Deployment is **gated by realized risk behavior, not by a fixed dollar ceiling.**
The operator has chosen *not* to set an absolute hard cap: the system may scale
indefinitely **so long as the drawdown limits in § 6 and § 7 continue to be
satisfied in live trading.** Drawdown discipline is the cap.

**Deployment path:**

1. **Smoke test — 5,000 SGD (~$3,700 USD), ~2 months.** First real-money
   deployment; wired 2026-06-16. Purpose is to validate execution, reconciliation,
   slippage, and the daily ops loop on live fills — *not* to prove return. Success =
   the operational gates behave, not a P&L target.
2. **Scale up** in steps thereafter (illustratively ~25K → ~50K → ~100K SGD →
   beyond), each step contingent on:
   - All 8 [go-live / continuation gates](go-live-checklist.md) passing at the
     current size.
   - [Divergence monitor](divergence-monitor.md) reporting **OK** for a sustained
     window (≥ 20 consecutive trading days) at the current size.
   - Live max drawdown at the current size **≤ 12%** (the Gate-3 bound).
   - No § 7 conditions tripped.
3. **No ceiling**, but every scale-up step is a deliberate decision made at a
   monthly review, never automatic. Step size is at operator discretion but should
   not more than ~double deployed capital in a single step before re-clearing gates.

If at any size the drawdown limits are breached, scaling **stops** and the relevant
§ 6 / § 7 action applies. Scaling only resumes after a clean re-clear of the gates.

### Funding currency & FX

Capital is wired in **SGD**, but the system trades **USD-denominated** US-listed
instruments (§ 4). Each deployment therefore carries an FX leg (SGD→USD, via IBKR
auto-conversion or a manual `IDEALPRO` trade). Consequences for this policy:

- **NAV is measured in USD**, the trading currency. All percent-based limits in § 6
  and the drawdown/retirement triggers in § 7 apply to the **USD** NAV, so they are
  unaffected by SGD/USD moves. The SGD figures above are funding amounts; the
  USD-equivalent at conversion is the working capital the limits act on.
- **SGD/USD FX risk** sits *outside* the strategy's measured P&L — a falling USD
  erodes SGD-terms returns independently of system performance. This is an accepted,
  unhedged exposure of holding a USD book funded in SGD; it is **not** a reason to
  halt under § 7 (which concerns deployed-capital drawdown, measured in USD).
- IBKR also provides access to non-US venues (e.g. LSE). This system does **not**
  trade them; any such use is outside this IPS and would require a § 9 universe
  amendment.

---

## 6. Risk limits (authoritative source: `config/default.yaml` § `risk`)

These limits are configured in `config/default.yaml` and are intended to be
enforced in code. The IPS records them so the operator cannot quietly loosen
them; **risk limits are never loosened during a drawdown** (§ 9).

> **2026-08-06 review correction / enforcement status.** The
> [implementation review](implementation-review-2026-08-06.md) found the trailing
> **stop-loss**, the **passive hard-ceiling / margin auto-trim**, the drawdown
> **circuit-breaker _liquidation_**, and the drawdown **gauge** were implemented
> but never invoked by the running risk service (the gauge measured the deployment
> budget rather than book equity, so the 10% pause / 20% breaker read a phantom
> ~0% in the capped regime).
>
> **T2 (#3) — landed.** A periodic driver on the passive-scan interval runs the
> trailing stop-loss and hard-ceiling auto-trim on the live path, and the drawdown
> gauge is measured on **real marked book equity** (cash + MTM), so the 10% pause
> engages on a real drawdown.
>
> **T1 (#2) — landed.** The kill switch is now **durable and fail-closed**: a kill
> is persisted (`system_halt` table) and reloaded on restart, so a restart after a
> kill stays halted until an explicit human clear via `DELETE /api/v1/kill`
> (admin-only). The **20% circuit breaker now liquidates** (activates the halt and
> flattens the book), not merely pauses buys. Kill/breaker liquidation reloads
> authoritative DB positions and routes each exit through the OrderLedger with a
> deterministic per-event id, so a replayed kill does not double-sell and exits
> actually reach IB; each position is guarded and the critical alert always fires.
>
> **Still pending:** the stop-loss / hard-ceiling auto-trim exits are *emitted* by
> the risk service but are not yet routed through the ledger the way the kill path
> is, so their end-to-end execution + a re-fire guard is tracked with **T4/T7**;
> execution's intent-less direct liquidation in the risk-down case depends on
> **T7**'s broker tracking for cross-restart idempotency. **Margin-critical
> auto-trim** stays alert-only (no margin-utilization data is plumbed).
> See § 12 of the review for the findings register.

Enforcement column: **auto** = system acts (buy paused) without a human;
**emit** = risk service emits the order but end-to-end execution lands with T1;
**alert** = operator is notified but must act; **pending** = declared, not yet wired.

| Limit | Value | Config key | Enforced |
|---|---:|---|---|
| Position entry limit | 5% of NAV | `position_entry_limit_pct` | auto |
| Sector concentration | 20% of NAV | `sector_concentration_pct` | auto |
| Trailing stop-loss | 15% | `stop_loss_trailing_pct` | emit (T2); executes with T1 |
| Drawdown — pause new buys | 10% | `drawdown_pause_pct` | auto (T2: on book equity) |
| Drawdown — circuit breaker (liquidate all) | 20% | `drawdown_circuit_breaker_pct` | auto (T1: halts + liquidates) |
| Position soft ceiling (notify) | 7% | `soft_ceiling_pct` | alert |
| Position hard ceiling (auto-trim to soft) | 15% | `hard_ceiling_pct` | emit (T2); executes with T1 |
| Margin warning / critical | 70% / 85% | `margin_warning_pct` / `margin_critical_pct` | pending (no margin data) |
| Correlation alert | 0.70 | `correlation_alert_threshold` | alert |

The 10% drawdown pause and 20% circuit breaker are **system-level automated**
responses. The retirement trigger in § 7 is a **human, account-level** decision that
sits above them.

---

## 7. Halt & retirement triggers

Pre-committed so future-self cannot talk current-self out of pulling the plug.

### Halt (reversible — roll back to paper)

Any [rollback trigger](rollback-playbook.md) fires the documented live→paper
procedure: kill-switch/circuit-breaker event, unresolved reconciliation discrepancy,
critical observability outage, or 3 consecutive sessions of slippage/fill-quality
breach. A halt is recoverable — the system can be re-promoted after re-clearing the
gates.

### Retire (terminal — shut the strategy down)

**The system is retired entirely — not merely rolled back — when cumulative loss on
deployed system capital reaches −25%.**

- Measured as peak-to-current on *deployed* system capital (cumulative, across the
  life of the deployment — not a single-drawdown reading).
- This is *more lenient* than the 20% automated circuit breaker by design: the
  circuit breaker liquidates and pauses; the −25% retirement decision ends the
  program. The gap between 20% and 25% is the operator's explicit acceptance of
  regime risk before giving up.
- At −25%: liquidate, switch to paper, write a post-mortem, and do **not** redeploy
  real capital without a written, dated IPS amendment justifying revival.

---

## 8. Monitoring & review cadence

- **Daily (automated):** the [divergence monitor](divergence-monitor.md) runs after
  the paper/live signal job (launchd job `local.algo-divergence-monitor`, 04:45 SGT
  Tue–Sat; see `deploy/launchd/`). Two-axis OK/WARNING/BREACH classification; exits
  non-zero on breach for alerting. A `local.algo-gateway-watchdog` job keeps the IB
  Gateway connection self-healing.
- **Monthly (operator):** formal review. Agenda:
  1. Divergence-monitor history for the month (any WARNING/BREACH days, and why).
  2. Live performance vs backtest expectation; live max drawdown vs the § 6 bounds.
  3. Execution quality — slippage (bps), failed-order rate, reconciliation status.
  4. Whether a scale-up step (§ 5) is warranted, or scaling should hold/reverse.
  5. Any sleeve-weight or config change proposals (subject to § 9).
  6. Distance to the § 7 retirement trigger.
- **Event-driven (ad-hoc):** any divergence-monitor BREACH, any circuit-breaker
  event, or any rollback trigger forces an immediate review independent of cadence.

---

## 9. Governance — who can change what, and when

- **Weights & universe:** changed **only at a monthly review**, with the rationale
  written down. Never mid-month, never reactively chasing a hot sleeve.
- **Risk limits (§ 6):** may be *tightened* at any time. May **never be loosened
  during an active drawdown** (defined as any period where the system is below its
  prior equity peak). Loosening outside a drawdown requires a written IPS amendment.
- **Retirement trigger (§ 7):** may not be loosened (made more lenient than −25%)
  while the system is in a drawdown. Tightening is always allowed.
- **Deployment / scale-up:** governed by the capital-scaling ladder, not by
  operator discretion — a rung is entered when its written criteria are met and
  left when they are breached. The monthly review is where the ladder's state is
  *read*, no longer where the step size is chosen. Amending the ladder itself is
  permitted only between epochs, and is logged like any amendment here.
- **Sleeve demotion:** mechanical, per
  [sleeve-kill-criteria.md](sleeve-kill-criteria.md) — not a review decision.
  **Retirement** of a sleeve is, and requires a logged decision.
- **All amendments to this IPS** are dated and appended to § 11 with a one-line
  rationale. The git history of this file is the amendment log.

---

## 10. Tax & accounting notes

- Trades are US-listed equities/ETFs executed via Interactive Brokers; capital is
  funded in **SGD** and converted to **USD** to trade (§ 5).
- Tax residency and the resulting treatment (withholding, capital-gains reporting,
  wash-sale handling) are the operator's responsibility and **not** automated by the
  system. As a non-US person, a **W-8BEN** governs US withholding (notably ~30% on
  US dividends; the tail_risk_hedge and sector sleeves hold dividend-paying ETFs).
  *(Open: confirm Singapore-residency reporting + W-8BEN on file before scaling past
  the smoke test.)*
- **SGD/USD FX gains/losses** on the cash leg are a separate line from strategy P&L
  and may have their own tax treatment; track conversions for records.
- The system's churn dropped ~60% (10,657 → 4,262 trades over the backtest) after
  the 2026-05 refactor, which is favorable for after-tax returns, but realized
  short-term gains remain the dominant tax characteristic of a momentum-tilted book.

---

## 11. Amendment log & appendices

### Amendment log

| Date | Change | Rationale |
|---|---|---|
| 2026-06-11 | Initial adoption | Phase-1 prerequisite. Capital: no fixed cap, drawdown-gated, $5K smoke test first. Retire at −25% deployed. Monthly review. |
| 2026-06-30 | Currency reconciliation (§ 5, § 8, § 10) | Smoke test funded as 5,000 SGD (wired 2026-06-16), not USD; added funding-currency/FX section (NAV measured in USD, SGD/USD FX is unhedged and outside § 7); W-8BEN / SGD-residency tax notes; refreshed monitoring note now that the divergence + gateway-watchdog launchd jobs are deployed. |
| 2026-08-06 | Implementation review adopted; § 6 enforcement caveat added | 6-agent read-only review found several § 6 limits declared but not wired on the live path (stop-loss, passive-scan, circuit-breaker liquidation; drawdown measured on budget not equity). **No limits changed** — this is a factual correction of the "enforced in code" claim. Tracked in **T1 (#2)** / **T2 (#3)**. Full review: [implementation-review-2026-08-06.md](implementation-review-2026-08-06.md). |
| 2026-08-17 | **Capital-scaling ladder supersedes § 5 in full** (KAN-37, direction D9/D14/D16) | Two conflicting deployment paths cannot both govern real money. The ladder's written rungs + clean-epoch criteria replace § 5's illustrative steps; the 8-gate re-clear is retained at Rung 0 entry and after a Rung-0 disarm, the capacity review at Rung 3. Records that the **divergence monitor is a capital-decision input by rule** — resolving its own doc's "not a kill switch" wording, which stays true of automated action and is now false of capital authority. Restates the two standing constraints the ladder does not override: the **30% household satellite ceiling by dollar** (§ 3) and **W-8BEN / tax closure before any scale-up** (§ 10). Prerequisite for Rung 0 entry. Per-sleeve demotion rules: [sleeve-kill-criteria.md](sleeve-kill-criteria.md). |

| 2026-08-17 | **Rung-0 allocation overlay: one sleeve** (§ 4) (KAN-34, direction D8) | The measured Rung-0 run showed the six-way split does not trade at this capital rather than merely trading expensively: positions size to $34–119, `quality_value` filled **0 of 7,395** entry signals and `tail_risk_hedge` 4 of 5,956, and round-trip commission drag on what did fill was 236–632 bps against $1,766 of gross P&L. Rung 0 therefore runs **`momentum` alone at 100%** ($444/position, 87.7% of the universe fillable, ~45 bps round-trip); the other five are **suspended, not retired**, and return at higher rungs by amendment. **No weight of record changed** — this is a rung-scoped overlay. Risk-posture consequences accepted and recorded in § 4: no standing crash hedge, and the crash entry freeze becomes a total trading freeze (exits unaffected). Conditional on `momentum`'s D10 edge verdict of record; fallback `sector_rotation`. Decision: [rung0-economics.md § 9](rung0-economics.md). |

### Appendix A — revival conditions for dropped sleeves

`mean_reversion` and `short_term_mr` were dropped 2026-05-26. Their signal functions
are preserved in `scripts/run_backtest.py`. Re-enabling either requires the
pre-committed macro conditions in
[mean-reversion-failure-analysis.md](../strategies/mean-reversion-failure-analysis.md)
to hold, and counts as a weight change under § 9 (monthly-review-only, documented).

### Appendix B — key backtest baseline (for divergence reference)

~10.1-year backtest (2016-05-31 → 2026-07-06), $100K basis. **Corrected
2026-07-10**: earlier figures (+420–427%) were produced with the per-sleeve
total-exposure limit silently disabled (internal leverage); Sharpe is
unchanged by the correction — the difference was leverage, not skill.

| Metric | Value |
|---|---:|
| Total return | +385.9% |
| CAGR | ~17.0% |
| Sharpe | 1.97 |
| Max drawdown | 11.60% |
| Win rate | 53.5% |
| Trades | 3,748 |

Full detail: [portfolio-2026-05.md](../strategies/portfolio-2026-05.md).
