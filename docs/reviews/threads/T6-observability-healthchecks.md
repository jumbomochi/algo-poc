# T6 — Observability & unattended healthchecks  [P1]

Part of the 2026-08-06 implementation review (`docs/operations/implementation-review-2026-08-06.md`, Theme 6 + § 9). Tracking issue linked via this PR's "Closes #…".

## Problem
A hung-but-alive service is invisible for up to ~24h: there are no app-level healthchecks, the metrics stack is not actually wired, and Redis streams grow unbounded.

## Checklist
- [ ] **Wire metrics** — call `setup_metrics()` / `start_http_server()` in each service `main`; verify Prometheus scrapes real targets. `observability.py:21,31`, `config/prometheus.yml`
- [ ] **Container healthchecks** — a liveness endpoint or heartbeat file so Docker restarts a deadlocked-but-alive process (the known stuck-modal class), which `restart: unless-stopped` alone misses. `docker-compose.yml`
- [ ] **Alert rules** — stream-idle / no-fills-in-N-min / dlq-depth / redis-memory.
- [ ] **Bound Redis** — set `maxmemory` + policy; cap streams with `XADD MAXLEN ~` or periodic `XTRIM` (streams currently grow forever → eventual OOM takes down the whole bus). `redis_client.py:31,126`

## Acceptance criteria
- A killed or wedged service pages within minutes, not at the next daily heartbeat.
- Money-critical metrics (orders, fills, kill, risk breaches) are scraped and dashboarded.
- Stream memory is bounded and alerted.

## Dependencies
- Shares the dlq-depth alert with **T4**.
