# Readiness Closure & Evidence Machinery — Story Backlog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan story-by-story. Acceptance criteria use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close every gap between "merged" and "actually protects the book" (readiness design tranches 1–3), then land the evidence machinery (state store, digests, epoch/ladder engine, edge framework) that makes the go-live gate a mechanical read — so epoch v2 can start and Rung 0 can arm on rules, not judgment calls.

**Architecture:** The money path is `scripts/run_paper.py` → Redis Streams → dockerized `risk_management`/`execution` services against IB Gateway (paper DUN551088). All risk-side exits route through ONE shared ledgered emitter (extracted from the working kill path); execution gains a direction-aware durable-halt gate plus broker-native GTC stops as the primary protection layer. Evidence (divergence verdicts, epochs, drills) becomes durable Postgres observations behind Alembic, read by digests and the gate evaluator through one shared query helper.

**Tech Stack:** Python 3.12, SQLAlchemy + Alembic, Redis Streams, ib_insync, pytest (`asyncio_mode = "auto"`), Prometheus/Grafana/Alertmanager, Telegram bot, launchd + Docker Compose.

**Sources (authoritative — read alongside each story):**
- `docs/designs/project-direction.md` (CEO plan of record; decisions D3–D17, 1A/2A/3A)
- `~/.gstack/projects/jumbomochi-algo-poc/huiliang-main-design-20260811-145715.md` (APPROVED readiness design; tasks T1–T15, test matrix #1–#30, lifecycle diagrams)

Test numbers like **#21** below refer to the readiness design's Test Matrix; **3A#N** refers to the direction doc's evidence-machinery test set.

## Global Constraints

- **Bar:** "safe to run real money unattended" — coded-but-unenforceable counts as broken, not done.
- **Destructive-actions policy (CLAUDE.md):** agents NEVER run DB deletes/truncates, DLQ drains, `launchctl` operations, volume removals, or paper-state resets — stories mark those steps **[OPERATOR]** and the agent's deliverable is the exact command + verification, handed to the human.
- **Ledger vocabulary is the real enum** (`shared/models/order_ledger.py:23`, transitions at `shared/order_ledger.py:31-54`): terminal-without-fill = {`RISK_REJECTED`, `SUBMISSION_FAILED`, `CANCELLED`, `EXPIRED`}; halt rejection = `SUBMISSION_FAILED` with `reason="halted"`; `APPROVED → CANCELLED` is ILLEGAL — never use it.
- **Exit-intent id scheme (D11):** `{kind}-{account}-{portfolio}-{conid}-{trading_date}-{seq}`; `seq` increments only when the prior intent for that scope/date is terminal.
- **Secrets:** no token literal in any committed or gitignored-but-required YAML; Alertmanager pinned ≥ v0.26 for `bot_token_file`.
- **Baseline coverage floor (D14):** delisted-name exclusions ≤ 5% of point-in-time membership-days, else baseline state = `BLOCKED` (never silently degraded).
- **Evidence store (D15):** stores only immutable observations, never derived truth; blindness is DERIVED (missing row on a NYSE trading day via `shared/market_calendar.py`); ONE shared query helper computes streaks/pauses for both digest and `go_live_gate.py`.
- **Epoch rules (D11/D12/D13):** 30-session divergence window; only BREACH counts (not WARNING); verdicts score only once the window lies fully inside the current rung; ≥15 round-trips AND ≥60% exposure sessions or the epoch EXTENDS; money-path/manifest changes restart the epoch.
- **Code conventions:** every module `from __future__ import annotations`; tests pytest with `asyncio_mode = "auto"`; each story is TDD — every acceptance-criteria checkbox is implemented test-first (write the named test, watch it fail, implement, watch it pass, commit); Beck two-commit (structural, then behavioral) where a story names it.
- **Worktree lanes (tranche 1):** A = Stories 1–7 (risk + `shared/`), B = 8–10 (execution), C = 11–12 (config-only), D = 13 (deploy). A owns `shared/`; B reads the same ledger APIs — land Story 1's ledger-semantics commit before B starts, or share the branch point.

## File Structure (new files this plan creates)

| File | Responsibility |
|---|---|
| `tests/services/risk_management/test_exit_emitter.py` | Test home for the shared exit emitter (Stories 2–7) |
| `config/alertmanager.yml` | Committed, secret-free Alertmanager route to Telegram |
| `shared/models/evidence.py` | ORM: `DivergenceDaily`, `GateEpoch`, `GateEpochEvent`, `DrillOutcome` |
| `alembic/versions/<rev>_evidence_state_store.py` | Migration for the four evidence tables |
| `shared/evidence_store.py` | The ONE query helper: streaks, blindness, pause-clock, rung state |
| `scripts/ops/evidence_digest.py` | Weekly Telegram evidence digest (reads store only) |
| `deploy/launchd/local.algo-evidence-digest.plist` + `run_evidence_digest.sh` | Weekly digest job |
| `scripts/ops/record_epoch.py` | Epoch manifest recording + rung-transition CLI |
| `backtest/membership.py` (or extend existing) | Coverage metric + 5% floor + BLOCKED state |
| `research/edge_framework/` (`deflated_sharpe.py`, `holdout.py`, `stability.py`) | D10 edge-validation framework |
| `docs/operations/sleeve-kill-criteria.md` | Written kill criteria for the six incumbents |
| `docs/operations/rung0-economics.md` | D8 investigation results + restructure decision |
| `tests/shared/test_evidence_store.py`, `tests/scripts/test_evidence_digest.py` | Evidence-machinery test set (3A) |

Dependency graph (story numbers):

```
Phase 1 (parallel lanes)                Phase 2                         Phase 3 / parallel with epoch v2
A: 1 → 2 → 4 → {5, 6}; {4,8} → 7        19 → 20 ─┬→ 24, 31              32 (ML decision)
       3 ──┘                            21 ──────┤                      33 (CI)
B: 8;  1 → 9 → 10                       22 → 23 ─┼→ 25 → 26            34 (kill criteria + amendments)
C: 11 → 12                              27, 28    │                     35 → 36 → 37 (edge framework)
D: 13                                   29 ←──────┘                     38 (docs hygiene, last)
Stops: 15 → 16 → 17                     30 = epoch v2 start (16,17,20,21,25,29)
1–10,13 → 14 (deploy) → 18 (DLQ drain)
```

---

## Phase 1 — Tranche 1: money-path safety

### Story 1: Exit intents created at APPROVED; kill-path illegal-transition fix; outbox isolation

**Size:** 2 SP | **Lane:** A | **Depends on:** — | **Source:** design T1 (D10, CONFIRMED latent P0)

**Files:**
- Modify: `services/risk_management/runner.py` (kill path `:1080-1159` — intent creation site)
- Modify: `shared/order_ledger.py` call-sites only (no transition-table change)
- Test: `tests/services/risk_management/test_runner.py`, `tests/scripts/test_run_paper_gate.py`

**Interfaces:**
- Consumes: `OrderLedger.create_intent`, `ALLOWED_TRANSITIONS` (`shared/order_ledger.py:31-54`), `OrderStatus` enum.
- Produces: the invariant every later story relies on — **risk-side exit intents are created and transitioned to `APPROVED` in the same transaction** (risk IS the approver). Kill/breaker exits are published at `APPROVED`, making execution's `record_submission` `APPROVED → SUBMITTED` transition legal.

Today kill exits publish at `PROPOSED`; execution's `record_submission` (`shared/order_ledger.py:248`) does `transition(SUBMITTED)` — illegal from `PROPOSED` — so the IB order goes live and THEN the ledger raises `InvalidOrderTransition` → DLQ with a live unattributed broker order. APPROVED-at-creation also keeps risk exits out of `run_paper.py:810`'s PROPOSED-outbox replay (the two outboxes currently collide).

**Acceptance criteria:**
- [ ] **#21** kill exit intent created at `APPROVED` → execution submits → `record_submission` transitions `APPROVED→SUBMITTED` legally: no `InvalidOrderTransition`, no `stream:approved_orders:dlq` entry, fill attributed to the intent.
- [ ] **#22** risk-created exit intents (status `APPROVED`) are NOT selected by `run_paper.py:810`'s PROPOSED-outbox replay — no cross-publication into `stream:recommendations`.
- [ ] Existing breaker/kill tests (`tests/services/risk_management/test_runner.py:582-631`) stay green.
- [ ] Verify: `pytest tests/services/risk_management/ tests/scripts/test_run_paper_gate.py tests/shared/test_order_ledger.py -v`

### Story 2: Structural extraction of `_emit_ledgered_exit` (zero behavior change)

**Size:** 1 SP | **Lane:** A | **Depends on:** Story 1 | **Source:** design T2 commit 1 (eng-review 4A)

**Files:**
- Modify: `services/risk_management/runner.py` (extract from kill path `:1080-1159`)
- Test: `tests/services/risk_management/test_runner.py`, new `tests/services/risk_management/test_exit_emitter.py`

**Interfaces:**
- Produces: `RiskServiceRunner._emit_ledgered_exit(kind: str, target: dict, exit_id: str, reason: str) -> None` — the SINGLE publish site for ALL risk-side exits (kill/breaker now; stop-loss/trim in Story 4). `target` is one `load_liquidation_targets` row (ticker/quantity/con_id/account_id/exchange/currency/portfolio).

This is the pure-structural Beck commit: kill/breaker behavior must be byte-identical afterward.

**Acceptance criteria:**
- [ ] **#1** existing breaker tests green unchanged, plus one new assertion that `_emit_ledgered_exit` is the only code path publishing to `stream:approved_orders` from the risk runner (e.g. all emit sites resolve to the one method).
- [ ] Missing-`con_id` handling from the kill path (`runner.py:1094-1113`, `liquidation_unroutable` critical alert, no doomed publish) lives inside the extracted method.
- [ ] Verify: `pytest tests/services/risk_management/ -v` — commit 1 lands with NO new behavior.

### Story 3: Identity-scoped liquidation targets + D11 exit-id helper

**Size:** 2 SP | **Lane:** A | **Depends on:** — | **Source:** design T4 (D11, CONFIRMED first-row-wins bug)

**Files:**
- Modify: `shared/liquidation.py` (`load_liquidation_targets:29-54`, id helpers)
- Test: new `tests/shared/test_liquidation.py`

**Interfaces:**
- Consumes: `Position` ORM (`shared/models/portfolio.py`).
- Produces: `load_liquidation_targets(session, *, account_id=None) -> list[dict]` now returns one row per **{account_id, portfolio, con_id}** (today it aggregates by ticker and keeps the FIRST row's account/portfolio/con_id — one sleeve/account can absorb or suppress another's exit). New helper `exit_intent_id(kind: str, account_id: str, portfolio: str, con_id: int, trading_date: date, seq: int) -> str` returning `{kind}-{account}-{portfolio}-{conid}-{trading_date}-{seq}`. `liquidation_exit_id` (epoch-based) stays for kill events — kills fire once, exits recur; do not conflate the two schemes.

**Acceptance criteria:**
- [ ] **#27** two portfolios holding the same ticker → `load_liquidation_targets` returns per-{account, portfolio, con_id} rows; each gets its own exit id; one scope's nonterminal exit cannot suppress the other's.
- [ ] Existing kill-path callers (`services/risk_management/runner.py`) updated for the new row granularity; kill/breaker tests green.
- [ ] Verify: `pytest tests/shared/test_liquidation.py tests/services/risk_management/ -v`

### Story 4: Stop-loss and passive-trim exits through the shared emitter

**Size:** 2 SP | **Lane:** A | **Depends on:** Stories 2, 3 | **Source:** design T2 commit 2 (fixes P0 #1: stop-loss sells never reach IB)

**Files:**
- Modify: `services/risk_management/runner.py` — replace the synthetic-id paths at `:1224` (`passive-trim-{uuid4}`) and `:1270` (`stop-loss-{uuid4}`) with `_emit_ledgered_exit` calls
- Test: `tests/services/risk_management/test_exit_emitter.py`

**Interfaces:**
- Consumes: `_emit_ledgered_exit` (Story 2), `exit_intent_id` (Story 3), APPROVED-at-creation (Story 1).
- Produces: every 30-min-scan exit carries full broker identity (account/portfolio/con_id/exchange/currency), a deterministic D11 id, and a ledger intent persisted before publish. Suppression rule: skip emission when ANY nonterminal sell intent for the {account, portfolio, con_id} scope exists in the ledger (exit-emitter or otherwise) — two open sells for one position is never correct.

Carry the design's exit-intent lifecycle diagram (design lines 83–111) into an inline comment at the emitter (eng-review 3A).

**Acceptance criteria:**
- [ ] **#2** E2E: stop-loss breach → ledger intent → execution consumes → mocked IB order placed; no `OrderIntentNotFound`, no DLQ entry.
- [ ] **#4** persistent breach across scans → exactly one nonterminal intent at a time.
- [ ] **#7** missing `con_id` on stop-loss/trim → `liquidation_unroutable`-style critical alert, no doomed publish (parity with kill path).
- [ ] **#8** suppression scope: ANY nonterminal sell intent for the scope (including a non-exit sell) suppresses emission.
- [ ] `ConflictingOrderIntent` on create ⇒ adopt the existing intent (idempotent re-entry).
- [ ] Verify: `pytest tests/services/risk_management/test_exit_emitter.py tests/services/risk_management/ -v`

### Story 5: Orphan-proof publication — `mark_published` + re-publish scan

**Size:** 1 SP | **Lane:** A | **Depends on:** Story 4 | **Source:** design T3 (eng-review 1A)

**Files:**
- Modify: `services/risk_management/runner.py` (30-min scan)
- Test: `tests/services/risk_management/test_exit_emitter.py`

**Interfaces:**
- Consumes: `OrderLedger.mark_published` (`shared/order_ledger.py:364`) and `run_paper.py`'s outbox-replay pattern.
- Produces: emitter flow `create_intent → publish → mark_published`. Each 30-min scan RE-PUBLISHES any unpublished nonterminal exit intent (same deterministic id — downstream replay is a no-op) instead of suppressing on it; suppression applies only to *published* in-flight intents. Without this, a crash between commit and publish leaves an orphan intent that permanently mutes the ticker's stop-loss.

**Acceptance criteria:**
- [ ] **#3** kill the emitter between persist and publish → next scan re-publishes the same id; downstream treats the replay as a no-op.
- [ ] Alert if the same intent needs re-publishing on repeated scans (replay-repeats visibility, per Failure Modes table).
- [ ] Verify: `pytest tests/services/risk_management/test_exit_emitter.py -v`

### Story 6: Re-fire and re-size semantics

**Size:** 2 SP | **Lane:** A | **Depends on:** Story 4 | **Source:** design T2 commit 2 (re-fire trigger + partial-fill rules)

**Files:**
- Modify: `services/risk_management/runner.py` (emitter + scan)
- Test: `tests/services/risk_management/test_exit_emitter.py`

**Interfaces:**
- Produces: re-fire emits a fresh intent ONLY when the prior attempt is terminal-without-full-fill ({`RISK_REJECTED`, `SUBMISSION_FAILED`, `CANCELLED`, `EXPIRED`}), on the next 30-min scan — never while one is in flight. On a partially-filled prior sell, the new exit is re-sized to the remaining position (never stacked). `seq` increments only when the prior intent for that scope/date is terminal.

**Acceptance criteria:**
- [ ] **#5** partial fill on prior exit → re-fire re-sized to remaining quantity.
- [ ] **#6** seq rollover: same scope breaches on consecutive trading dates → distinct ids, no collision with the prior terminal intent.
- [ ] A breach that persists across scans yields exactly one nonterminal intent at a time; re-fire occurs only after terminal-without-full-fill (success criterion 2, asserted end-to-end).
- [ ] Verify: `pytest tests/services/risk_management/test_exit_emitter.py -v`

### Story 7: Oversell guard

**Size:** 1 SP | **Lane:** A/B seam | **Depends on:** Stories 4, 8 | **Source:** design T5 (D12)

**Files:**
- Modify: `services/execution/order_manager.py`, `services/execution/ib_executor.py`, `services/risk_management/runner.py` (emitter)
- Test: `tests/services/execution/test_runner.py`

**Interfaces:**
- Produces: before an emergency sell is submitted, open BUY orders for the same `con_id` are cancelled, then the sell quantity is capped to `min(ledger position, broker-reported position)`. Position projection is async — a working BUY can otherwise turn an exit into a short.

**Acceptance criteria:**
- [ ] **#26** working BUY on the con_id + emergency sell → BUY cancelled first, sell capped to broker-reported quantity, never short.
- [ ] Verify: `pytest tests/services/execution/test_runner.py -v -k oversell`

### Story 8: orderRef stamping + exact IB account pin

**Size:** 1 SP | **Lane:** B | **Depends on:** — | **Source:** design T5 (D12) + T4 (D11)

**Files:**
- Modify: `services/execution/ib_executor.py`, `services/execution/order_manager.py`, `config/default.yaml` (configured account id)
- Test: `tests/services/execution/test_runner.py` (or executor test module)

**Interfaces:**
- Produces: (a) every IB submission stamped `orderRef=recommendation_id` and `account=<configured account>` — the reconcile sweep (Story 10) discovers raced orders account-wide from broker `openTrades` by orderRef, because in the crash window `ib_order_id` hasn't reached the ledger; (b) executor asserts `managedAccounts` contains exactly the configured account id (DUN551088), not just the `DU`/`U` prefix.

**Acceptance criteria:**
- [ ] **#25 (stamp half)** every submission carries `orderRef=recommendation_id`; verified against paper `openTrades` in the drill.
- [ ] **#28** executor refuses a Gateway session whose `managedAccounts` lacks the configured account id, even when the prefix matches.
- [ ] Verify: `pytest tests/services/execution/ -v -k "orderref or account_pin"`

### Story 9: Direction-aware halt gate in execution

**Size:** 2 SP | **Lane:** B | **Depends on:** Story 1 (ledger semantics) | **Source:** design T7 (D9, fixes P0 #2)

**Files:**
- Modify: `services/execution/runner.py` (inject `HaltStateRepository`; check before submission AND in `setup()` PEL replay `:122-142`)
- Test: `tests/services/execution/test_runner.py`

**Interfaces:**
- Consumes: `HaltStateRepository.load_active_halt(mode=...)` (`shared/halt_state.py:24`), injected via constructor like `order_ledger`.
- Produces: the gate blocks **exposure-INCREASING orders only**. Ledger-backed risk-reducing sells (stop-loss / trim / liquidation) MUST pass during a halt — a halt is precisely when risk publishes liquidation sells. Halted buys are durably rejected (`SUBMISSION_FAILED`, `reason="halted"`) and acked — not DLQ'd, not retained. **Halt-lookup failure is its own state:** DB read fails → do NOT ack, retain for retry with backoff, page "unable to determine halt state". Carry the design's halt-enforcement diagram (design lines 115–145) into an inline comment at the check.

**Acceptance criteria:**
- [ ] **#9** halted buy is durably rejected AND acked — not in `:dlq`, not redelivered via PEL.
- [ ] **#10** restart with halted PEL entries → all buys rejected, none submitted.
- [ ] **#12** halt cleared → previously-rejected orders are NOT resurrected.
- [ ] **#23** during a halt, a ledgered risk-reducing sell submits; a buy is rejected `SUBMISSION_FAILED reason="halted"` and acked.
- [ ] **#24** DB error on the halt read → message retained (no ack, no DLQ), retried with backoff, "unable to determine halt state" alert emitted.
- [ ] Verify: `pytest tests/services/execution/test_runner.py -v -k halt`

### Story 10: Post-halt reconcile sweep on its own timer

**Size:** 2 SP | **Lane:** B | **Depends on:** Stories 8, 9 | **Source:** design T7 (D9/D12)

**Files:**
- Modify: `services/execution/runner.py`
- Test: `tests/services/execution/test_runner.py`

**Interfaces:**
- Produces: on halt activation (and periodically while halted), the sweep maps live broker orders to intents (ledgered `ib_order_id`, falling back to account-wide `openTrades` orderRef discovery) and cancels any exposure-increasing order submitted inside the check-to-submit race window — EXEMPTING ledgered risk-reducing sells. The sweep runs on its **own unconditional timer** — it must NOT piggyback on the unfilled-order sweep, which silently no-ops without `_market_calendar` (`runner.py:911`).

**Acceptance criteria:**
- [ ] **#11** an order that slipped past the check inside the race window is cancelled by the sweep.
- [ ] **#13** sweep timer fires with `_market_calendar = None`.
- [ ] **#25 (sweep half)** sweep discovers and cancels a raced order visible only by orderRef.
- [ ] Sweep does not cancel liquidation/stop sells (direction-aware exemption, part of #23).
- [ ] Verify: `pytest tests/services/execution/test_runner.py -v -k sweep`

### Story 11: Alertmanager + observability stack, honestly up

**Size:** 2 SP | **Lane:** C | **Depends on:** — | **Source:** design T8 (2A, D13; fixes P0 #3)

**Files:**
- Create: `config/alertmanager.yml` (secret-free, committed)
- Modify: `docker-compose.observability.yml` (Alertmanager service pinned ≥ v0.26, redis-exporter `REDIS_PASSWORD`, `restart: unless-stopped` on ALL observability services)
- Test: CI step running `amtool check-config config/alertmanager.yml`

**Interfaces:**
- Produces: Prometheus-originated alerts (HeartbeatStale et al.) route via a minimal Alertmanager `telegram_configs` to the same bot/chat — deliberately NOT through the notifications service (HeartbeatStale exists to catch that service wedged). Token via `bot_token_file: /etc/alertmanager/secrets/telegram_token`, rendered from `${TELEGRAM_BOT_TOKEN}` in `.env` at container start. App-originated alerts keep the verified notifications→Telegram path.

**Acceptance criteria:**
- [ ] **#14** `amtool check-config` on the committed `alertmanager.yml` passes in CI.
- [ ] No token literal in any committed or gitignored-but-required YAML (grep the diff).
- [ ] redis-exporter authenticates post-T3 (`REDIS_PASSWORD` wired); scrape targets verified up.
- [ ] All observability services carry `restart: unless-stopped`.
- [ ] Verify: `docker compose -f docker-compose.yml -f docker-compose.observability.yml config` renders; `amtool check-config config/alertmanager.yml`.

### Story 12: Alert-threshold retune + external dead-man + synthetic delivery test

**Size:** 1 SP | **Lane:** C | **Depends on:** Story 11 | **Source:** design T8 (D13; `alert_rules.yml:9-15` self-declares not pager-ready)

**Files:**
- Modify: `config/alert_rules.yml`, `deploy/launchd/run_paper.sh` (dead-man ping)
- Test: drill checklist (not pytest) + `scripts/ops/send_test_alert.py`

**Interfaces:**
- Produces: rules retuned/business-hours-gated for a once-daily system BEFORE routing to Telegram (or the pager cries wolf until muted); one external dead-man check on the 04:15 run (e.g. healthchecks.io ping from `run_paper.sh` — nothing internal can report the whole host down).

**Acceptance criteria:**
- [ ] Every rule in `alert_rules.yml` reviewed against the 04:15-daily cadence; the file's "not pager-ready" warning comment removed only when true.
- [ ] `run_paper.sh` pings the dead-man URL on successful completion; missing ping by deadline pages externally.
- [ ] **criterion 4 [OPERATOR-ASSISTED DRILL]** HeartbeatStale fires on a synthetic notifications-service wedge and the Telegram page arrives via the Alertmanager route — end-to-end, not assumed.
- [ ] Synthetic app-layer alert → Telegram message received (`send_test_alert.py`).

### Story 13: Land launchd deploy hardening

**Size:** 1 SP | **Lane:** D | **Depends on:** — | **Source:** design T9 (working tree already contains the draft — land it with 3 fixes)

**Files:**
- Modify: `deploy/launchd/run_paper.sh` (`mkdir -p "$LOG_DIR"`), `deploy/launchd/deploy.sh` (write-free dry-run), `deploy/launchd/README.md` (kill the manual-`cp` teaching), `deploy/launchd/run_divergence.sh` (exit 3 = BLIND alert), `.gitignore` (`.codegraph/`)
- Test: `tests/deploy/test_launchd_deploy_hardening.py` (exists uncommitted)

**Acceptance criteria:**
- [ ] **#16 [IRON-RULE REGRESSION]** `deploy.sh --dry-run` on a fresh HOME leaves the filesystem untouched (the current draft creates dirs before the dry-run branch).
- [ ] **#17** `run_paper.sh` creates `LOG_DIR` before first write (behavioral test, not grep).
- [ ] Committed `run_divergence.sh` classifies exit 3 as the BLIND alert (retires the "UNEXPECTED exit code 3" misclassification).
- [ ] `.codegraph/` ignored; README teaches `deploy.sh`, not manual `cp`.
- [ ] **#18 [OPERATOR]** fresh-host deploy drill: `deploy.sh` with no `~/ibc` and no `~/Library/LaunchAgents` succeeds; operator runs `deploy/launchd/deploy.sh` + the printed `launchctl` reloads.
- [ ] Verify: `pytest tests/deploy/ -v`

### Story 14: Container deploy + cold-reboot verification

**Size:** 1 SP | **Lane:** D (after merge of lanes A–D) | **Depends on:** Stories 1–10, 13 | **Source:** design T9 / D13 ("without this, tranche 1's code never runs")

**Files:**
- Create: deploy-step section in `deploy/launchd/README.md` (or `docs/operations/`) — rebuild/force-recreate/verify/rollback runbook

**Acceptance criteria:**
- [ ] `docker compose build && docker compose up -d --force-recreate` for risk_management/execution documented and executed (the 2026-08-07 stale-image lesson: `--build` alone leaves containers on the old image).
- [ ] Running image hash verified to match the build; rollback path (previous image tag) noted.
- [ ] **[OPERATOR]** cold-reboot verification of the whole stack: all 7 launchd jobs load, containers healthy, `deploy.sh` reports zero drift between repo and `~/ibc` (success criterion 7).

### Story 15: Broker-stop prototype spike on DUN551088

**Size:** 1 SP | **Depends on:** — | **Source:** design T6 (D16) — decision gate for Stories 16–17

**Files:**
- Create: `docs/operations/broker-stop-prototype.md` (findings)

**Acceptance criteria:**
- [ ] A GTC stop order placed on DUN551088 via ib_insync; written findings on: trigger semantics (STP trigger method), persistence across a Gateway restart, visibility in `openTrades`, interaction with a cancel-all.
- [ ] Go/no-go note for the production design (order type, TIF, sizing hook) — the open question from the design's sign-off list ("confirm the broker-stop prototype behaves before the v2 gate epoch starts").

### Story 16: GTC stop placement sized by the IPS stop rule

**Size:** 2 SP | **Depends on:** Stories 4, 15 | **Source:** design T6 (D16 — broker-native stops are the PRIMARY protection)

**Files:**
- Modify: `services/execution/ib_executor.py` + `order_manager.py` (stop order type), `services/risk_management/runner.py` (stop levels per IPS rule)
- Test: `tests/services/execution/test_runner.py`, `tests/services/risk_management/`

**Interfaces:**
- Produces: every open position gets a GTC stop order AT IB, sized by the IPS stop rule — protection that survives Redis/Postgres/Docker/host failure. Feature-flagged (`config/default.yaml`), default OFF until the epoch-v2 boundary (Story 30). This supersedes the daily-close-marks accepted risk.

**Acceptance criteria:**
- [ ] On position open (and on startup for uncovered positions), a GTC stop is placed at the IPS-rule level, stamped with account + orderRef.
- [ ] Whole-position coverage: sum of stop quantities equals the open position quantity per {account, portfolio, con_id}.
- [ ] Flag off ⇒ behavior identical to today (regression suite green).
- [ ] Verify: `pytest tests/services/execution/ tests/services/risk_management/ -v -k broker_stop`

### Story 17: Broker-stop verifier/adjuster scan + kill-path interaction

**Size:** 2 SP | **Depends on:** Story 16 | **Source:** design T6 (D16)

**Files:**
- Modify: `services/risk_management/runner.py` (30-min scan role change), `services/execution/` (cancel-all interaction)
- Test: `tests/services/risk_management/test_exit_emitter.py` (verifier), drill 3c

**Interfaces:**
- Produces: the 30-min software scan's stop-loss role becomes VERIFY/ADJUST the broker stop — recreate if missing, resize after fills, alert on drift — with the ledgered software emit path (Stories 4–6) remaining as fallback and for trim/kill exits broker stops can't express. Kill-path cancel-all must not orphan stop coverage mid-liquidation.

**Acceptance criteria:**
- [ ] **#29** missing GTC stop recreated within one scan cycle; stop resized after a partial fill; drift (wrong size/price) alerts; kill-path cancel-all does not leave positions stop-less mid-liquidation (pytest where mockable + drill 3c).
- [ ] Success criterion 3c **[OPERATOR-ASSISTED DRILL]**: every open paper position has a correctly-sized GTC stop at IB; it survives a Gateway restart; the verifier recreates a deleted one within one cycle.
- [ ] Verify: `pytest tests/services/risk_management/ -v -k verifier`

### Story 18: Approved-orders DLQ audit + operator drain

**Size:** 1 SP | **Depends on:** Stories 4, 14 | **Source:** design item 1 ("drain the poison backlog")

**Files:**
- Create: audit note in `docs/operations/` (superseded-entry table)

**Acceptance criteria:**
- [ ] Every entry in `stream:approved_orders:dlq` audited and mapped to its superseding new-path intent (or flagged if not superseded).
- [ ] **[OPERATOR]** human runs the deletion per repo policy; agent hands over the exact command + post-check (`EXISTS stream:approved_orders:dlq` → 0).
- [ ] Success criterion 8 (second half): DLQ empty after the drain; T6 dlq-depth alert clear.

---

## Phase 2 — Tranche 2: gate validity + evidence machinery

### Story 19: Bounded delisted-name fallback (coverage metric, 5% floor, BLOCKED)

**Size:** 2 SP | **Depends on:** — | **Source:** design T10 (D14)

**Files:**
- Modify: `backtest/` baseline generation (e.g. `backtest/divergence.py` / runner), `scripts/divergence_monitor.py`
- Test: `tests/backtest/`

**Interfaces:**
- Produces: the baseline artifact reports a coverage metric (excluded names as % of point-in-time membership-days). Exclusions ≤ 5% or the baseline state is **BLOCKED** — the divergence monitor refuses a BLOCKED baseline rather than degrading. A conservative write-off treatment for unpullable names is the documented alternative.

**Acceptance criteria:**
- [ ] **#30 (floor half)** baseline generation with exclusions > 5% membership-days → `BLOCKED`; the monitor refuses to score against it.
- [ ] Coverage metric present in the baseline artifact; exclusion list documented per name.
- [ ] Verify: `pytest tests/backtest/ -v -k coverage`

### Story 20: Membership snapshot, SECTOR_MAP extension, headline re-run, divergence rebaseline

**Size:** 2 SP (agent-side; operator data pull gates it) | **Depends on:** Stories 13, 14, 19 | **Source:** design item 5; fixes P1 "divergence blind since T5"

**Files:**
- Modify: `shared/universe.py` (`SECTOR_MAP`), membership snapshot data, baseline artifact
- Reference: `docs/operations/backtest-baseline.md`

**Acceptance criteria:**
- [ ] S&P membership snapshot built; `SECTOR_MAP` extended so newly added delisted names get REAL sector labels — interaction with `821207e` checked (unmapped sectors freeze entries; `Unknown` collapse would distort the 30% sector cap in the re-run).
- [ ] **[OPERATOR]** IB bars pulled including delisted names (the single operator-only step gating this tranche — schedule it first).
- [ ] Headline re-run on the point-in-time universe; baseline regenerated with the Story-19 coverage floor met.
- [ ] Success criterion 5: divergence job exits 0 with all included sleeves reporting against the T5-realistic baseline (any unpullable names documented in the exclusion list).
- [ ] `run_divergence.sh` deployed via `deploy.sh` (no more `~/ibc` hand-forks).

### Story 21: `__drill__` portfolio-tag exclusion

**Size:** 1 SP | **Depends on:** — | **Source:** design T11 (D15)

**Files:**
- Modify: `scripts/run_paper.py` / `scripts/paper_state.py` (tag), `scripts/ops/go_live_gate.py` (gate metrics), `scripts/divergence_monitor.py` (divergence input)
- Test: `tests/scripts/`

**Acceptance criteria:**
- [ ] **#30 (tag half)** `__drill__`-tagged positions/fills are excluded from gate metrics and divergence input.
- [ ] Tag round-trips: a drill order carries the tag from emission through fill projection to `equity_snapshots` (note: `run_pipeline_report.sh` already excludes `\_%`-prefixed portfolios — verify consistency).
- [ ] Verify: `pytest tests/scripts/ -v -k drill`

### Story 22: Evidence state store — migration + ORM

**Size:** 2 SP | **Depends on:** — | **Source:** direction 1A/D15

**Files:**
- Create: `shared/models/evidence.py`, `alembic/versions/<rev>_evidence_state_store.py`
- Test: `tests/shared/test_evidence_store.py`

**Interfaces:**
- Produces (observations ONLY, never derived truth):
  - `DivergenceDaily(id, sleeve, session_date, status ∈ {OK, WARNING, BREACH, NO_DATA}, baseline_id, window_sessions, threshold, created_at)` — exactly the per-sleeve verdict rows the monitor produced; unique on (sleeve, session_date, baseline_id).
  - `GateEpoch(id, rung, started_at, manifest JSONB, status ∈ {RUNNING, CLEAN, BREACHED, EXTENDED, RESTARTED}, ended_at)` — manifest per D13: portfolio weights, sleeve set, baseline id, membership snapshot, divergence window/threshold, cost model, money-path commit.
  - `GateEpochEvent(id, epoch_id, event_type, detail JSONB, occurred_at)` — transition EVENTS (start, restart+reason, breach, extend, disarm, rung change).
  - `DrillOutcome(id, epoch_id, drill_type ∈ {restart_halt, synthetic_stop}, passed, detail, occurred_at)`.
- NO streak columns, NO blindness columns — those are computed at read time (Story 23).

**Acceptance criteria:**
- [ ] `alembic upgrade head` from the current merged head (`82623f87013d` lineage) creates the four tables; downgrade drops them.
- [ ] ORM round-trip tests for each model; uniqueness constraint on divergence rows enforced.
- [ ] Verify: `pytest tests/shared/test_evidence_store.py -v; alembic upgrade head && alembic downgrade -1` (scratch DB).

### Story 23: Shared evidence query helper

**Size:** 2 SP | **Depends on:** Story 22 | **Source:** direction 1A/D15/D11 (3A tests 2–3)

**Files:**
- Create: `shared/evidence_store.py`
- Test: `tests/shared/test_evidence_store.py`

**Interfaces:**
- Produces the ONE helper both the digest and `go_live_gate.py` must use:
  - `breach_streak(session, sleeve, as_of: date) -> int` — consecutive BREACH sessions (WARNING does not count; only counts once the 30-session window lies fully inside the current rung).
  - `blind_sessions(session, as_of: date) -> list[date]` — DERIVED: NYSE trading days (via `shared/market_calendar.py`) with no divergence row; a dead monitor is self-evident.
  - `epoch_progress(session, epoch_id) -> EpochProgress` — sessions elapsed excluding pause days (blind days pause the clock), round-trips completed, exposure-session %, criteria green/amber, blindness-streak (>5 consecutive = safety incident).

**Acceptance criteria:**
- [ ] **3A#2** the 10-consecutive-session trigger fires at exactly 10; a 9-OK-9 pattern stays clean.
- [ ] **3A#3** NO_DATA pause-clock arithmetic: blind days count neither way and pause the epoch clock; >5 consecutive blind sessions flags a safety incident.
- [ ] `go_live_gate.py` and the digest (Story 26) import from this module only — no second implementation, no log parsing.
- [ ] Verify: `pytest tests/shared/test_evidence_store.py -v`

### Story 24: Divergence monitor writes verdict rows

**Size:** 1 SP | **Depends on:** Story 22 (meaningful data needs Story 20) | **Source:** direction 1A

**Files:**
- Modify: `scripts/divergence_monitor.py`
- Test: `tests/scripts/` (monitor tests)

**Acceptance criteria:**
- [ ] Every monitor run persists one `DivergenceDaily` row per included sleeve (status incl. NO_DATA) with baseline id, window, threshold — the pins the epoch manifest records.
- [ ] Re-run on the same date/baseline is idempotent (upsert or skip, no duplicates).
- [ ] Monitor exit codes unchanged (launchd wrapper contract preserved, incl. exit-3 BLIND from Story 13).
- [ ] Verify: `pytest tests/scripts/ -v -k divergence`

### Story 25: Epoch manifest recording + rung-transition engine

**Size:** 2 SP | **Depends on:** Stories 22, 23 | **Source:** direction D13/D16 (3A tests 4–5)

**Files:**
- Create: `scripts/ops/record_epoch.py` (CLI: start epoch, record manifest, evaluate transition, record drill outcome)
- Test: `tests/scripts/test_record_epoch.py`

**Interfaces:**
- Produces: epoch lifecycle per the ladder rules — clean-epoch evaluation (breach streak ≥10 · 12% USD-NAV drawdown bound · zero safety incidents · drill set passed · ≥15 round-trips AND ≥60% exposure sessions else EXTEND); de-scaling (any breach → drop one rung, epoch restarts); **Rung-0 floor** (breach at Rung 0 disarms live entirely → paper; re-entry needs a fresh gate review); two consecutive breached epochs at any rung → Rung 0 + incident review; action precedence halt → demote → de-scale recorded as ONE event.

**Acceptance criteria:**
- [ ] **3A#4** rung transitions including the Rung-0 disarm floor behave per the ladder table (property-style tests over event sequences).
- [ ] **3A#5** drill-outcome recording: a drill run writes a `DrillOutcome` row tied to the epoch.
- [ ] Epoch start records the full D13 manifest including the money-path commit hash; a manifest-item change mid-epoch → `RESTARTED` event with reason.
- [ ] Evidence-quantum shortfall → `EXTENDED`, never CLEAN.
- [ ] Verify: `pytest tests/scripts/test_record_epoch.py -v`

### Story 26: Weekly evidence digest

**Size:** 2 SP | **Depends on:** Stories 23, 25, 12 | **Source:** direction D3.4/2A (3A tests 1, 6)

**Files:**
- Create: `scripts/ops/evidence_digest.py`, `deploy/launchd/local.algo-evidence-digest.plist`, `deploy/launchd/run_evidence_digest.sh`
- Test: `tests/scripts/test_evidence_digest.py` (golden renders)

**Interfaces:**
- Consumes: `shared/evidence_store.py` ONLY (never logs/scrollback); the verified Telegram bot creds; the dead-man check URL.
- Produces: weekly Telegram digest — equity vs baseline · per-sleeve divergence status · DLQ depth · alerts fired · drills due · epoch progress (week N of 6, criteria green/amber). Failure semantics (2A): NEVER skips (quiet week sends "no fills, epoch week N of 6, all green"); partial sources send with a MISSING-SOURCES banner; BLIND/NO_DATA renders as the loudest FIRST line; pings the dead-man check on success (no ping by deadline → external page).

**Acceptance criteria:**
- [ ] **3A#1** golden digest renders per state: all-green / quiet week / MISSING-SOURCES / BLIND-first / breach-streak (fixture-driven snapshot tests).
- [ ] **3A#6** dead-man ping asserted on successful send; a source failure still sends (banner) rather than raising.
- [ ] Digest reads only the evidence store — test injects a store fixture, no log files touched.
- [ ] **[OPERATOR]** launchd job installed via `deploy.sh`.
- [ ] Verify: `pytest tests/scripts/test_evidence_digest.py -v`

### Story 27: Morning 3-line digest upgrade

**Size:** 1 SP | **Depends on:** — | **Source:** direction D3.4

**Files:**
- Modify: `deploy/launchd/run_pipeline_report.sh` (compact Telegram summary section)

**Acceptance criteria:**
- [ ] The post-04:15 Telegram summary reports exactly the direction's three facts: fills, rejections, halt state (extending the existing BUY/SELL/divergence summary; halt state read from the DB latch, not logs where feasible).
- [ ] A failed/missing paper run still sends (existing ❌ semantics preserved).
- [ ] Verify: shell run against a fixture log directory; visual check of one live send.

### Story 28: `run_paper.py` publish-failure alert

**Size:** 1 SP | **Depends on:** — | **Source:** design T14 (P2: "a down stack must not look like a successful run")

**Files:**
- Modify: `scripts/run_paper.py:1344-1348`
- Test: `tests/scripts/`

**Acceptance criteria:**
- [ ] **#20** Redis publish failure → high-priority alert emitted AND nonzero exit (so launchd logs it); the warn-only path is gone.
- [ ] Verify: `pytest tests/scripts/ -v -k publish_failure`

### Story 29: Per-epoch drills, evidence-isolated

**Size:** 1 SP (mostly operator) | **Depends on:** Stories 14, 17, 21, 22 | **Source:** design item 6 (D15) + success criteria 1–3

**Files:**
- Create: drill runbook in `docs/operations/` + outcomes recorded via `record_epoch.py`

**Acceptance criteria:**
- [ ] **[OPERATOR-ASSISTED]** restart/halt drill: buy approved pre-halt + service restart → durably rejected, never submitted; a ledgered liquidation SELL published during the halt still executes; a race-window order is cancelled by the sweep (success criterion 3).
- [ ] **[OPERATOR-ASSISTED]** synthetic stop-loss drill: breach → ledger intent → execution → IB paper fill recorded, no DLQ entry (success criterion 1), under a `__drill__` portfolio tag.
- [ ] Explicit unwind + reconciliation step; drill fills invisible to gate metrics and divergence (Story 21 verified live).
- [ ] Outcomes recorded as `DrillOutcome` rows (first instance of the recurring per-epoch set).

### Story 30: Epoch v2 start (milestone)

**Size:** 1 SP | **Depends on:** Stories 16, 17, 20, 21, 25, 29 (i.e. tranches 1+2 deployed) | **Source:** direction Track 1 item 2 / design item 7 (D15)

**Acceptance criteria:**
- [ ] Broker-native stops enabled (feature flag ON) AT the epoch boundary so v2 evidence reflects the protected system.
- [ ] Epoch manifest recorded via `record_epoch.py` (weights, sleeve set, baseline id, membership snapshot, window=30/threshold, cost model, money-path commit).
- [ ] Pre-fix paper history preserved but labeled pre-fix (it measured a system whose stops never executed); v2 window = 60 calendar days per the gate rule.
- [ ] Stated in writing: gate review = 8-gate checklist + clean-epoch criteria + D10 edge evaluation + D14 draft/adversarial-review/7-day cooling-off.

### Story 31: Rung-0 economics investigation (D8)

**Size:** 2 SP | **Depends on:** Story 20 (PIT universe + cost model) | **Source:** direction sequencing item 2 — **gates epoch v2's restructure decision, run before/alongside Story 30**

**Files:**
- Modify: `scripts/run_backtest.py` (whole-share rounding mode at fixed capital, if not already expressible)
- Create: `docs/operations/rung0-economics.md`
- Test: `tests/backtest/` (rounding mode)

**Acceptance criteria:**
- [ ] Backtest run at Rung-0 capital (~USD 3.7k) with whole-share rounding + the commission floor; quantified: per-position sizes (~$34–119 expected), commission drag in bps (84–293 expected range), fidelity vs the unconstrained baseline.
- [ ] Whole-share mode covered by a unit test (rounding + cash constraint respected).
- [ ] **[OPERATOR]** restructure decision logged from the three named options: concentrate Rung 0 on fewer sleeves / raise Rung-0 funding / accept with a capital-specific baseline — the agent drafts the decision memo with the numbers; the owner decides.
- [ ] Per-rung capital-specific baseline requirement (D16) noted in the memo for whichever option wins.

---

## Phase 3 — Tranche 3 + governance + edge framework (parallel with epoch v2)

### Story 32: ML-path decision, then loader fix OR demotion

**Size:** 2 SP | **Depends on:** — (dated: end of tranche 3) | **Source:** design T12 (D17)

**Files:**
- If KEEP: `scripts/retrain_model.py` + `shared` model registry (single format, `content_hash` at save, cadence wired); if DEMOTE: remove/demote `services/signal_generation/` + `services/ml_model/` runners, document `run_paper.py` as the only recommendation source
- Test: `tests/` per branch below

**Acceptance criteria:**
- [ ] Decision memo written on its own rubric (architecture decision, NOT promotion-pipeline): keep-and-wire vs demote, noting `retrain_cadence_months` has no caller and `model_versions` has 0 rows; confirm whether the daily path is intentionally model-free.
- [ ] **#19 (conditional)** if KEEP: fresh retrain → `load_active()` accepts the model (roundtrip pytest; success criterion 6). If DEMOTE: replacement test asserts the dormant runners are gone/inert and compose no longer starts them.
- [ ] Verify: `pytest tests/ -v -k "retrain or ml_model"`

### Story 33: CI — linux lockfile recompile + Dependabot triage

**Size:** 1 SP | **Depends on:** — | **Source:** design T13 (red on every run since 08-09)

**Files:**
- Modify: `.github/workflows/security.yml`, `requirements.lock`

**Acceptance criteria:**
- [ ] `requirements.lock` recompiled on linux (container or CI job) so the ubuntu lockfile-recompile step matches.
- [ ] `security.yml` green on main (success criterion 7, first half).
- [ ] The 18 Dependabot PRs triaged: each merged, closed-with-reason, or queued — none left red-and-ignored.
- [ ] Verify: `gh run list --workflow=security.yml --limit 1` shows success post-merge.

### Story 34: Per-sleeve kill criteria + IPS amendment + checklist amendment

**Size:** 2 SP | **Depends on:** — (hard prerequisite for Rung 0 arming) | **Source:** direction D3.3/D9/D14/D16

**Files:**
- Create: `docs/operations/sleeve-kill-criteria.md`
- Modify: `docs/operations/investment-policy-statement.md` (§5 superseded per D16, §9 amendment logged), `docs/operations/go-live-checklist.md` (Two-Person Approval section)

**Acceptance criteria:**
- [ ] Written kill criteria for EACH of the six incumbent sleeves per D3.3: 10-consecutive-session out-of-band divergence vs own baseline · sleeve drawdown > backtest max-DD × 1.5 (or a justified per-sleeve budget) · signal staleness > 5 sessions · attributable safety incident — "no written kill criteria → no live promotion" satisfied before Rung 0.
- [ ] IPS amendment: ladder supersedes §5 IN FULL; divergence monitor named a capital-decision input by rule; 30% household satellite ceiling + W-8BEN closure restated as standing constraints.
- [ ] Checklist amended ONCE for the D14 solo substitute: written draft → independent cross-model AI adversarial review → 7-day cooling-off → any unresolved challenge blocks.
- [ ] Promotion funding rule recorded: newly promoted sleeve takes pro-rata weight, ≤10% of portfolio per promotion, rebalance logged.

### Story 35: Edge-validation framework core — deflated Sharpe + multiple-testing correction

**Size:** 2 SP | **Depends on:** — (occupies a research WIP slot; must finish before Rung 0 arms) | **Source:** direction D10

**Files:**
- Create: `research/edge_framework/deflated_sharpe.py`, `research/edge_framework/__init__.py`
- Test: `tests/research/test_deflated_sharpe.py`

**Interfaces:**
- Produces: `deflated_sharpe_ratio(returns: pd.Series, n_trials: int, *, skew=None, kurt=None) -> DSRResult(dsr, psr, sr, threshold_sr)` implementing Bailey & López de Prado's DSR (expected-max-SR benchmark across `n_trials`, non-normality-adjusted SR std error); `n_trials` must be an explicit, documented input (the sleeve-selection history — six survivors after dropping two losers ⇒ n_trials ≥ 8).

**Acceptance criteria:**
- [ ] DSR unit-tested against published reference values (and degenerate cases: 1 trial ⇒ PSR; huge n_trials ⇒ DSR → 0 for modest SR).
- [ ] Multiple-testing correction takes the trial count from a declared registry, not a default.
- [ ] Verify: `pytest tests/research/test_deflated_sharpe.py -v`

### Story 36: Edge framework — holdout protocol + parameter-stability tests

**Size:** 2 SP | **Depends on:** Story 35 | **Source:** direction D10

**Files:**
- Create: `research/edge_framework/holdout.py`, `research/edge_framework/stability.py`
- Test: `tests/research/test_edge_framework.py`

**Interfaces:**
- Produces: `HoldoutProtocol` (pre-registered train/holdout split with purge/embargo consistent with the T5 purged walk-forward; holdout touched ONCE per pre-registered evaluation) and `parameter_stability(backtest_fn, param_grid, metric) -> StabilityReport` (metric surface over neighboring parameter values; a sleeve whose edge lives on a knife's-edge parameter fails).
- These checks become permanent gate-S/gate-P requirements in the promotion pipeline.

**Acceptance criteria:**
- [ ] Holdout protocol enforces single-evaluation semantics (second evaluation on the same registered split raises/flags).
- [ ] Stability report flags a synthetic knife-edge strategy and passes a synthetic plateau strategy (fixture tests).
- [ ] Promotion-pipeline doc updated: gate S and gate P require these checks (direction D3.2 wording).
- [ ] Verify: `pytest tests/research/test_edge_framework.py -v`

### Story 37: Incumbent sleeve edge evaluation (all six)

**Size:** 2 SP | **Depends on:** Stories 20, 35, 36 | **Source:** direction D9/D10 — **must complete before Rung 0 arms; runs parallel with epoch v2**

**Files:**
- Create: `docs/operations/incumbent-edge-evaluation.md` + evaluation runner script under `research/edge_framework/`

**Acceptance criteria:**
- [ ] Each of the six incumbent sleeves evaluated: DSR with the honest trial count (post-hoc selection after dropping two losers is exactly what this corrects for), holdout performance on the PIT universe with T5-realistic costs, parameter stability.
- [ ] Written verdict per sleeve feeding the gate review; explicit statement that divergence-fidelity is NOT edge (direction: "neither substitutes for the other").
- [ ] Results land before gate day (gate review is blocked without them).

### Story 38: Findings register + pager-decision documentation (closes the paper trail)

**Size:** 1 SP | **Depends on:** everything it records (run last) | **Source:** design T15 (P2)

**Files:**
- Modify: `docs/operations/implementation-review-2026-08-06.md` (§12 register), `docs/reviews/threads/` checklists

**Acceptance criteria:**
- [ ] All 32 §12 findings updated to verified reality (success criterion 8, first half); thread checklists checked to match shipped state.
- [ ] Telegram-as-pager decision documented (both layers; heavier stack only if Telegram proves insufficient).
- [ ] Explicit deferrals recorded WITH risk-acceptance rationale: intraday marks (superseded by broker stops), margin plumbing (unlevered long-only), reprice-with-quotes (age-out stands), sweep-calendar constructor param.

---

## Explicitly NOT in this plan (deferred by the source docs)

Arming the live U* account (gated on epoch v2 + gate review) · full T8 consolidation beyond the Story-32 decision · reprice-with-quotes · margin plumbing · per-service Redis ACLs / HMAC · mid-session completed-order reconciliation · Dependabot auto-regen · regime-filter research (WIP limit) · sentiment 2026-11 gate eval (own clock, first full promotion-pipeline case).
