# Implementation Review — algo-poc

**Status:** Final — adopted 2026-08-06
**Owner:** Huiliang Lui (operator)
**Tracking:** work threads **T1–T9** = issues #2–#10, draft PRs #12–#20; this doc = PR #11. Finding-level traceability in § 12.
**Reviewer:** 6-agent read-only review (execution, risk/capital, alpha pipeline, backtest/research, infra/reliability, security). No code changed.
**Scope:** ~19.7K LOC production across 8 services + backtest/research/sentiment stacks, cross-checked against the [IPS](investment-policy-statement.md) and live topology.
**Companion:** this doc is the source-of-truth backlog; each *work thread* in § 9 maps to one branch/PR.

---

## 1. Executive assessment

This is genuinely strong engineering for a solo live-money system: a ~1:1 test-to-code ratio, a transactional-outbox order path with DB-enforced idempotency, fail-closed reconciliation, a pre-committed IPS with retirement triggers, and an incident-hardened ops layer. The operator has already caught and corrected a leverage bug that inflated backtests (+427% → +386%).

The review found **one dangerous, recurring pattern**: *safety controls that are written and tested but never wired into the code path that actually runs live.* The IPS § 6 states nine risk limits are "enforced in code"; several are not enforced on the live path. The mechanisms designed to save capital in a crisis — kill switch, stop-loss, circuit-breaker liquidation — are the ones with the most gaps. The root cause is **two parallel implementations** (`run_paper.py` inline logic vs. the microservice runners) with unclear ownership, which let these gaps hide: three independent reviewers could not determine which path was authoritative.

At the current $3.7K smoke-test scale the dollar risk is small — but validating exactly these mechanisms **is the stated purpose of the smoke test**, so closing them is on the critical path to any scale-up decision.

## 2. Confirmed live topology

- `scripts/run_paper.py` runs daily via launchd, computes the 6 sleeves, and **publishes to `stream:recommendations`** (`run_paper.py:881`). It also runs its own reconciliation and per-sleeve trailing-stop exits inline (`run_paper.py:133-224`).
- The Docker services **`risk_management`, `execution`, `portfolio_accounting`** run `restart: unless-stopped` and are the load-bearing consumers that gate risk, place IB orders, and project fills.
- The Docker **`signal_generation` / `ml_model`** services appear **dormant**, superseded by `run_paper.py`. There are also two model systems (multiclass `.joblib` microservice vs. binary LightGBM `.txt` script path) with a loader mismatch.

> **Open item for the operator to confirm:** whether the Docker stack runs continuously or only around the daily window. This changes the Redis-OOM timeline and whether the dormant services are ever loaded.

---

## 3. Theme 1 — The emergency stop is fragile exactly when it's needed 🔴

| # | Finding | Location | Failure scenario |
|---|---|---|---|
| 1.1 | Kill switch is **not latching/persisted → fails OPEN on restart** | `kill_switch.py:18`; `risk/runner.py:176-211,849-899` | Kill fires → liquidates → ACKs. A later deploy/OOM/crash restarts the service with `_active=False`; it silently trades the next recommendation while the operator believes it is halted. |
| 1.2 | **20% circuit breaker never liquidates** — result only rejects a buy | `engine.py:280-289` consumed at `risk/runner.py:358-378` | Book draws down 25%; nothing sells, buys merely pause. |
| 1.3 | Kill liquidation is **not idempotent → can flip short** | `execution/runner.py:596-601` (replay path `:130-144`) | Kill submits exits, process crashes before fills project; restart replays the kill and re-sells the same shares. |
| 1.4 | Kill acts on **stale in-memory single-account positions**, never reloads broker truth | `risk/runner.py:867` | A narrowed/empty in-memory book leaves real open positions un-liquidated. |
| 1.5 | Kill while **IB disconnected aborts on first ticker; critical alert never fires** | `execution/runner.py:588-620` | Emergency flatten fails silently; operator gets no notification. |

## 4. Theme 2 — IPS-mandated risk limits not enforced on the live path 🔴

| # | Finding | Location | Note |
|---|---|---|---|
| 2.1 | **No independent/intraday stop-loss** — `run_stop_loss_check` has zero callers | `risk/runner.py:931-975` | `stop_loss_trailing_pct` (15%) inert; only live stop is the once-daily EOD sleeve exit. |
| 2.2 | **No hard-ceiling auto-trim / margin-critical trim** — `run_passive_scan` never runs | `risk/runner.py:901-929` | `passive_scan_interval_minutes` referenced only in config/tests. |
| 2.3 | **Drawdown measured on `deployable_capital`** (pinned at USD cap), not book equity | `risk/runner.py:674,684-693`; `capital.py:74-82` | Peak ≈ nav ≈ cap ⇒ drawdown reads ~0%; the 10% pause / 20% breaker are inert in the capped regime. |
| 2.4 | **Reprice loop / partial-fill review are dead code** | `order_manager.py:235-319,321-366` | `reprice_interval_minutes`, `max_reprice_attempts`, `min_viable_fill_pct` inert; unfilled limits die at IB session expiry. |

**Governance:** reconcile the IPS § 6 claims with what the code actually enforces — wire the code to match, or amend the IPS to state reality.

## 5. Theme 3 — At-least-once streams meet non-idempotent in-memory state 🟠

| # | Finding | Location |
|---|---|---|
| 3.1 | Risk **`process_fill` double-counts on replay** (DB already reflects the fill, then replay re-applies it) | `risk/runner.py:219-262` (replayed via `drain_pending :189-195`) |
| 3.2 | `process_fill` **mixes native `fill.commission` into USD** cash/NAV | `risk/runner.py:247,255` (should use `commission_trading`) |
| 3.3 | **Steady-state poison messages silently parked** in the PEL — log-only, no DLQ, no alert | `risk/runner.py:1038`, `execution/runner.py:664`, `ml_model/runner.py:160`, `signal_generation/runner.py:272` |
| 3.4 | **Nothing monitors any `:dlq` stream**; notifications' DLQ path **never acks** (PEL leak, duplicate alerts) | `redis_client.py:118-126`; `notifications/runner.py:84-86` |
| 3.5 | ml_model / signal_generation have **no `drain_pending`**; ml_model **acks on buffering** (loses in-flight + buffered signals on restart) | `ml_model/runner.py:136,159`; `signal_generation/runner.py:261,271` |

## 6. Theme 4 — The backtest that justifies the strategy is optimistic by construction 🟠

| # | Finding | Location | Effect |
|---|---|---|---|
| 4.1 | **Survivorship / winner pre-selection** — `SP500_TOP50` is a static list "as of early 2025" over a 10yr backtest | `universe.py:13-21` | Inflates every metric; no point-in-time membership, no delisted names. |
| 4.2 | **Same-bar entry fill** — signal on today's close, filled at today's low/support | `backtest/runner.py:98-101`; `simulator.py:31` | Textbook fake alpha; also contaminates ML labels. |
| 4.3 | **Same-bar exit fill at the day's open** — exit decided on close, filled at that day's open | `simulator.py:45-68`; `backtest/runner.py:107-117` | Understates every stop-out ⇒ **reported ~11.6% max DD is partly artifact**. |
| 4.4 | **Fundamentals look-ahead that also hits LIVE decisions** — `report_date` = period-end, no filing lag | `fetch_fundamentals.py:82-88,118-119`; live call at `run_paper.py:652` | Knows figures weeks early, in paper trading too. |
| 4.5 | **ML filter can be applied in-sample**; walk-forward has no holding-period purge | `train_signal_model.py:117-141,37-114`; `feature_extractor.py:106` | Removes trades it already knows lost. |
| 4.6 | **Divergence monitor baselines against this optimistic backtest** | `backtest/divergence.py:228-302` | Live trails "by construction"; loosening thresholds masks real drift. |
| 4.7 | Cost model understates cost for the tiny live account (no per-order commission floor); population-std Sharpe | `run_backtest.py:1948-1951`; `metrics.py:78-89` | Directionally optimistic. |

> The newer `research/` framework does this **correctly** (purged+embargoed CV, DSR, BH-FDR, point-in-time panel). The fix is to route the backtest through that discipline — the machinery already exists in-repo.

## 7. Theme 5 — Security: the trust boundary is the network, and defaults are open 🟠

> Detailed exploitation paths and the local IB Gateway settings are intentionally kept out of this public doc (see the operator's private security note). This section states *what* to harden and *where*, not *how* to exploit it.

| # | Area | Location | Priority |
|---|---|---|---|
| 5.1 | **Redis + Postgres are unauthenticated and bound to all interfaces in the committed compose** — only a gitignored override hardens them, so a fresh clone/redeploy starts exposed | `docker-compose.yml:8-10,21-22,139-140`; `redis_client.py` | High |
| 5.2 | **No authenticity on inter-service messages** — any writer to a stream is fully trusted; add per-service Redis ACLs (publish-only vs read-only) and integrity on the money streams | `schemas/messages.py`; `execution/runner.py` | High |
| 5.3 | **IB Gateway API exposure** — verify the API listener is loopback/firewalled to the Docker bridge, and review the blind-trading / auto-accept-connection settings before live | `docker-compose.yml:52-59,116-130` (Gateway settings live outside the repo) | High |
| 5.4 | **Untrusted deserialization** — `joblib.load` on a DB-controlled model path with no integrity check; record and verify a content hash/signature before load | `ml_model/registry.py:77` | Med-High |
| 5.5 | **Live-mode guard drift** — the kill/auth live check reads a raw env var instead of the validated `AppConfig.mode`; align it so it can't be bypassed at the live cutover | `api/auth.py:23-61` | Med |
| 5.6 | `.env` file permissions; no dependency lockfile / vuln scanning | `.env`; `pyproject.toml` | Med / Low |

> **Strengths preserved:** route-level authz is consistent (`kill` requires admin role), git secret hygiene is clean, all SQL is parameterized, no shell injection.

## 8. Theme 6 — A hung service is invisible for up to 24 hours 🟠

| # | Finding | Location |
|---|---|---|
| 6.1 | **No app-level healthchecks** (only pg/redis); `restart: unless-stopped` misses deadlock-but-alive (the known stuck-modal class) | `docker-compose.yml` |
| 6.2 | **Metrics not wired** — `setup_metrics`/`start_http_server` have zero callers; Prometheus scrapes dead ports; no alert rules | `observability.py:21,31`; `config/prometheus.yml` |
| 6.3 | **Unbounded Redis streams** — no `MAXLEN`/`XTRIM`/`maxmemory` → eventual OOM kills the whole bus silently | `redis_client.py:31,126`; redis service |
| 6.4 | **No message `schema_version`** — a partial deploy adding a required field drops in-flight messages to silent validation errors | `schemas/messages.py` |

> **Strengths preserved:** gateway watchdog (two-strike, auth-refusal), daily positive Telegram heartbeat (dead-man's switch), verified `pg_dump` backups, alembic preflight, TTY-only/paper-only destructive guards.

---

## 9. Work threads (parallel backlog)

Each thread is independently workable and maps to one branch/PR. Priority: **P0** before the next live session, **P1** before scaling past the smoke test, **P2** hygiene/correctness.

| ID | Thread | Pri | Covers | Primary files |
|---|---|---|---|---|
| **T1** | Kill-switch & circuit-breaker state machine | P0 | 1.1–1.5, 2.x breaker→liquidate | `kill_switch.py`, `risk/runner.py`, `execution/runner.py` |
| **T2** | Runtime risk enforcement (periodic driver) | P0 | 2.1–2.3 | `risk/runner.py`, `engine.py`, `passive_monitor.py` |
| **T3** | Message-bus lockdown | P0 | 5.1 | `docker-compose.yml`, `redis_client.py`, new `override.example` |
| **T4** | Idempotent fills & stream hygiene | P1 | 3.1–3.5 | `risk/runner.py`, `redis_client.py`, all consumer runners |
| **T5** | Backtest realism & divergence rebaseline | P1 | 4.1–4.7 | `backtest/*`, `universe.py`, `fetch_fundamentals.py`, `run_paper.py` |
| **T6** | Observability & healthchecks | P1 | 6.1–6.3 | `observability.py`, `docker-compose.yml`, `config/prometheus.yml` |
| **T7** | Execution order-lifecycle robustness | P2 | reconnect callbacks, reprice loop, exit-order tracking, U* assertion | `execution/ib_executor.py`, `order_manager.py`, `execution/runner.py` |
| **T8** | Consolidate dual implementations + model loader | P2 | Theme 7, loader mismatch | `run_paper.py`, `services/{signal_generation,ml_model}`, `registry.py` |
| **T9** | Security & supply-chain hardening | P2 | 5.2–5.6, 6.4 | `api/auth.py`, `registry.py`, `schemas/messages.py`, `pyproject.toml`, `.env` handling |

Dependencies: T1 and T7 both touch `execution/runner.py` (sequence or coordinate). T2's drawdown-on-equity change should land before relying on T1's breaker→liquidate.

## 10. What could not be verified (read-only)

- Whether the Docker stack runs continuously vs. only around the daily window.
- Which model loader the live path uses; the true IB Gateway bind scope (needs `lsof -i :7497`).
- Whether any *reported* backtest figure used the in-sample `--ml-filter`.
- IB corporate-action (split/dividend) adjustment; CVE scan of resolved dependency versions.

## 11. What's genuinely strong (do not regress)

- The DB order/fill path: deterministic `recommendation_id` + transactional outbox + unique constraints + `FillProjector` idempotency (praised independently by 3 reviewers).
- Fail-closed funding gate (`funding.py`); dual-currency USD-vs-USD discipline (`capital.py`).
- The `research/` factor framework's methodology (purged/embargoed CV, DSR, BH-FDR, point-in-time panel).
- The incident-hardened ops layer (watchdog, heartbeat, backups, preflight).

---

## 12. Findings register

Traceability for every numbered finding → work thread → evidence → status.
**Status verified 2026-10-01 against `origin/main` `a3503fb` (KAN-41).** At
adoption (2026-08-06) every row read "Open".

How the work actually landed, since the Issue/PR columns alone would mislead:
T2/T3/T5/T6/T9 merged as PRs #13/#14/#16/#17/#20. **T1, T4 and T7 did not** —
PRs #12/#15/#18 were closed unmerged because that work landed through the
Session A integration merge `de7a761` (2026-08-09); cite the per-commit SHAs
below. **T8's PR #19 was closed unmerged** and its branch never landed; 7.0 was
resolved by KAN-35 instead. Later readiness stories (KAN-4…KAN-92) reworked
much of the same code; where they matter, the KAN key is cited.

Status vocabulary: **Fixed** (code + test), **Partial** (the named residual is
listed below the table), **Superseded** (replaced by a different mechanism),
**Deferred** (accepted, with rationale and where it is tracked), **Open**
(genuinely not done — carries an owner). Test paths are relative to `tests/`.

| ID | Finding | Sev | Thread | Issue | PR | Status | Evidence |
|---|---|---|---|:--:|:--:|---|---|
| 1.1 | Kill switch fails OPEN on restart | High | T1 | #2 | #12 | **Fixed** | `2521251`, `7b1d576`, `8894142`; `kill_switch.py` `reload_from_store`/`sync_from_store`; admin-only clear `DELETE /api/v1/kill` (`40c3f3d`); execution halt gate KAN-12. `services/risk_management/test_kill_switch.py::TestKillSwitchPersistence::test_reload_from_store_stays_halted_after_restart` |
| 1.2 | 20% circuit breaker never liquidates | High | T1 | #2 | #12 | **Fixed** | `40c3f3d`; risk `_emit_drawdown_gauge` → `_liquidate_all(event_type="circuit_breaker_liquidation")`. `services/risk_management/test_runner.py::TestCircuitBreakerLiquidation::test_breaker_liquidates_and_halts` |
| 1.3 | Kill liquidation not idempotent (can short) | High | T1 | #2 | #12 | **Fixed** | `6af0219` (deterministic ids via ledger); KAN-4, KAN-6, KAN-9; oversell guard KAN-10. `services/risk_management/test_runner.py::TestKillLiquidation::test_replayed_kill_does_not_double_submit` |
| 1.4 | Kill uses stale in-memory positions | Med | T1 | #2 | #12 | **Fixed** | `6af0219`, KAN-6; risk `_authoritative_open_positions` reads the DB. `services/risk_management/test_runner.py::TestKillLiquidation::test_kill_liquidates_authoritative_db_positions` |
| 1.5 | Kill during IB disconnect aborts, no alert | Med | T1 | #2 | #12 | **Fixed** | `6af0219`, `1d0d042`; per-ticker try/except + unconditional critical alert. `services/execution/test_runner.py::TestKillHandling::test_kill_per_ticker_failure_continues_and_alerts` |
| 2.1 | No independent/intraday stop-loss (dead) | High | T2 | #3 | #13 | **Partial** (R1) | `9716eb0` periodic driver `run_periodic_risk_checks`, ledger-routed by KAN-7. `services/risk_management/test_runner.py::TestPeriodicRiskEnforcement::test_periodic_checks_drive_stop_loss` |
| 2.2 | No hard-ceiling / margin auto-trim (dead) | Med-High | T2 | #3 | #13 | **Partial** (R2) | Hard ceiling: `9716eb0` `run_passive_scan`, KAN-7. `services/risk_management/test_runner.py::TestPeriodicRiskEnforcement::test_hard_ceiling_breach_auto_trims_to_soft`. Margin half deferred |
| 2.3 | Drawdown measured on budget, not equity | Med | T2 | #3 | #13 | **Fixed** | `9716eb0` `_book_equity_from_db`; USD equity columns KAN-44. `services/risk_management/test_stop_loss.py::TestDrawdownOnBookEquity::test_book_equity_engages_circuit_breaker` |
| 2.4 | Reprice / partial-fill loop dead | Med | T7 | #8 | #18 | **Partial** (R3) | `847ad47` sweep driven from `run()`; `5f09ec6` sits out outside RTH. `services/execution/test_runner.py::TestUnfilledSweepDriver::test_maybe_run_unfilled_sweep_respects_interval`. Reprice-with-quotes deferred |
| 3.1 | Risk `process_fill` double-counts on replay | High | T4 | #5 | #15 | **Fixed** | `16efde4` dedup by `execution_id`. `services/risk_management/test_runner.py::TestFillProcessing::test_replayed_fill_does_not_move_book` |
| 3.2 | `process_fill` mixes commission currency | Med | T4 | #5 | #15 | **Fixed** | `16efde4`, `1d0d042` (uses `commission_trading`). `services/risk_management/test_runner.py::TestFillProcessing::test_process_fill_uses_commission_trading_usd` |
| 3.3 | Poison messages silently parked (no DLQ) | Med | T4 | #5 | #15 | **Fixed** | `fb0281a`, `1d0d042`, `f1446de`. `services/risk_management/test_runner.py::TestSteadyStatePoisonHandling::test_poison_message_is_dead_lettered_acked_and_alerted`; `services/execution/test_runner.py::TestExecutionPoisonHandling::test_poison_message_dead_lettered_acked_alerted` |
| 3.4 | `:dlq` unmonitored; notifications DLQ no-ack | Med | T4 | #5 | #15 | **Partial** (R4) | `736c1a0` notifications ack-after-send + risk `_check_dlq_depths` → `dlq_backlog` alert. `services/notifications/test_runner.py::TestNotificationsServiceRunner::test_dlq_path_acks_after_send`; `services/risk_management/test_runner.py::TestDlqDepthMonitor::test_alerts_when_dlq_has_backlog` |
| 3.5 | ml/signal no `drain_pending`; ack-on-buffer | Med | T4 | #5 | #15 | **Fixed**, then **Superseded** | `f1446de`. `services/ml_model/test_runner.py::TestMLConsumerLoop::test_incomplete_signal_is_not_acked`. Both services left compose in KAN-35 (`12635e1`) |
| 4.1 | Survivorship / winner-preselected universe | High | T5 | #6 | #16 | **Partial — bias accepted** (R5) | Code: `8666ca3`, `2635674` `MembershipCalendar`; coverage floor KAN-22. `shared/test_universe.py::TestMembershipCalendar::test_contains_is_point_in_time`. Re-run BLOCKED (KAN-52); bias accepted D18 (KAN-59), code path KAN-68 |
| 4.2 | Same-bar entry fill (look-ahead) | High | T5 | #6 | #16 | **Fixed** | `8666ca3`. `backtest/test_runner_next_bar_fills.py::TestNextBarEntryFill::test_entry_decided_on_close_fills_at_the_next_open` |
| 4.3 | Same-bar exit fill at day's open | High | T5 | #6 | #16 | **Fixed** | `8666ca3`. `backtest/test_simulator.py::TestMarketExit::test_market_exit_fills_at_open` |
| 4.4 | Fundamentals look-ahead (also live path) | High | T5 | #6 | #16 | **Fixed** | `8ff56a1` `build_fundamentals_lookup`, shared by `run_paper.py`. `backtest/test_fundamentals_cache.py::TestFilingLag::test_lag_defaults_to_the_10q_filing_deadline` |
| 4.5 | ML filter in-sample; no purge/embargo | Med | T5 | #6 | #16 | **Fixed** | `8ff56a1` `purged_train_mask`, `assert_ml_filter_out_of_sample`. `backtest/test_signal_model_training.py::TestOutOfSampleGuard::test_rejects_a_backtest_that_overlaps_the_training_window` |
| 4.6 | Divergence baseline optimistic | Med | T5 | #6 | #16 | **Fixed** | `b809ff0` `is_like_for_like`; KAN-23, KAN-51 (pin), KAN-60 (Rung-0 pin, D21). `backtest/test_divergence.py::TestExecutionModel::test_same_bar_backtest_is_not_comparable`. Pinned baselines inherit 4.1's accepted bias |
| 4.7 | Cost model understated; pop-std Sharpe | Low-Med | T5 | #6 | #16 | **Fixed** | `8666ca3` commission floor + tiered slippage, `ddof=1`. `backtest/test_metrics.py::TestSharpeRatio::test_sharpe_uses_sample_stdev`; `backtest/test_costs.py::TestCommissionFloor::test_small_order_pays_the_per_order_minimum` |
| 5.1 | Redis/PG open in committed compose | High | T3 | #4 | #14 | **Fixed** | `c7ba601`. `deploy/test_message_bus_lockdown.py::test_postgres_redis_and_api_ports_are_loopback_bound`, `::test_redis_requires_auth_via_requirepass` |
| 5.2 | No inter-service message authenticity | High | T3 | #4 | #14 | **Deferred** | T3 deferred it explicitly; one shared Redis credential, no per-service ACL or money-stream integrity check. Tracked in `TODOS.md` ("Per-service Redis ACLs…"). Revisit before live money scales |
| 5.3 | IB Gateway API exposure | High | T9 | #10 | #20 | **Open** (R6) | Host configuration, not in the repo (§10). KAN-11 (`627c345`) pins the exact account, which narrows wrong-account risk but not API exposure |
| 5.4 | `joblib.load` untrusted deserialization | Med-High | T9 | #10 | #20 | **Fixed** | `250a9df`, `304a09e` (content hash on the DB row). `services/ml_model/test_model_roundtrip.py::test_a_tampered_native_file_is_refused` |
| 5.5 | Live-mode guard drift (raw env var) | Med | T9 | #10 | #20 | **Fixed** | `250a9df`; `api/auth.py` `resolve_mode()` reads `AppConfig.mode`. `services/api/test_auth.py` |
| 5.6 | `.env` perms; no dependency lockfile | Med-Low | T9 | #10 | #20 | **Fixed** | Lockfile `250a9df`; deterministic check + CI KAN-36; guardrail KAN-57; pip-audit (`security.yml`), KAN-91. Secrets moved to the login keychain KAN-16 (`0eee862`); `.env` is not read by any job |
| 6.1 | No app-level healthchecks | High | T6 | #7 | #17 | **Fixed** | `afeb89e`, `4c63979` heartbeat-file healthchecks; `gateway_watchdog.sh` alerts on an unhealthy stack (KAN-66). `deploy/test_observability_healthchecks.py::test_every_worker_service_has_a_heartbeat_healthcheck`. Detection, not auto-restart (Docker does not restart `unhealthy`) |
| 6.2 | Metrics not wired; no alert rules | High | T6 | #7 | #17 | **Partial** (R7) | In code: `afeb89e` `setup_metrics()` in every service; `config/alert_rules.yml`; Alertmanager KAN-14; thresholds KAN-15. `deploy/test_observability_healthchecks.py::test_every_backend_service_calls_setup_metrics`. **Not deployed** |
| 6.3 | Unbounded Redis streams (OOM risk) | High | T6 | #7 | #17 | **Fixed** | `afeb89e`, `4c63979` `XADD MAXLEN ~ 25000` + `maxmemory 512mb noeviction`. `deploy/test_observability_healthchecks.py::test_redis_service_sets_a_maxmemory_ceiling_with_noeviction`. Its memory alert shares R7 |
| 6.4 | No message `schema_version` | Med | T9 | #10 | #20 | **Fixed** | `250a9df`; additive-only rule in `shared/schemas/messages.py`. `shared/test_schemas.py::TestSchemaVersion::test_future_schema_version_is_rejected` |
| 7.0 | Two parallel implementations + model-loader mismatch | Med | T8 | #9 | #19 | **Superseded** | Dual path: KAN-35 (`12635e1`, D17, `docs/decisions/ml-path-2026-09.md`) — `run_paper.py` is the only recommendation source; the services remain as offline tools. Loader: `af6ae6d`. `services/ml_model/test_model_roundtrip.py::test_a_native_lightgbm_file_round_trips_through_the_registry` |

**Tally (32):** 22 Fixed · 1 Fixed-then-Superseded · 1 Superseded · 6 Partial · 1
Deferred · 1 Open.

### Residuals — what is still not done, and who owns it

Every Partial and Open row above resolves to one of these. The owner is the
operator throughout (single-operator system); "where tracked" is where the next
reader should look.

| Ref | Residual | Disposition | Where tracked |
|---|---|---|---|
| R1 | Stop-loss marks are daily closes, so a breach is acted on after the next close. Broker GTC stops (KAN-19/KAN-20) are built but `broker_stops_enabled: false` until epoch v2 | Deferred | `paging-and-accepted-deferrals.md` §2.1; KAN-33 (held) |
| R2 | Margin utilisation hard-coded `0.0`; the margin-critical trim cannot fire | Deferred | `paging-and-accepted-deferrals.md` §2.2 |
| R3 | Sweep ages out, does not reprice; calendar set by private attribute. `OrderManager.handle_partial_fill` and `execution.min_viable_fill_pct` still have no caller | Deferred (reprice, calendar) · **Open** (dead partial-fill surface — wire or delete on the next execution-runner touch) | `paging-and-accepted-deferrals.md` §2.3–2.4 |
| R4 | Live DLQ-depth alert covers only risk's three input DLQs. `stream:approved_orders:dlq`, `stream:alerts:dlq` and execution's kill DLQ surface only in the weekly evidence digest (KAN-29) and a Prometheus rule with no evaluator | **Open** | `dlq-audit-2026-08.md` (KAN-21) |
| R5 | IB serves no delisted history, so ~11% of membership-days stay excluded and every baseline is `BLOCKED`; accepted as a documented, time-bounded bias. Re-evidence no earlier than 2029-08-18 from forward capture (KAN-58) | Deferred (decision D18, re-accepted D20/D21) | `backtest-baseline.md`; `docs/designs/project-direction.md` D18 |
| R6 | IB Gateway API bind scope / trusted-IP settings never verified from the repo | **Open** — host check required before live capital | §10 of this review; `go-live-checklist.md` |
| R7 | The observability overlay (Prometheus, Alertmanager, Grafana, redis-exporter) is not running, so no rule in `config/alert_rules.yml` is evaluated | **Open** — operator decision whether to deploy it | `paging-and-accepted-deferrals.md` §1 |

**Priority rollup (historical):** P0 = T1 (#2/#12), T2 (#3/#13), T3 (#4/#14) · P1 = T4 (#5/#15), T5 (#6/#16), T6 (#7/#17) · P2 = T7 (#8/#18), T8 (#9/#19), T9 (#10/#20).

> The full-detail version of this review — including security exploitation paths and the local IB Gateway settings — is intentionally kept out of this public repo. It lives locally, gitignored, at `output/implementation-review-2026-08-06.FULL-PRIVATE.md`.
