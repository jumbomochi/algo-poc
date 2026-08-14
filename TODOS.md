# TODOS

Deferred work with context. Added by /plan-eng-review and /plan-ceo-review 2026-08-11.

## Regime-filter research thread
- **What:** Evaluate regime gating (trend/chop/vol state filters) for existing sleeves, run through the promotion pipeline (research → shadow → paper → live).
- **Why:** 2026 discourse treats regime filters as table stakes for systematic strategies; sleeves currently trade regime-blind. `config/default.yaml`'s ml_model section already carries regime-detection flags nothing fully exploits.
- **Pros:** Cheap research surface (config bones exist); potential drawdown reduction in regime shifts.
- **Cons:** Third research thread for a solo operator; violates the direction doc's two-thread WIP limit today.
- **Context:** Deferred by /plan-ceo-review D3.5 (2026-08-11). Enters when a research WIP slot frees (after sentiment 2026-11 eval or the D17 ML decision resolves). Must pass the promotion pipeline's pre-registered gates like any other thread.
- **Depends on / blocked by:** Research WIP slot; promotion pipeline (direction doc) defines its gates.

## Per-service Redis ACLs + money-stream integrity
- **What:** Scoped Redis users per service; authenticity (HMAC field) on `stream:approved_orders` and `stream:kill`.
- **Why:** T3 locked the bus behind ONE shared password — any service (or anything holding the credential) can publish approved orders or kills. Finding 5.2 ("no inter-service message authenticity", `docs/operations/implementation-review-2026-08-06.md`) is still open; T3 explicitly deferred it.
- **Pros:** Closes the last open T3 finding; blast-radius containment if any one service is compromised.
- **Cons:** ~1 day human / ~1h CC; touches every service's Redis client; thin threat model while localhost-only single-operator.
- **Context:** T3's report flagged it as a follow-up thread candidate (`.superpowers/sdd/PARALLELIZATION/task-T3-report.md:317`). Start: `redis ACL SETUSER` per service in the compose entrypoint + an HMAC field on money-stream messages keyed from `.env`.
- **Depends on / blocked by:** Nothing. Revisit before live money scales.

## Mid-session completed-order reconciliation
- **What:** On execution reconnect, fetch IB executions that completed during the outage and project them immediately, instead of waiting for the next daily reconciliation.
- **Why:** T7 left this as "a larger follow-up" — an order that fills during an execution restart is invisible until the daily backstop (logged, not silent, but hours stale).
- **Pros:** Closes the stale-book window after any daytime restart; makes the halt reconcile sweep's broker-truth view complete.
- **Cons:** ~1-2 days human / ~1-2h CC; replay logic must respect the T4 idempotent-fills path.
- **Context:** The eng review's D12 decision (stamp `orderRef=recommendation_id` on every IB submission) makes this deterministic: `reqExecutions` + orderRef → intent mapping. Start: `services/execution/runner.py` `setup()` after `restore_broker_tracking`.
- **Depends on / blocked by:** D12 (orderRef stamping) landing in tranche 1.

## Dependabot lockfile auto-regeneration
- **What:** CI workflow that recompiles `requirements.lock` inside Dependabot PRs so they pass the lockfile-freshness gate automatically.
- **Why:** 18 Dependabot PRs are red on the same job (the macOS-vs-ubuntu recompile T9 predicted) — persistent red CI trains the operator to ignore the security gate.
- **Pros:** Every future dependency bump self-heals; the gate stays credible.
- **Cons:** ~half day human / ~30min CC; bot-pushes-to-bot-PR permissions are fiddly (needs a PAT or workflow_run pattern).
- **Context:** Tranche 3 item 9 of the readiness plan fixes the BASE lockfile on linux; this is the follow-on for future bumps. Start: `workflow_run` job triggered by Dependabot PRs running `pip-compile` and pushing the regenerated lock.
- **Depends on / blocked by:** Tranche 3 item 9 (linux lockfile recompile) first.

## Sub-1-share allocations are silently dropped (high-priced tickers)
- **What:** `_effective_quantity` (`services/execution/ib_executor.py:142-162`) truncates with `float(int(quantity))`, so any allocation worth less than one share is rejected as `SUBMISSION_FAILED` and never traded. Rounding, not truncation, is the candidate fix — or sizing that refuses to emit sub-1-share intents.
- **Why:** Observed 2026-08-14: `quality_value` sized LLY at 0.7633 shares (**$922.83** at $1209) and the order was dropped entirely. The sleeve had ~$13.5k of unused headroom, so this is a *rounding* limitation, not a budget one. Two dust intents the same day (SCHW 0.0003 = $0.03, ARKG 0.0001 = $0.00) are harmless noise, but they share the root cause and pollute the intent ledger.
- **Pros:** Recovers real intended exposure on any ticker priced above a sleeve's per-signal allocation; LLY, and anything else in the four-figure club, is currently untradeable by the smaller sleeves.
- **Cons:** Naive round-half-up overspends every sleeve by up to half a share price, and also turns 1.9 into 2 (today it becomes 1) — so it changes sizing everywhere, not just the dropped cases. Needs tests + an execution service redeploy. `fractional_orders: false` is not the lever: IBKR paper rejects fractional API orders (Error 10243).
- **Context:** Decided 2026-08-14 to leave as-is rather than force a discretionary 1-share ($1,209 vs the $922.83 the model asked for, +31%). LLY exposure is not actually absent — `sector_rotation` holds 2 shares. Revisit as a sizing question (should a sleeve emit an intent it cannot fill?) rather than an execution rounding tweak.
- **Depends on / blocked by:** Nothing.
