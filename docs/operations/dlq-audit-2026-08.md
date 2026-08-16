# Approved-orders DLQ audit — 2026-08

**Issue:** [KAN-21](https://huiliang.atlassian.net/browse/KAN-21) · **Audited:** 2026-08-16 18:15–18:30 UTC (2026-08-17 early SGT)
**Target:** `stream:approved_orders:dlq` on the paper stack's Redis (`algo-poc-redis-1`, published on `127.0.0.1:56379`)
**Verdict: the queue does not exist. Zero entries. Nothing to drain, and nothing was deleted.**

The dead-lettering [KAN-7](https://huiliang.atlassian.net/browse/KAN-7) fixed
was real in code but **never once triggered**: no exit was lost, because no exit
was ever attempted. Which means the stop-loss path is **fixed and unproven, not
fixed and demonstrated** — it has still never run end-to-end against the live
book, and the first genuine 15% breach will be its first real test.

---

## Why this audit was expected to find a backlog

KAN-21 was written on the premise that every stop-loss and hard-ceiling trim
sell emitted since the T2 periodic-risk driver landed had dead-lettered, and
that those messages were still parked in `stream:approved_orders:dlq`.

The *mechanism* was real and is well documented in
[KAN-7](https://huiliang.atlassian.net/browse/KAN-7) (`4a1e066`): both exit
paths published with a synthetic `stop-loss-{uuid4}` / `passive-trim-{uuid4}`
recommendation id and no backing order intent, while execution's first act on
an approved order is a ledger lookup — so each one would have raised
`OrderIntentNotFound`, landed in the DLQ, and been acked, silently.

What the premise assumed but did not check is that the emitters had **fired at
all**. They had not. The defect was latent, never triggered.

## Evidence

All commands run against the live paper stack from `/Users/huiliang/GitHub/algo-poc`.

### 1. The DLQ key does not exist

```bash
docker compose exec -T redis redis-cli -a "$REDIS_PASSWORD" --no-auth-warning EXISTS stream:approved_orders:dlq
# -> 0
docker compose exec -T redis redis-cli -a "$REDIS_PASSWORD" --no-auth-warning KEYS '*dlq*'
# -> (empty)
```

The whole keyspace is 9 keys, all of them live streams:
`stream:alerts`, `stream:approved_orders`, `stream:events`, `stream:fills`,
`stream:fundamentals`, `stream:kill`, `stream:market_data`,
`stream:recommendations`, `stream:signals`. No dead-letter stream of any kind.

### 2. No sell has ever reached `stream:approved_orders`

`XLEN stream:approved_orders` = **133**, spanning `1783615608818-0`
(2026-07-09T16:46:48Z) to `1786674948768-0` (2026-08-14T02:35:48Z). The history
is complete, not a survivor of trimming: `publish()` caps streams at
`DEFAULT_STREAM_MAXLEN = 25_000` (`shared/redis_client.py:52`), three orders of
magnitude above 133.

Across all 133 entries:

| Field | Values present |
|---|---|
| `action` | `buy` × 133 — **no `sell`, ever** |
| `stop` / `trim` / `liquidat` (any field, case-insensitive) | 0 occurrences |

Every recommendation id is of the
`sleeve-<run_date>-<account_id>-<mode>-<portfolio>-<ticker>-<action>` form
(`scripts/run_paper.py:887-890`). No synthetic uuid id was ever published, so
none could have dead-lettered.

### 3. The ledger agrees

`order_intents` holds 112 rows (2026-07-30 → 2026-08-14), **all `action = BUY`**
(`CANCELLED` 54, `EXPIRED` 30, `RISK_REJECTED` 15, `SUBMISSION_FAILED` 13). No
risk-side exit intent has ever been created, under either the old or the new
scheme.

### 4. Why the emitters never fired: no breach ever occurred

Both triggers are threshold checks against live book state, and the book has
never come near either threshold.

Trailing stop is `stop_loss_trailing_pct: 15.0` (`config/default.yaml`). Worst
current drawdown from the trailing high, across all 12 open positions:

| Ticker | Sleeve | Current | High since entry | Trailing DD | vs 15% stop |
|---|---|---|---|---|---|
| CSCO | momentum | 111.68 | 122.57 | **8.88%** | no breach |
| GOOGL | quality_value | 345.90 | 377.65 | 8.41% | no breach |
| AAPL | quality_value | 305.93 | 332.47 | 7.98% | no breach |
| LLY | sector_rotation | 1180.16 | 1231.94 | 4.20% | no breach |
| UNH | momentum | 401.73 | 415.33 | 3.27% | no breach |
| HACK | thematic_momentum | 118.18 | 121.89 | 3.04% | no breach |
| PANW | sector_rotation | 384.27 | 396.00 | 2.96% | no breach |
| CIBR | thematic_momentum | 99.60 | 102.20 | 2.54% | no breach |
| SKYY | thematic_momentum | 165.34 | 168.91 | 2.11% | no breach |
| META | quality_value | 589.85 | 599.12 | 1.55% | no breach |
| TLT | tail_risk_hedge | 82.04 | 82.82 | 0.94% | no breach |
| HUM | sector_rotation | 389.05 | 389.05 | 0.00% | no breach |

Hard ceiling is `hard_ceiling_pct: 15.0` of NAV. Largest single position is
PANW at **3.46%** — under even the 7% soft ceiling, let alone the trim trigger.
(NAV and "deployable capital" are the same denominator here:
`_refresh_portfolio_from_db` sets `nav = float(snapshot.deployable_capital)`,
`services/risk_management/runner.py:1968-1969`.)

The driver was live and running the sweep at least once: the sole `dlq_backlog`
alert in `stream:alerts` (2026-08-09T13:53:16Z, `stream:fills:dlq`, depth 28) is
emitted by `_check_dlq_depths()`, the last step of `run_periodic_risk_checks()`
— reached only after the sequential `await`s of `run_stop_loss_check()` and
`run_passive_scan()`, so that scan evaluated both triggers and raised neither.
This is one data point, not proof of continuous operation; the conclusion does
not rest on it, because §2, §3 and §5 each independently show nothing was ever
emitted.

### 5. No dead-letter alert for this stream has ever been raised

Since `fb0281a` ("steady-state poison handling — DLQ + ack + alert",
2026-08-08), execution's steady-state consumer publishes a **high-priority
`poison_message` alert per dead-lettered message**, approved orders included
(`services/execution/runner.py:1847-1852`, reached from `_consume_and_process`
on `APPROVED_ORDERS_STREAM` at `1790-1796`).

`stream:alerts` holds 44 entries spanning 2026-07-04T15:51Z → 2026-08-09T13:53Z,
untrimmed. Event-type census:

| `event_type` | Count |
|---|---|
| `drawdown_rejection` | 20 |
| `entry_rejection` | 18 |
| `kill_switch_liquidation` | 2 |
| `kill_switch_activated` | 2 |
| `ops_test` | 1 |
| `dlq_backlog` | 1 (`stream:fills:dlq`, depth 28) |
| **`poison_message`** | **0** |

Not one poison message on any stream, ever. This is independent of the `EXISTS`
check in §1 — it would still hold if the Redis keyspace had been rebuilt.

Two limits worth stating: it covers 2026-08-08 (when the alert was added)
through 2026-08-09T13:53Z, the last entry in `stream:alerts`; and the
startup-replay drain (`services/execution/runner.py:257-267`) dead-letters
without publishing this alert, so it covers steady-state consumption only.
Evidence §2 carries the rest of the window.

### 6. Corroboration from the 2026-08-10 precedent

The `stream:fills:dlq` clearance on 2026-08-10 ended with an operator check that
**no `*dlq*` keys remained**. That check ran *after* the drain, so on its own it
cannot distinguish "never existed" from "deleted in the same operation" — but
combined with §2 (nothing that could dead-letter was ever published, before or
after) the first reading is the only one available.

Recalled from the operator's account of that clearance, not from a committed
record: no note of it exists in `docs/`, `scripts/` or git history. Treat it as
corroboration, not as a primary source.

## Per-entry inventory and classification

| # | Ticker | Qty | Original timestamp | `_error` | Verdict |
|---|---|---|---|---|---|
| — | — | — | — | — | **No entries. The queue does not exist.** |

**superseded:** 0 · **already-exited:** 0 · **needs-action:** 0

There is no needs-action entry, and — the question that category exists to
answer — there is no unprotected position hiding behind the empty queue. A
needs-action finding would mean a still-open position whose stop was attempted
and lost. Evidence §4 shows no stop was ever *attempted*, because no position
ever breached. The two are different failures and only the second one is
present, so no escalation is required before proceeding.

## Operator actions

### Deletion — not required

There is nothing to delete. `stream:approved_orders:dlq` does not exist, so the
`DEL` that KAN-21 anticipated would be a no-op against a nonexistent key.
**Do not run a speculative `DEL`** — it proves nothing and, if a constant is
ever renamed, a `DEL` on the wrong key returns the same `0`.

### Post-check — run this instead

Confirms the audited state rather than a deletion:

```bash
docker compose exec -T redis redis-cli -a "$REDIS_PASSWORD" --no-auth-warning EXISTS stream:approved_orders:dlq
# expected: 0
docker compose exec -T redis redis-cli -a "$REDIS_PASSWORD" --no-auth-warning KEYS '*dlq*'
# expected: empty
```

### Alert state

`DeadLetterQueueBacklog` (`config/alert_rules.yml`) fires on
`redis_stream_length{key=~".*:dlq"} > 10` for 5m. With the key absent,
redis_exporter emits no `redis_stream_length` series for it at all, so the
alert has never had a firing condition for this queue and has nothing to clear.
The one `:dlq` series that ever crossed the threshold was `stream:fills:dlq`,
resolved 2026-08-10.

Caveat, stated plainly: this could not be observed on a live Prometheus,
because the observability stack was not running at audit time (see open
finding B).

## Deployment state at audit time (KAN-21 AC7)

The audit ran **after** the new emitter path was deployed, so a drain could not
have been refilled by the old one:

- Containers rebuilt and recreated 2026-08-17 02:12 +08 under
  [KAN-17](https://huiliang.atlassian.net/browse/KAN-17); all 10 services
  healthy. Running container image `sha256:21011f60…` matches the built
  `algo-poc-risk-management` image id exactly — no stale-image gap.
- Inside the running container, `/app/services/risk_management/runner.py`
  contains `_emit_ledgered_exit` (5 occurrences) and **zero** `uuid.uuid4`
  — the synthetic-id path that caused the dead-lettering is gone from the
  deployed image, not just from the tree.

## Open findings

Both are recorded here and deliberately **not fixed** — KAN-21 puts "changing
DLQ handling or the alert threshold" out of scope.

**A. No *depth* check covers the approved-orders DLQ.**
`RiskManagementRunner._check_dlq_depths()` iterates
`(RECOMMENDATIONS_STREAM, KILL_STREAM, FILLS_STREAM)` — the streams *risk*
consumes. `stream:approved_orders` is execution's, and execution has no
equivalent depth check.

This is narrower than "nothing watches it": per-message `poison_message` alerts
*do* cover the stream (§5), so a forming backlog would have been loud, one
high-priority alert at a time. What is missing is the aggregate view — the
"there are N of these parked" signal that caught the fills backlog. Only the
Prometheus rule provides that for approved orders, and only above 10 entries.
Guarded by
`tests/operations/test_dlq_audit_note.py::test_open_finding_is_still_open`,
which fails if the gap is closed so this note gets corrected.

**B. The observability stack was down at audit time.**
No `prometheus`, `alertmanager`, `grafana` or `redis-exporter` container was
running, so `DeadLetterQueueBacklog` — and every other rule in
`config/alert_rules.yml` — had no evaluator. Nothing in
`docs/operations/container-deploy.md` or `deploy/launchd/` brings the stack up.
Combined with finding A, an approved-orders backlog would currently produce
per-message alerts but no backlog signal at all. Raised on
[KAN-17](https://huiliang.atlassian.net/browse/KAN-17); worth confirming whether
manual start is intentional or a gap in the deploy.

## What this note preserves

KAN-21's rationale for auditing before draining was that the DLQ messages were
the only record of how long the stop-loss P0 was active and which positions it
touched. That record turns out to be: **the P0 was latent for its whole life.**
Real in code from the T2 driver until KAN-7 (`4a1e066`, 2026-08-16), it was
never reached, because between 2026-07-30 and 2026-08-16 no position in the
paper book breached a 15% trailing stop or a 15% NAV ceiling. No exit was lost,
because no exit was ever attempted.

That is a weaker reassurance than it sounds, for the reason stated in the
verdict at the top: nothing was lost, but nothing was proved either. The
evidence that would have shown the exit path working end-to-end is the same
evidence this audit went looking for and did not find.
