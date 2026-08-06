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

Traceability for every numbered finding → work thread → tracking issue / draft PR → status. **Status as of adoption (2026-08-06): all Open (tracked).** Update the Status column as threads land.

| ID | Finding | Sev | Thread | Issue | PR | Status |
|---|---|---|---|:--:|:--:|---|
| 1.1 | Kill switch fails OPEN on restart | High | T1 | #2 | #12 | Open |
| 1.2 | 20% circuit breaker never liquidates | High | T1 | #2 | #12 | Open |
| 1.3 | Kill liquidation not idempotent (can short) | High | T1 | #2 | #12 | Open |
| 1.4 | Kill uses stale in-memory positions | Med | T1 | #2 | #12 | Open |
| 1.5 | Kill during IB disconnect aborts, no alert | Med | T1 | #2 | #12 | Open |
| 2.1 | No independent/intraday stop-loss (dead) | High | T2 | #3 | #13 | Open |
| 2.2 | No hard-ceiling / margin auto-trim (dead) | Med-High | T2 | #3 | #13 | Open |
| 2.3 | Drawdown measured on budget, not equity | Med | T2 | #3 | #13 | Open |
| 2.4 | Reprice / partial-fill loop dead | Med | T7 | #8 | #18 | Open |
| 3.1 | Risk `process_fill` double-counts on replay | High | T4 | #5 | #15 | Open |
| 3.2 | `process_fill` mixes commission currency | Med | T4 | #5 | #15 | Open |
| 3.3 | Poison messages silently parked (no DLQ) | Med | T4 | #5 | #15 | Open |
| 3.4 | `:dlq` unmonitored; notifications DLQ no-ack | Med | T4 | #5 | #15 | Open |
| 3.5 | ml/signal no `drain_pending`; ack-on-buffer | Med | T4 | #5 | #15 | Open |
| 4.1 | Survivorship / winner-preselected universe | High | T5 | #6 | #16 | Open |
| 4.2 | Same-bar entry fill (look-ahead) | High | T5 | #6 | #16 | Open |
| 4.3 | Same-bar exit fill at day's open | High | T5 | #6 | #16 | Open |
| 4.4 | Fundamentals look-ahead (also live path) | High | T5 | #6 | #16 | Open |
| 4.5 | ML filter in-sample; no purge/embargo | Med | T5 | #6 | #16 | Open |
| 4.6 | Divergence baseline optimistic | Med | T5 | #6 | #16 | Open |
| 4.7 | Cost model understated; pop-std Sharpe | Low-Med | T5 | #6 | #16 | Open |
| 5.1 | Redis/PG open in committed compose | High | T3 | #4 | #14 | Open |
| 5.2 | No inter-service message authenticity | High | T3 | #4 | #14 | Open |
| 5.3 | IB Gateway API exposure | High | T9 | #10 | #20 | Open |
| 5.4 | `joblib.load` untrusted deserialization | Med-High | T9 | #10 | #20 | Open |
| 5.5 | Live-mode guard drift (raw env var) | Med | T9 | #10 | #20 | Open |
| 5.6 | `.env` perms; no dependency lockfile | Med-Low | T9 | #10 | #20 | Open |
| 6.1 | No app-level healthchecks | High | T6 | #7 | #17 | Open |
| 6.2 | Metrics not wired; no alert rules | High | T6 | #7 | #17 | Open |
| 6.3 | Unbounded Redis streams (OOM risk) | High | T6 | #7 | #17 | Open |
| 6.4 | No message `schema_version` | Med | T9 | #10 | #20 | Open |
| 7.0 | Two parallel implementations + model-loader mismatch | Med | T8 | #9 | #19 | Open |

**Priority rollup:** P0 = T1 (#2/#12), T2 (#3/#13), T3 (#4/#14) · P1 = T4 (#5/#15), T5 (#6/#16), T6 (#7/#17) · P2 = T7 (#8/#18), T8 (#9/#19), T9 (#10/#20).

> The full-detail version of this review — including security exploitation paths and the local IB Gateway settings — is intentionally kept out of this public repo. It lives locally, gitignored, at `output/implementation-review-2026-08-06.FULL-PRIVATE.md`.
