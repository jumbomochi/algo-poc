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

## Setup

Create two checks at an external provider (healthchecks.io, Better Uptime,
Cronitor — anything that pages on a missed ping) and store their ping URLs.

```bash
# Both go in the login keychain alongside the other secrets. They are
# OPTIONAL secrets: `secrets.sh --check` reports them, but their absence does
# not make it exit non-zero (that status means "the stack cannot
# authenticate", and an unconfigured dead-man switch is a different problem).
deploy/launchd/secrets.sh --import      # prompts for both at the end
deploy/launchd/secrets.sh --check       # confirm they resolve
```

Configure the external checks:

| Check | Period | Grace | Why |
|---|---|---|---|
| Watchdog | 5 min | ≥ 15 min | Alertmanager re-notifies every 5 min; the grace must survive one missed ping without a false page. |
| Paper run | 26 h | 2 h | `run_paper.sh` runs daily **including weekends** (it exits 0 on a non-trading day, having simply committed no signals), so a 26 h period pages after exactly one missed day. It is deliberately **not** gated on the NYSE calendar: a calendar bug would silence the switch, which is the one failure mode it must not have. |

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

`algo_deadman_ping` pings **only** when the paper run exits 0. A wrapper that
pinged unconditionally would report a crashed run to the external checker as a
healthy one — the 2026-08-13/14 silence, reproduced with extra steps. The
"ran and failed" case is covered by the wrapper's own Telegram and
`algo_alert_local` paths, which are host-local and therefore useless for the
"never ran" case. The two are complements, not alternatives.

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
