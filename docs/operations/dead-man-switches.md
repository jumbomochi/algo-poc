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
| `ALGO_DEADMAN_PAPER_URL` | `deploy/launchd/run_paper.sh`, on a **successful** run only | once a day (~04:20 SGT, Tue–Sat) | the host is up, but the trading run did not happen or failed |
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

Set the provider's timezone to **Asia/Singapore** first — every cron below is
in SGT, because the launchd slots are.

| Check | Switch | Provider schedule | Grace | Why |
|---|---|---|---|---|
| Watchdog | `DEADMAN_WATCHDOG_URL` | `*/5 * * * *` | ≥ 15 min | Alertmanager re-notifies every 5 min; the grace must survive one missed ping without a false page. |
| Paper run | `ALGO_DEADMAN_PAPER_URL` | `15 4 * * 2-6` | 2 h | `local.algo-paper-trading.plist` is `Weekday` 2–6, i.e. **Tue–Sat SGT** — which is US Mon–Fri, because the 04:15 SGT run covers the session that closed at 04:00 SGT that morning. Sunday and Monday are legitimately quiet. A flat 26 h period instead of this cron pages every Sunday and stays red through Monday. |
| Divergence | `ALGO_DEADMAN_DIVERGENCE_URL` | `45 4 * * 2-6` | 4 h | Same Tue–Sat shape, 30 min after the paper run. The 4 h grace covers the 5-minute DB port wait plus a slow cold boot. |
| Backtest refresh | `ALGO_DEADMAN_REFRESH_URL` | `0 5 * * 2` | 12 h | Weekly (Tue 05:00 SGT), and the run itself can take hours against ~830 point-in-time tickers, so the grace is generous enough that a merely slow run does not page. |
| DB backup | `ALGO_DEADMAN_BACKUP_URL` | `15 5 * * *` | 2 h | Daily at 05:15 — this one really is every day, weekends included. |
| Evidence digest | `ALGO_DEADMAN_DIGEST_URL` | `0 8 * * 1` | 12 h | Weekly (Mon 08:00 SGT). Prefer the cron over a plain "8 days": an 8-day period lets a missed Monday stay invisible for over a week, which is the failure KAN-64 existed to close. |

The cron is the **expected check-in time**, and every wrapper waits on a port or
a container before it pings, so the ping lands a few minutes after the slot. The
grace absorbs that; do not shift the cron to compensate.

`tests/operations/test_dead_man_switches_note.py` reads the plists and fails if
the weekday set or the hour in this table stops matching them, so the table
cannot drift the way the "runs daily including weekends" claim did between
2026-08-21 and 2026-09-08.

### The baseline has its own check, and it judges the PIN

`ALGO_DEADMAN_REFRESH_URL` covers "the weekly refresh did not run", and since
2026-09-10 it is finally armed — it only pings on a *successful* refresh, so it
could not arm itself until one happened.

That leaves a different question, which is the one the daily report answers:
**is the baseline the monitor actually grades against still current and usable?**
The baseline of record is `divergence.baseline_pin` in `config/default.yaml`,
resolved through `scripts/ops/baseline_pin.py` — **not** the newest
`output/backtest_multi_*.json`, whatever happens to sort last.

That distinction is not pedantry. The first version of this check measured the
newest artifact, and on 2026-09-10 it went silent the moment a refresh
succeeded — while the pin stayed 23 days old and the artifact that silenced it
was `BLOCKED` at 17.39% excluded and could never be pinned.

The daily report prints both facts on one line, because either alone misleads:

```
pin is backtest_multi_20260819_183451.json (23d old, coverage BLOCKED at 11.28% excluded);
newest is backtest_multi_20260910_235624.json (0d, coverage BLOCKED at 17.39% excluded — cannot be pinned)
```

It escalates through `algo_alert_local` plus Telegram when:

| Condition | Why |
|---|---|
| the pin cannot be resolved | the monitor will exit 3 (BLIND) on its next run |
| the pin's file is missing | same, and a different fix from "it is old" |
| the pin is older than **30 days** | past the monitor's comparison window, live and backtest stop overlapping meaningfully — which it already hints at with *"Only 22 overlapping days available (requested 30)"* |
| the newest artifact is **unusable** and fresh (≤1 day) | a refresh that succeeds and produces something unpinnable is otherwise invisible: it exits 0, pings its dead-man, and logs the coverage warning to a file nobody opens |

A newer, **usable** artifact sitting unpinned is deliberately *not* an alert.
Re-pinning is a deliberate act (KAN-51), so that is the normal state after
every refresh.

The unusable case alerts only while it is news. Repeating it every morning until
a vendor exists for delisted history (KAN-59/KAN-60) is how an alert gets
ignored; after the first day it stays in the report body as state.

Owned by `deploy/launchd/lib/baseline_age.sh`; thresholds overridable with
`ALGO_BASELINE_PIN_MAX_DAYS` and `ALGO_BASELINE_FRESH_DAYS`.

### A check that has never been pinged does not alert

This is the trap that cost two of the six switches. Importing the URL arms
nothing on its own: at healthchecks.io (and every provider with the same model)
a check that has **never been pinged** stays in a *new* state, greyed out, with
its timer not yet started — it will sit there indefinitely without alerting.

On 2026-09-08, `algo-backtest-refresh` and `algo-watchdog` both read
`Last Ping: Never` for exactly this reason, so neither could have paged for
anything. Each switch needs **one successful ping** before it is live. After
importing a URL, confirm the provider shows a real check-in — a green check that
has received a ping, not merely a check that exists.

Two of them cannot get that first ping today without other work:

* `DEADMAN_WATCHDOG_URL` is pinged by Alertmanager, and the observability stack
  is not deployed on this host. Pause the check rather than leaving it grey, so
  the dashboard shows six switches you believe in rather than five plus a
  decoration.
* `ALGO_DEADMAN_REFRESH_URL` only pings on a **successful** weekly refresh, and
  the refresh has been failing. The switch is correct; the job is not.

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
