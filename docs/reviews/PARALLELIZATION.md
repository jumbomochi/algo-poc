# T1–T9 Parallelization Plan

Coordination doc for working the 9 open review PRs (#12–#20) across **two concurrent
sessions**. Source: the 2026-08-06 implementation review
(`docs/operations/implementation-review-2026-08-06.md`) and the per-thread specs in
`docs/reviews/threads/T*.md`.

Each PR currently contains **only its thread spec** — the implementation is still to be
written. "Parallelism" here is about which implementation footprints collide, not about
the current (empty) diffs.

## The core constraint

`risk/runner.py` and `execution/runner.py` are each edited by multiple threads, so those
threads cannot run as independent sessions without constant rebasing.

| Hot file | Touched by |
|---|---|
| `risk/runner.py` | **T1, T2, T4** |
| `execution/runner.py` | **T1, T4, T7** |
| `engine.py` | T1, T7 |
| `redis_client.py` | T3, T4, T6 |
| `docker-compose.yml` | T3, T6 |
| `registry.py` | T8, T9 |
| `run_paper.py` | T5, T8 |
| `signal_generation/runner.py`, `ml_model/runner.py` | T4, T8 |

T1 is inherently cross-service (risk decides the kill; execution liquidates), so it can't
be split from either hot file. **T1, T2, T4, T7 are one tightly-coupled cluster** and must
be one serial track. Everything else is genuinely independent.

---

## Session A — Runtime-safety cluster (P0 focus, SERIAL)

Owns `risk/runner.py`, `execution/runner.py`, `engine.py`, `capital.py`, `kill_switch.py`,
`ib_executor.py`, `projector.py`, `order_manager.py`, and the service runners for
`ml_model` / `signal_generation` / `notifications`.

Do these **in order** (one branch/PR at a time; do not parallelize within the session):

1. **T2** — runtime risk enforcement (PR #13, `fix/T2-runtime-risk-enforcement`) [P0]
   Drive `run_stop_loss_check` / `run_passive_scan` periodically; measure drawdown on
   **marked book equity**, not `deployable_capital`. Do first because T1's circuit breaker
   needs a real drawdown reading.
2. **T1** — kill-switch & circuit-breaker (PR #12, `fix/T1-kill-switch-breaker`) [P0]
   Latch + persist kill (fail-closed on restart); fire liquidation on the 20% breaker;
   idempotent liquidation via OrderLedger; reload authoritative positions at kill time.
3. **T7** — execution lifecycle (PR #18, `fix/T7-execution-lifecycle`) [P2]
   Same `execution/runner.py` + `engine.py` surface as T1 → serialize behind it. Re-register
   callbacks on reconnect; terminalize under-filled intents; guard duplicate terminal status.
4. **T4** — idempotent fills & stream hygiene (PR #15, `fix/T4-idempotent-fills-streams`) [P1]
   Dedup risk book by `execution_id`; use `commission_trading` (USD); steady-state poison →
   DLQ+ack+alert; `drain_pending` on ml_model/signal_generation.

---

## Session B — Everything else (mutually PARALLEL, independent of Session A)

Can start **immediately** and does not touch `risk/runner.py` / `execution/runner.py`.
Work these in any order except T8 last:

- **T5** — backtest realism (PR #16, `fix/T5-backtest-realism`) [P1]
  Owns `backtest/`, `simulator.py`, `universe.py`, `run_backtest.py`, `metrics.py`,
  `fetch_fundamentals.py`, `train_signal_model.py`, `backtest/divergence.py`.
  Fully independent; highest-leverage P1 (feeds the scale-up decision).
- **T3 + T6 together** — infra (PRs #14 `fix/T3-message-bus-lockdown` [P0] and
  #17 `fix/T6-observability-healthchecks` [P1]). Co-located because both own
  `docker-compose.yml` + `redis_client.py`. T3 = Redis/PG auth + loopback binding;
  T6 = wire metrics, container healthchecks, alert rules, bound Redis streams.
- **T9** — security/API hardening (PR #20, `fix/T9-security-hardening`) [P2]
  Owns `api/auth.py`, `api/app.py`, `schemas/messages.py`, dependency lockfile,
  `registry.py` (integrity hash on model load).
- **T8** — consolidate dual implementations (PR #19, `fix/T8-consolidate-implementations`) [P2]
  **LAST.** Its own spec says "after T1/T2/T4 land." Collides with T4 on the service
  runners and with T5/T9 on `run_paper.py` / `registry.py`. Ideally start after Session A
  has merged.

---

## Cross-session caveats (small, three total)

1. **`redis_client.py`** — the only real A↔B overlap. T4 (Session A) edits the DLQ region
   (~lines 118–126); T3/T6 (Session B) edit maxmemory/trim (~lines 31, 126). Different
   regions; whoever merges second rebases ~3 lines.
2. **`registry.py`** — T9 (integrity hash) then T8 (loader fix), both in Session B. Do T9
   before T8.
3. **`run_paper.py`** + service runners — T8 collides with T5/T9/T4, which is why T8 is
   last and ideally lands after Session A.

## Merge order (recommended)

```
T2 → T1 → T7 → T4        (Session A, serial)
T5, {T3,T6}, T9          (Session B, parallel, any order)
T8                       (after A merges)
```

## Per-PR file footprints (reference)

- **T1** #12: `kill_switch.py`, `risk/runner.py`, `execution/runner.py`, `engine.py`
- **T2** #13: `risk/runner.py`, `capital.py`
- **T3** #14: `docker-compose.yml`, `redis_client.py`, `docker-compose.override.yml.example`
- **T4** #15: `risk/runner.py`, `execution/runner.py`, `ml_model/runner.py`,
  `signal_generation/runner.py`, `redis_client.py`, `notifications/runner.py`
- **T5** #16: `universe.py`, `run_backtest.py`, `simulator.py`, `backtest/runner.py`,
  `fetch_fundamentals.py`, `run_paper.py`, `train_signal_model.py`, `metrics.py`,
  `backtest/divergence.py`
- **T6** #17: `observability.py`, `config/prometheus.yml`, `docker-compose.yml`, `redis_client.py`
- **T7** #18: `ib_executor.py`, `execution/runner.py`, `projector.py`, `engine.py`, `order_manager.py`
- **T8** #19: `run_paper.py`, `signal_generation`/`ml_model`, `registry.py`, `retrain_model.py`
- **T9** #20: `registry.py`, `api/auth.py`, `api/app.py`, `schemas/messages.py`, `.env`, lockfile
