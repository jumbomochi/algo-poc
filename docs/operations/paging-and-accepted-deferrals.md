# Paging decision and accepted deferrals

Two things that until KAN-41 lived only in a settings file and in the
operator-local readiness design (2026-08-11, tranche 1 item 3 and
tranche 3 item 12): **how this system pages a human**, and
**which known gaps were deliberately left open, and why**. If you are debugging
a page that never arrived, or wondering whether a dead monitor is a bug, start
here.

State verified 2026-10-01 against `origin/main` `a3503fb` and the running host.

---

## 1. The pager is Telegram, on every layer

The design decision is "Telegram on both layers" — the application layer and
the Prometheus layer. A third, the launchd wrappers, sits outside both. All
three deliver to the same Telegram bot and chat.

| Layer | Source | Delivery path | Config |
|---|---|---|---|
| Application | Any service publishing to `stream:alerts` (kill, halt, DLQ, divergence BREACH, reconciliation, stop-loss…) | `services/notifications` → Telegram | `notifications.telegram_enabled`, `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` |
| Prometheus | `config/alert_rules.yml` (HeartbeatStale et al.) | Prometheus → Alertmanager `telegram_configs` → Telegram | `config/alertmanager.yml`, `docker-compose.observability.yml` |
| Host / wrapper | The launchd wrappers (`deploy/launchd/run_*.sh`) | `algo_alert_local` + Telegram directly, and healthchecks.io dead-man pings | `deploy/launchd/secrets.sh`, `dead-man-switches.md` |

**Why the Prometheus layer has its own route and does not go through the
notifications service:** HeartbeatStale exists to catch a *wedged
notifications service*. If its page were delivered by that service, the one
failure it is built to report would also silence it. The Alertmanager route is
the path that does not depend on the thing it watches (`config/alertmanager.yml:1-10`).

**Why Telegram and not a paging product:** a solo operator, one device, and a
system that trades once a day. A heavier stack (PagerDuty-style escalation,
acknowledgement, on-call rotation) is revisited **only if Telegram proves
insufficient in practice** — for example a page that was delivered but missed
because nothing escalated it.

### What actually fires today

Be precise about this, because the config reads as more complete than the
deployment is:

- **Application alerts fire.** The notifications container runs in the
  deployed stack (`docker ps`, 2026-10-01).
- **Wrapper alerts and dead-man switches fire.** They run under launchd, not
  Docker, and page even when the containers are down
  (`dead-man-switches.md`).
- **Prometheus alerts do not fire.** The observability overlay
  (`docker-compose.observability.yml`: Prometheus, Alertmanager, Grafana,
  redis-exporter) is **not running** on the host — no such container exists on
  2026-10-01. Every rule in `config/alert_rules.yml`, HeartbeatStale included,
  has no evaluator. The config is correct and tested (`amtool check-config` is a
  required CI check); it is not deployed. Until it is, the independent path for
  "the notifications service is wedged" is the external dead-man switches, not
  HeartbeatStale. See also `dlq-audit-2026-08.md` finding B.

Bringing the overlay up is the command in `CLAUDE.md` ("Start with
observability stack") plus the credentials listed there; the delivery drill is
in `dead-man-switches.md` §"Verification drill (KAN-15 AC8)".

---

## 2. Accepted deferrals

Four gaps named in the readiness design as **explicit deferrals, not silent
drops**. Each is a known dead or partial mechanism that someone reading the code
will find; each entry says why that is acceptable *now* and what changes the
answer.

### 2.1 Intraday marks for the stop-loss

**The gap.** No service writes intraday prices. The risk service's 30-minute
periodic check (`services/risk_management/runner.py` `run_periodic_risk_checks`,
`risk.passive_scan_interval_minutes: 30`) refreshes marks from the DB, which
holds daily closes — so it re-evaluates the same price all day. A stop breached
intraday is acted on after the next close is ingested.

**The planned supersession.** Broker-native GTC stops (D16; KAN-19 places them,
KAN-20 verifies them) move stop enforcement to IB, which triggers intraday
regardless of how fresh our marks are. With those live, intraday marks would
only improve trailing-stop adjustment freshness and passive-scan NAV accuracy.

**Current state — read this before relying on the supersession.**
`execution.broker_stops_enabled` is **`false`** (`config/default.yaml:114`),
parked with epoch v2 (KAN-33, held 2026-09-25). So today the supersession has
**not** happened: the protection in force is the software stop on daily-close
marks. The risk is a gap-and-run intraday move being acted on one session late.

**Why that is accepted today.** The account is paper, unlevered, long-only, and
the 30-minute scan plus the daily sleeve exit both act on the next close; the
design's rationale for deferring intraday marks rests on broker stops, and those
turn on together with epoch v2.

**What changes the answer.** Enabling `broker_stops_enabled` without first
running the broker-stop drill (`drill-runbook.md`), or trading real money
before it is enabled. Before live capital, either the flag is on or this
deferral is re-decided.

### 2.2 Margin data plumbing

**The gap.** Margin utilisation is hard-coded `0.0` at every construction site
(`services/risk_management/runner.py:176,197,845,1988`;
`scripts/run_paper.py:955`), so the margin-critical trim in
`passive_monitor.py:91` can never fire. Wiring it needs an IB account-value
feed into the risk service.

**Why accepted.** The account trades unlevered long-only, cash-funded. The
margin monitor guards a state the strategy cannot currently enter.

**What changes the answer.** Any leverage, shorting, or margin-account use —
including a strategy change that could open a short. The oversell guard
(KAN-10) is what stops an emergency sell from opening one today.

### 2.3 Reprice-with-quotes

**The gap.** The unfilled-order sweep does not reprice: execution has no live
quote feed, so `OrderManager.sweep_unfilled_orders` is called with an empty
quote map and unfilled limits age out and are cancelled, freeing their
reservations (`services/execution/runner.py` `maybe_run_unfilled_sweep`
docstring). `reprice_interval_minutes` / `max_reprice_attempts` therefore bound
the age-out, not a reprice ladder.

**Why accepted.** The failure mode is a **missed fill, not a loss**: an order
that does not fill is cancelled honestly and the reservation released.

**Not part of this deferral.** `OrderManager.handle_partial_fill` and
`execution.min_viable_fill_pct` still have no caller. That is dead code, not an
accepted gap — it is open as residual R3 in the findings register
(`implementation-review-2026-08-06.md` §12): wire it or delete it on the next
execution-runner change.

**What changes the answer.** Fill-rate evidence that missed fills are material
to a sleeve's returns, or a strategy that needs intraday execution.

### 2.4 Sweep calendar as a constructor parameter

**The gap.** The unfilled-order sweep no-ops unless `_market_calendar` is set,
and it is set by poking a private attribute from `__main__`
(`services/execution/runner.py:2078`). A runner constructed any other way — a
test, a future entrypoint — silently has no sweep.

**Why accepted.** Small, and the production entrypoint does set it. The
safety-critical sweep does **not** depend on it: KAN-13's post-halt reconcile
sweep runs on its own timer and is independent of `_market_calendar` by design
(`maybe_run_halt_sweep` docstring).

**What changes the answer.** Nothing needs to — fold it into the next change
that touches the execution runner's constructor.

---

## 3. Stale-documentation corrections

Claims this backlog found wrong, recorded so the next reader is not misled
the same way. The first two were already corrected in place; the third is corrected by KAN-41:

| Where | Was | Now | Corrected in |
|---|---|---|---|
| `TODOS.md` (mid-session reconciliation entry) | orderRef stamping (D12) listed as pending | Records that D12 has shipped: `ib_executor.py` sets `order.orderRef` on both submission paths and reads it back via `find_order_by_ref` / `restore_order_by_ref` | `627c345` (KAN-11) |
| `backtest-baseline.md` | Pointed at `scripts/fetch_fundamentals.py` for `SECTOR_MAP` | Points at `shared/universe.py`, noting `fetch_fundamentals.py` only re-exports it | `d86a6f6` (KAN-23) |
| `TODOS.md` ("Dual signal paths") | Listed the authoritative-path call as open, blocked on D17, with both services "up and healthy" | Marked resolved: KAN-35 (D17) demoted both services to offline tools; `run_paper.py` is the only recommendation source | KAN-41 |
