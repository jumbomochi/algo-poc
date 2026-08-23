# Operations Runbook Index

Entry point for operational documentation.

## Runbooks

- `go-live-checklist.md` — Paper-to-live promotion gates
- `rollback-playbook.md` — Time-bound rollback procedure
- `container-deploy.md` — Putting merged code into the risk/execution containers: build, `--force-recreate`, image-hash proof, retag rollback, cold-reboot check (KAN-17)
- `divergence-monitor.md` — Daily live-vs-backtest divergence check (scripts/divergence_monitor.py)
- `backtest-baseline.md` — Regenerating the headline backtest: point-in-time universe, next-open fills, real costs
- `api-security.md` — TLS, secrets, auth lockout, dependency scanning, schema/model integrity (T9)
- `dependency-lockfile.md` — Regenerating the lockfiles, why each `uv pip compile` flag exists, upgrade procedure, version-ceiling policy (KAN-36)
- `drill-evidence-isolation.md` — The portfolio exclusion contract: which readers must exclude `__drill__`/`_`-prefixed portfolios, and how to run a tagged drill (KAN-24)
- `drill-runbook.md` — The two per-epoch drills, step by step: out-of-band halt (not a kill), the broker-stop breach that takes a real fill, unwind/reconcile, and what the drills deliberately do not prove (KAN-32)
- `broker-stop-prototype.md` — Broker-native GTC stop spike on the paper account: runbook, findings, go/no-go for KAN-19 (scripts/ops/broker_stop_spike.py)
- `dead-man-switches.md` — The two external checks that page when this host goes quiet, why "no trades today" is deliberately not an internal alert, and the delivery drill (KAN-15)
- `dlq-audit-2026-08.md` — 2026-08 audit of `stream:approved_orders:dlq`: the queue never existed, why the stop-loss dead-lettering was latent rather than active, and the two monitoring gaps left open (KAN-21)
- `incident-2026-08-21-gateway-and-docker.md` — 2026-08-21 incident: an IB login rejection at the 23:55 IBC auto-restart and a dead Docker engine, overlapping. Why the loudest alert (a 20-hour Error 1100) was false, why `Weekday` 2–6 is correct and not a Mon–Fri bug, and the three evidence gaps to date (KAN-62…KAN-67)
- Reconciliation procedures (TBD)
- Incident response/escalation procedures (TBD)

## Reviews & governance

- `investment-policy-statement.md` — IPS: risk limits (§ 6), deployment gates, retirement triggers
- `sleeve-kill-criteria.md` — Written demotion rules for the six live sleeves: the four triggers, per-sleeve drawdown budgets, the promotion funding rule, and a worked dry-run demotion review (KAN-37)
- `edge-validation-framework.md` — What gate S and gate P require beyond backtest fidelity: deflated Sharpe against a declared trial count, the pre-registered single-use holdout, parameter stability (KAN-38)
- `incumbent-edge-evaluation.md` — The framework applied to the six incumbent sleeves: n_trials = 8, the named load-bearing parameter per sleeve, what a failing verdict means, and the per-sleeve verdicts Rung 0 is blocked on (KAN-40)
- `implementation-review-2026-08-06.md` — 2026-08-06 implementation review + findings register; work threads **T1–T9** = issues #2–#10, draft PRs #12–#20

## Related strategy docs

- `../strategies/portfolio-2026-05.md` — Current active 6-sleeve portfolio configuration
- `../strategies/mean-reversion-failure-analysis.md` — Why mean_reversion + short_term_mr were dropped 2026-05-26
