# Operations Runbook Index

Entry point for operational documentation.

## Runbooks

- `go-live-checklist.md` — Paper-to-live promotion gates
- `rollback-playbook.md` — Time-bound rollback procedure
- `divergence-monitor.md` — Daily live-vs-backtest divergence check (scripts/divergence_monitor.py)
- `backtest-baseline.md` — Regenerating the headline backtest: point-in-time universe, next-open fills, real costs
- `api-security.md` — TLS, secrets, auth lockout, dependency scanning, schema/model integrity (T9)
- `dependency-lockfile.md` — Regenerating the lockfiles, why each `uv pip compile` flag exists, upgrade procedure, version-ceiling policy (KAN-36)
- `broker-stop-prototype.md` — Broker-native GTC stop spike on the paper account: runbook, findings, go/no-go for KAN-19 (scripts/ops/broker_stop_spike.py)
- Reconciliation procedures (TBD)
- Incident response/escalation procedures (TBD)

## Reviews & governance

- `investment-policy-statement.md` — IPS: risk limits (§ 6), deployment gates, retirement triggers
- `implementation-review-2026-08-06.md` — 2026-08-06 implementation review + findings register; work threads **T1–T9** = issues #2–#10, draft PRs #12–#20

## Related strategy docs

- `../strategies/portfolio-2026-05.md` — Current active 6-sleeve portfolio configuration
- `../strategies/mean-reversion-failure-analysis.md` — Why mean_reversion + short_term_mr were dropped 2026-05-26
