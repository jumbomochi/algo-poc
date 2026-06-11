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

1. **Drawdown protection** — backtested max DD 10.85% vs ~30% for CW8 over the same
   horizon.
2. **Skill-building & optionality** — operating a live systematic book is a durable
   capability; the infrastructure has option value beyond this strategy.
3. **Return** — secondary. The headline edge is ~+7 pp CAGR vs CW8, but only
   **~+2.78 pp is durable skill alpha**; the remaining ~+4 pp is a US-vs-international
   concentration premium that is regime-dependent and may fade or invert. The IPS
   treats return as a *bonus on top of* drawdown protection, not the thesis.

The operator commits to not rationalizing the strategy upward into "core" status
on the strength of a good run. Promotion past satellite scale is governed by § 5,
not by enthusiasm.

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

## 5. Capital deployment — drawdown-gated, no fixed cap

Deployment is **gated by realized risk behavior, not by a fixed dollar ceiling.**
The operator has chosen *not* to set an absolute hard cap: the system may scale
indefinitely **so long as the drawdown limits in § 6 and § 7 continue to be
satisfied in live trading.** Drawdown discipline is the cap.

**Deployment path:**

1. **Smoke test — $5,000, ~2 months.** First real-money deployment. Purpose is to
   validate execution, reconciliation, slippage, and the daily ops loop on live
   fills — *not* to prove return. Success = the operational gates behave, not a P&L
   target.
2. **Scale up** in steps thereafter (e.g. $25K → $50K → $100K → beyond), each step
   contingent on:
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

---

## 6. Risk limits (authoritative source: `config/default.yaml` § `risk`)

These are enforced in code. The IPS records them so the operator cannot quietly
loosen them; **risk limits are never loosened during a drawdown** (§ 9).

| Limit | Value | Config key |
|---|---:|---|
| Position entry limit | 5% of NAV | `position_entry_limit_pct` |
| Sector concentration | 20% of NAV | `sector_concentration_pct` |
| Trailing stop-loss | 15% | `stop_loss_trailing_pct` |
| Drawdown — pause new buys | 10% | `drawdown_pause_pct` |
| Drawdown — circuit breaker (liquidate all) | 20% | `drawdown_circuit_breaker_pct` |
| Position soft ceiling (notify) | 7% | `soft_ceiling_pct` |
| Position hard ceiling (auto-trim to soft) | 15% | `hard_ceiling_pct` |
| Margin warning / critical | 70% / 85% | `margin_warning_pct` / `margin_critical_pct` |
| Correlation alert | 0.70 | `correlation_alert_threshold` |

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
  the paper/live signal job. Two-axis OK/WARNING/BREACH classification; exits non-zero
  on breach for alerting. *(Pending: launchd plist for the daily run — see handoff
  open items.)*
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
- **Deployment / scale-up (§ 5):** operator decision at a monthly review only.
- **All amendments to this IPS** are dated and appended to § 11 with a one-line
  rationale. The git history of this file is the amendment log.

---

## 10. Tax & accounting notes

- Trades are US-listed equities/ETFs executed via Interactive Brokers.
- Tax residency and the resulting treatment (withholding, capital-gains reporting,
  wash-sale handling) are the operator's responsibility and **not** automated by the
  system. *(Open: confirm residency-specific reporting requirements before scaling
  past the smoke test.)*
- The system's churn dropped ~60% (10,657 → 4,262 trades over the backtest) after
  the 2026-05 refactor, which is favorable for after-tax returns, but realized
  short-term gains remain the dominant tax characteristic of a momentum-tilted book.

---

## 11. Amendment log & appendices

### Amendment log

| Date | Change | Rationale |
|---|---|---|
| 2026-06-11 | Initial adoption | Phase-1 prerequisite. Capital: no fixed cap, drawdown-gated, $5K smoke test first. Retire at −25% deployed. Monthly review. |

### Appendix A — revival conditions for dropped sleeves

`mean_reversion` and `short_term_mr` were dropped 2026-05-26. Their signal functions
are preserved in `scripts/run_backtest.py`. Re-enabling either requires the
pre-committed macro conditions in
[mean-reversion-failure-analysis.md](../strategies/mean-reversion-failure-analysis.md)
to hold, and counts as a weight change under § 9 (monthly-review-only, documented).

### Appendix B — key backtest baseline (for divergence reference)

9.97-year backtest (2016-05-31 → 2026-05-22), $100K basis:

| Metric | Value |
|---|---:|
| Total return | +420.4% |
| CAGR | 17.98% |
| Sharpe | 1.97 |
| Max drawdown | 10.85% |
| Win rate | 53.82% |
| Trades | 4,262 |

Full detail: [portfolio-2026-05.md](../strategies/portfolio-2026-05.md).
