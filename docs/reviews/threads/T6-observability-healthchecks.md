# T6 — Observability & unattended healthchecks  [P1]

Part of the 2026-08-06 implementation review (`docs/operations/implementation-review-2026-08-06.md`, Theme 6 + § 9). Tracking issue linked via this PR's "Closes #…".

## Problem
A hung-but-alive service is invisible for up to ~24h: there are no app-level healthchecks, the metrics stack is not actually wired, and Redis streams grow unbounded.

## Status (verified 2026-10-01, KAN-41)

Checked against `origin/main` `a3503fb`. A box is ticked only when shipped code and a test meet it; anything partial stays unticked with the residual named. Finding-level status and the residual register (R1–R7) are in `docs/operations/implementation-review-2026-08-06.md` §12.

## Checklist
- [ ] **Wire metrics** — call `setup_metrics()` / `start_http_server()` in each service `main`; verify Prometheus scrapes real targets. `observability.py:21,31`, `config/prometheus.yml`
  — *Partial:* wired in every service (`afeb89e`, `test_every_backend_service_calls_setup_metrics`), but nothing scrapes them — the observability overlay is not deployed (R7).
- [x] **Container healthchecks** — a liveness endpoint or heartbeat file so Docker restarts a deadlocked-but-alive process (the known stuck-modal class), which `restart: unless-stopped` alone misses. `docker-compose.yml`
  — *Done:* heartbeat-file healthchecks (`afeb89e`, `4c63979`). Docker marks a wedged container unhealthy but does not restart it; `gateway_watchdog.sh` alerts on it (KAN-66).
- [x] **Alert rules** — stream-idle / no-fills-in-N-min / dlq-depth / redis-memory.
  — *Written:* `config/alert_rules.yml`, retuned by KAN-15, routed by KAN-14. No evaluator in production (R7).
- [x] **Bound Redis** — set `maxmemory` + policy; cap streams with `XADD MAXLEN ~` or periodic `XTRIM` (streams currently grow forever → eventual OOM takes down the whole bus). `redis_client.py:31,126`

## Acceptance criteria
- [ ] A killed or wedged service pages within minutes, not at the next daily heartbeat. — *Not met via Prometheus* (R7). What fires today: the watchdog's unhealthy-stack alert, `stream:alerts` → Telegram, and the dead-man switches (KAN-65).
- [ ] Money-critical metrics (orders, fills, kill, risk breaches) are scraped and dashboarded. — Exported and a dashboard is committed; not scraped (R7).
- [ ] Stream memory is bounded and alerted. — *Half met:* bounded (`test_redis_service_sets_a_maxmemory_ceiling_with_noeviction`); the alert has no evaluator (R7).

## Dependencies
- Shares the dlq-depth alert with **T4**.
