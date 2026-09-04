# What actually runs live

Two signal systems exist in this repo. Only one of them trades. This document
says which, because not knowing was expensive: the 2026-08-06 implementation
review found that the ambiguity is *why* the unwired-safety bugs in T1/T2 hid —
three reviewers could not tell which path was authoritative.

Measured 2026-09-04 against the running stack, not inferred from the code.

## The authoritative path

```
  04:15 launchd  ->  scripts/run_paper.py
                       computes every sleeve's signals in-process
                       (make_*_signals_fn from scripts/run_backtest.py)
                       |
                       |  writes order intents, publishes them
                       v
                     stream:approved_orders   ── 154 entries
                       |
                       v
                     docker: risk_management -> execution
                       |
                       v
                     stream:fills             ── 17 entries
```

`run_paper.py` is the signal brain. The sleeve parameters, the risk engines and
the universe scoping all live there, and `docker` supplies risk enforcement,
execution and accounting downstream of it.

## The dormant path

```
  docker: signal_generation
      |  publishes -> stream:signals          ── XLEN 0, never written
      v
  docker: ml_model            "Up 8 days (healthy)"
      consumer group ml_model: last-delivered-id 0-0, pending 0, lag 0
      -> has read nothing, ever
      |
      v
  stream:recommendations      ── 192 entries, ALL from run_paper.py
```

Both services are running and both report healthy. Neither has done any work.

**"Healthy" here means the loop is turning, not that output is being
produced.** `services/ml_model/runner.py` calls `write_heartbeat()` once per
iteration of its main loop regardless of whether a message was consumed, and the
container healthcheck reads that heartbeat. A service that can never produce
output will report healthy indefinitely.

If `ml_model` ever did receive a signal, `_handle_signal` catches every
exception from `process_signals`, logs a warning and leaves the message
buffered and pending — deliberately, so a transient failure does not
dead-letter a valid signal. The consequence is that a permanent failure is also
only a warning.

## Which decision governs the dormant path

Deleting or wiring `signal_generation` / `ml_model` is the **D17 ML
architecture decision**, dated end of readiness tranche 3
(`docs/designs/project-direction.md`). It is deliberately not settled here.

What *was* settled (2026-09-04) is the model loader, which was broken
independently of that decision:

- `scripts/retrain_model.py` writes LightGBM's native `.txt` via
  `Booster.save_model` and records a `ModelVersion` row pointing at it;
- `ModelRegistry.load_active` did `joblib.load` unconditionally, and
  `_verify_integrity` refused any row without a `content_hash` — which the
  retrainer never set.

So every model the retrainer promoted was unloadable through the registry meant
to serve it. Both faults are fixed and pinned by a round-trip test using a real
LightGBM `Booster`; a stub would round-trip through joblib happily and prove
nothing about the formats.

## How to re-check these numbers

```bash
docker ps --format '{{.Names}}\t{{.Status}}' | grep algo-poc
docker exec -e REDISCLI_AUTH="$(security find-generic-password -s algo-poc \
  -a REDIS_PASSWORD -w)" algo-poc-redis-1 redis-cli --no-auth-warning \
  XINFO GROUPS stream:signals
```

Note the container name: `docker ps | grep redis` also matches
`crypto-poc-redis-1`, a different project on the same host.
