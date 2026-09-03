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
- **Depends on / blocked by:** Nothing. D12 orderRef stamping has shipped — `services/execution/ib_executor.py` sets `order.orderRef` on both submission paths and reads it back via `find_order_by_ref`/`restore_order_by_ref`.

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

## Benchmark-relative per-sleeve kill criteria
- **What:** Add a trigger of the form "alpha vs the sleeve's own universe below X over N months" to `docs/operations/sleeve-kill-criteria.md`, which today has no benchmark-relative trigger of any kind.
- **Why:** The 2026-08-28 CEO review added a live passive comparator to the daily digest (E4). Without a kill trigger reading it, that column is a dashboard nobody acts on — the same shape as the Prometheus gauges nobody scrapes.
- **Pros:** Closes the loop from measurement to action; matches the D3.3 ladder (`live -> paper -> shadow`, one step, never retirement); makes "underperformed its own universe" a rule rather than an opinion.
- **Cons:** Alpha is noisy over short windows — the 2016-2026 estimates had t-statistics of 0.32 to 1.25 over a full decade, so a monthly threshold would demote on noise. Needs a calibrated window before a threshold can be chosen honestly.
- **Context:** Deferred by /plan-ceo-review 2026-08-28 (E5, cherry-pick ceremony). The review found the six sleeves run at beta 0.114-0.550 with alpha indistinguishable from zero against their own universes. Start once E4 has shipped and accumulated enough live sessions to calibrate a threshold against real tracking error. Full record: `~/.gstack/projects/jumbomochi-algo-poc/ceo-plans/2026-08-28-benchmark-relative-edge.md`.
- **Effort:** S (human ~1 day / CC ~45min). **Priority:** P2.
- **Depends on / blocked by:** E4 (benchmark column in `scripts/ops/evidence_digest.py`), which is itself gated on verifying the digest job actually runs (KAN-64 AC7).

## `eligible_tickers=[]` means opposite things in sibling signal factories
- **What:** Normalise the universe-scoping fallback across all five signal factories in `scripts/run_backtest.py` so an empty list consistently means "trade nothing", never "trade everything".
- **Why:** The four ranking factories use `eligible = set(eligible_tickers or bars_by_ticker)` (`:881` and siblings), so `eligible_tickers=[]` falls back to the whole `bars_by_ticker` union — the exact unscoped state that let `sector_rotation` hold HUM and LLY. `make_earnings_drift_signals_fn` (`:1562`) uses `frozenset(...) if eligible_tickers is not None else None`, which treats `[]` as "nothing". Same parameter name, opposite behaviour, and the dangerous direction is the silent one.
- **Pros:** Removes a footgun at its root rather than documenting it; one consistent rule for every future sleeve.
- **Cons:** Touches the signal path of four sleeves, so it is a behaviour change to the backtest and wants its own diff and its own full suite run. Nothing passes an empty list today, so it is latent, not live.
- **Context:** Raised by the /ship pre-landing review on 2026-09-01 during the sleeve-scoping fix, and deliberately deferred to keep that PR narrow. Guarded meanwhile by `tests/scripts/test_run_paper_sleeve_scoping.py::test_a_sleeve_still_trades_its_own_universe`, which fails if any sleeve ends up scoped to nothing.
- **Depends on / blocked by:** Nothing.

## `test_pipeline_report` fails inside the UTC/SGT date-boundary window
- **What:** Make `tests/deploy/test_pipeline_report.py::test_the_message_reports_the_documented_facts_in_order` date-boundary safe.
- **Why:** The test seeds a fill at `datetime.now(timezone.utc)` (`:458`) and then asserts `"fills:1" in msg`, but the report counts fills by local (SGT) day. During the hours where the UTC date and the SGT date differ, the seeded fill lands outside the report's window and the assertion fails. Observed failing on 2026-08-31 and passing again on 2026-09-01 with no code change in between, which is what identified it as a boundary flake rather than a break.
- **Pros:** Removes a test that fails for ~8 hours a day on this host's timezone; a suite that is red on the clock trains people to ignore red.
- **Cons:** Needs the report's own day-window semantics pinned down first — the fix is either seeding in local time or freezing the clock, and picking wrong just moves the flake.
- **Context:** Found by /ship on 2026-09-01 while landing the sleeve-scoping fix; verified pre-existing by stashing the branch changes and reproducing on `develop`. Not caused by that work and left untouched (`REPO_MODE=collaborative`).
- **Depends on / blocked by:** Nothing.

## AVB and EQR need conId overrides (Error 200 on a live gateway)
- **What:** Probe IB Gateway read-only for the conIds of AVB (AvalonBay) and EQR (Equity Residential), and add them to `CONTRACT_CONID_OVERRIDES` in `shared/universe.py` if the gateway serves bars by conId.
- **Why:** Both returned `Error 200: No security definition has been found ... Stock(symbol='AVB', exchange='SMART', currency='USD')` on the 2026-08-19 baseline run, so both have **zero** bars across all 2,511 sessions — 5,022 membership-days, 44% of the still-listed exclusion. Both are still S&P members at snapshot end and are large, liquid REITs, so this is a stale gateway contract view, not a delisting. It is the same shape as the existing MMC→MRSH and FI→FISV overrides.
- **Pros:** Recovers 5,022 membership-days in every future baseline; removes two names that silently vanish from any universe that includes them.
- **Cons:** Needs a live read-only IB connection to look the conIds up (the D18 memo set the precedent: clientId 77, no orders). Does **not** unblock D18 — coverage moves 11.28% → 10.88%, still above the 5% floor. The override is a workaround for a gateway data problem, and `shared/universe.py:433` already says to re-verify and delete entries if the gateway's contract view is ever refreshed.
- **Context:** Found by the T11 investigation on 2026-09-03 from `~/ibc/logs/backtest_refresh_20260819.log`. The other 16 still-listed exclusions are a different cause — `Error 162: HMDS query returned no data` on the deepest year-chunks, i.e. IB not serving that history depth for a contract that resolved fine (PEP resolved with `conId=11017`). That is not repairable in code, and live is unaffected because it fetches `years=1`.
- **Depends on / blocked by:** An authorised read-only IB probe.

## Trial registry counts sleeves, not parameterizations
- **What:** `n_trials = 8` counts the eight candidate sleeves that reached a backtest, not the lookbacks, thresholds and top-N values tried inside each one. Make the registry count the real search.
- **Why:** The deflation under-corrects in a known direction: `SR*` is computed from a search smaller than the one actually run, so every DSR is too generous.
- **Pros:** Makes the bar honest rather than known-optimistic; removes one of the four caveats currently attached to every D10 verdict.
- **Cons:** Strictly raises `SR*`, so it makes already-failing sleeves fail harder and yields no new information today (all six already fail at the current, more generous bar). Reconstructing the historical parameter search may not be possible from git history at all — which would make the new count a guess, and a guessed deflation is worse than a documented under-correction.
- **Context:** Deferred by /plan-ceo-review 2026-08-28 (E6). Already recorded as a known limit in `docs/operations/incumbent-edge-evaluation.md` ("Eight counts sleeves, not parameterizations"); this entry exists so the deferral rationale is discoverable alongside it. Becomes relevant only if a sleeve ever approaches passing, or when a new strategy is registered and its parameter search can be counted prospectively.
- **Effort:** M (human ~3 days / CC ~2h). **Priority:** P3.
- **Depends on / blocked by:** Nothing. Best done prospectively on the next new strategy rather than retroactively on the incumbents.
