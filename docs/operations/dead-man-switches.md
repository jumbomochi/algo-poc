# Dead-man switches (KAN-15 / P1-12)

Every other check in this repo runs on the machine it is monitoring:
Prometheus, Alertmanager, the container healthchecks, the launchd wrappers'
own Telegram alerts. All of them share one blind spot — **if the Mac is off,
asleep, or off the network, the thing that was supposed to shout is the thing
that is gone.** A monitor cannot report its own absence.

There is a second, subtler blind spot. This system trades **once a day**
(`deploy/launchd/run_paper.sh`, ~04:15 SGT). From inside, a day on which every
signal was a SKIP and a day on which the run never happened at all look
*identical*: no recommendations, no approved orders, no fills, every stream
flat. No PromQL over `redis_stream_*` can tell those apart — which is why
`config/alert_rules.yml` deliberately no longer tries.

Both gaps are closed the same way: something inside pings something outside on
a healthy beat, and **the outside pages when the pings stop**. Nothing here
has to detect anything. The absence of a message is the message.

| Switch | Pinged by | Cadence | Covers |
|---|---|---|---|
| `DEADMAN_WATCHDOG_URL` | Alertmanager, from the always-firing `Watchdog` rule | every 5 min | Prometheus / Alertmanager / docker / the host stopped |
| `ALGO_DEADMAN_PAPER_URL` | `deploy/launchd/run_paper.sh`, on a **successful** run only | once a day (~04:20 SGT) | the host is up, but the trading run did not happen or failed |
| `ALGO_DEADMAN_DIVERGENCE_URL` | `deploy/launchd/run_divergence.sh`, on a run that reached a **verdict** (exit 0/1/3/4, not 2) | once a day (~04:50 SGT, Tue–Sat) | the 04:45 drift check did not happen — as on 2026-08-13/14, which left a permanent hole in the gate evidence |
| `ALGO_DEADMAN_REFRESH_URL` | `deploy/launchd/run_backtest_refresh.sh`, on a **successful** run only | once a week (~Tue 05:00–11:00 SGT) | the weekly baseline refresh did not happen — as on 2026-08-11, when the host booted after the calendar slot and launchd did not re-fire it |
| `ALGO_DEADMAN_BACKUP_URL` | `deploy/launchd/run_db_backup.sh`, on a **verified** dump | once a day (~05:16 SGT) | the RPO ≤ 1 day promise quietly stopped being kept |
| `ALGO_DEADMAN_DIGEST_URL` | `scripts/ops/evidence_digest.py`, on a **delivered** digest | once a week (Mon ~08:00 SGT) | the weekly evidence digest was not sent |

Two jobs deliberately have no switch, and say so in their own headers:
`run_pipeline_report.sh` (its only output *is* a daily message, so a missed run
is a missing report) and `gateway_watchdog.sh` (a `StartInterval` job with no
slot to miss, whose failure surfaces as an unreachable Gateway in two jobs that
are covered). `tests/deploy/test_deadman_ping.py` fails if a wrapper has
neither a ping nor a stated reason.

## Setup

Create one check per switch at an external provider (healthchecks.io, Better
Uptime, Cronitor — anything that pages on a missed ping) and store their ping
URLs. One check per job, not one shared: an external checker pages on a
*missing* ping, so a shared URL would keep looking healthy for as long as any
single job kept running, which defeats the point of knowing which one stopped.

```bash
# They all go in the login keychain alongside the other secrets, as OPTIONAL
# secrets: `secrets.sh --check` reports them, but their absence does not make
# it exit non-zero (that status means "the stack cannot authenticate", and an
# unconfigured dead-man switch is a different problem).
deploy/launchd/secrets.sh --import      # prompts for each of them at the end
deploy/launchd/secrets.sh --check       # confirm they resolve
```

Configure the external checks:

| Check | Period | Grace | Why |
|---|---|---|---|
| Watchdog | 5 min | ≥ 15 min | Alertmanager re-notifies every 5 min; the grace must survive one missed ping without a false page. |
| Paper run | 26 h | 2 h | `run_paper.sh` runs daily **including weekends** (it exits 0 on a non-trading day, having simply committed no signals), so a 26 h period pages after exactly one missed day. It is deliberately **not** gated on the NYSE calendar: a calendar bug would silence the switch, which is the one failure mode it must not have. |
| Divergence | 26 h | 4 h | The job is Tue–Sat, so Sunday and Monday are legitimately quiet — set the check to skip them if the provider supports a cron schedule, otherwise use a 74 h period and accept the slower Monday signal. The 4 h grace covers the 5-minute DB port wait plus a slow cold boot. |
| Backtest refresh | 8 days | 12 h | Weekly (Tue 05:00 SGT), and the run itself can take hours against ~830 point-in-time tickers. 8 days pages after exactly one missed Tuesday; a generous grace keeps a merely slow run from paging. |
| DB backup | 26 h | 2 h | Daily at 05:15, same shape as the paper run. |
| Evidence digest | 8 days | 12 h | Weekly (Mon 08:00 SGT). |

The Alertmanager container reads `DEADMAN_WATCHDOG_URL` from the environment
(`.env`, or `eval "$(deploy/launchd/secrets.sh --export)"` before
`docker compose up`). If it is unset the container **still starts** — it warns
on stderr and repoints the `Watchdog` route at the `null` receiver. That is
deliberate: an unconfigured dead-man switch is a monitoring gap, but refusing
to start over it would take Telegram delivery down for every real alert too,
which is strictly worse.

The Watchdog must never reach Telegram. It fires permanently by design; a
permanently-firing alert in the chat trains the operator to mute the bot, and
a muted channel is less monitoring than one that was never wired up.

## Why a failed run must not ping

`algo_deadman_ping` pings **only** when it is handed exit code 0. A caller that
pinged unconditionally would report a crashed run to the external checker as a
healthy one — the 2026-08-13/14 silence, reproduced with extra steps. The
"ran and failed" case is covered by the wrapper's own Telegram and
`algo_alert_local` paths, which are host-local and therefore useless for the
"never ran" case. The two are complements, not alternatives.

**What counts as "healthy" is per-job, and is decided by the caller, not by
`deadman.sh`.** The helper only ever pings on a 0; each wrapper maps its own
outcome onto that, once, in one place:

- `run_backtest_refresh.sh` passes its exit code straight through, and routes
  *every* exit through a single `refresh_exit()` funnel so a future early-abort
  cannot become a healthy beat by omission.
- `run_divergence.sh` maps exits 0, 1, 3 and 4 to a healthy beat, because all
  four mean the monitor ran and reached a verdict — and a real BREACH lasts
  days, during which suppressing the ping would saturate the external check
  exactly when "did it run?" is the question you most need answered. Only
  exit 2, where nothing could be judged, stays silent.
- `run_db_backup.sh` pings only after `pg_restore --list` has read the archive
  back. A dump that exists but cannot be restored is not a backup.

The ping is also **incapable of failing the run**: every function in
`deploy/launchd/deadman.sh` returns 0, and the outcome is written to the day's
log as `dead-man switch: …` rather than branched on. Monitoring must never be
able to cause the outage it exists to detect.

Ping URLs are bearer capabilities — anyone holding one can forge a healthy
ping and switch the dead-man off permanently. They are never logged verbatim;
the log line carries scheme and host only.

## Verification drill (KAN-15 AC8) — operator, ~10 minutes

This is the acceptance criterion that cannot be automated: it proves the
delivery path end to end, including the part that does not depend on the
service being monitored. **Run it by hand; do not let an agent run it.**

Prerequisite: the observability overlay is up with real Telegram credentials.

```bash
docker compose -f docker-compose.yml -f docker-compose.observability.yml up -d
```

1. **Confirm the rules loaded and the Watchdog is firing.**
   `http://localhost:9091/alerts` → `Watchdog` FIRING, and nothing else.
   If any other rule is firing on an idle system, stop — that is the
   regression this story exists to prevent.

2. **Confirm the route.**
   `http://localhost:9093/#/alerts` → the Watchdog is present and its receiver
   is `deadman`, not `telegram`. Your external Watchdog check should be
   showing pings arriving every ~5 minutes.

3. **Wedge the notifications service.** This is the important one: the alert
   about a broken notifications service must not be delivered *through* the
   notifications service.

   ```bash
   docker compose kill -s SIGSTOP notifications     # freeze, do not stop
   ```

   Within ~4 minutes (`> 120s` plus `for: 2m`) `HeartbeatStale{job="notifications"}`
   should fire and **a Telegram message should arrive**, via Alertmanager,
   while the notifications service is frozen.

   ```bash
   docker compose kill -s SIGCONT notifications     # thaw
   ```

   Expect a `RESOLVED` message shortly after.

4. **Break the daily switch.** Temporarily point `ALGO_DEADMAN_PAPER_URL` at a
   check you can watch, let one scheduled run go by (or run the wrapper by
   hand), and confirm the external check shows the ping. Then pause the
   launchd job for longer than the grace period and confirm the external
   provider pages you. Re-enable it afterwards.

5. **Confirm the newer switches are configured at all.** `secrets.sh --check`
   lists every optional secret; a switch with no URL logs `NOT CONFIGURED` in
   the job's own log and pages nobody. That failure mode looks identical to a
   working switch until the day you need it.

   ```bash
   deploy/launchd/secrets.sh --check
   grep 'dead-man switch' ~/ibc/logs/divergence_$(date +%Y%m%d).log
   ```

Record the outcome (dates, what arrived, what did not) on
<https://huiliang.atlassian.net/browse/KAN-15>.

## What is intentionally *not* alerted internally

- **"No trades today."** Legitimate and common. Covered by the daily dead-man
  switch, which distinguishes "ran and had nothing to do" (pings) from "did
  not run" (does not ping). Do not add a rule for it — it would page on every
  quiet day and get the channel muted.
- **Container auto-restart on `unhealthy`.** `docker-compose.yml` documents
  the deliberate choice to make a wedge *visible* rather than silently
  restart it. That is why the alert path matters; changing it is a separate
  decision.

## See also

- `config/alert_rules.yml` — the rules, including the `Watchdog`
- `config/alertmanager.yml` — routing; the `deadman` and `null` receivers
- `deploy/launchd/deadman.sh` — the ping helper
- `tests/deploy/alert_rules_test.yml` — the `promtool` replay of a quiet
  weekend, a no-trade day, and every genuine failure mode
