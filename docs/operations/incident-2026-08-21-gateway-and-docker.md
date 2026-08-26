# Incident — 2026-08-21: two overlapping faults, one lost session

**Issues:** [KAN-62](https://huiliang.atlassian.net/browse/KAN-62) · [KAN-63](https://huiliang.atlassian.net/browse/KAN-63) · [KAN-64](https://huiliang.atlassian.net/browse/KAN-64) · [KAN-65](https://huiliang.atlassian.net/browse/KAN-65) · [KAN-66](https://huiliang.atlassian.net/browse/KAN-66) · [KAN-67](https://huiliang.atlassian.net/browse/KAN-67)
**Investigated:** 2026-08-21 00:25–03:30 UTC (08:25–11:30 SGT) · **Recovered:** same day
**Impact:** the 04:15 paper run, the 04:45 divergence monitor, and the 05:15 DB backup all failed. The 2026-08-21 evidence row was recovered by a catch-up run at 11:20 SGT.

**Verdict: two independent faults overlapped and were initially read as one.** IB
rejected the Gateway's login at the nightly IBC auto-restart, closing port 7497
through the whole run window. Separately, Docker's engine died while Docker
Desktop's GUI stayed alive, taking Postgres and every service container with it.
Neither of the two alerts that reached the operator named either cause, and the
loudest alert of the three — a 20-hour Error 1100 — was false.

All timestamps below are **SGT (+08)**, matching the log files. The IBC gateway
log, the wrapper logs, and `ALERTS.log` are all stamped in local time.

---

## The two faults

| | Fault A | Fault B |
|---|---|---|
| What | IB rejected the Gateway login at the 23:55 IBC auto-restart | Docker's engine died with the Electron GUI still alive |
| Began | 2026-08-20 23:55:09 | between 2026-08-20 13:38 and 2026-08-21 04:45 |
| Killed | the 04:15 paper run | the 04:45 divergence monitor, the 05:15 backup, all containers |
| Recovered | 08:21 08:20:15, by a human dismissing a dialog | 08:21 11:10, by force-killing and relaunching Docker |
| Ticket | [KAN-62](https://huiliang.atlassian.net/browse/KAN-62) | [KAN-66](https://huiliang.atlassian.net/browse/KAN-66) |

They share no mechanism. They are in one document because they happened on one
night and the alerting made them look related.

---

## Fault A — the login rejection

### Timeline

From `~/ibc/logs/ibc-3.23.0_GATEWAY-10.43_Saturday.txt` (IBC rotates by weekday
name; the session that began Saturday 2026-08-15 ran for six days):

```
08-20 12:17:29  IBC: Re-login to session / Click button: Re-login
08-20 12:17:30  detected dialog entitled: Unrecognized Username or Password    (line 1526)
                  -> execution writes ~/ibc/state/gateway_connectivity_lost
08-20 23:55:00  detected dialog entitled: Restart in progress                  (AutoRestartTime=11:55 PM)
                IBC returned exit status 0
                autorestart file found -> "authentication will not be required"
08-20 23:55:07  Login dialog WINDOW_OPENED: LoginState is LOGGED_OUT
08-20 23:55:09  detected dialog entitled: Unrecognized Username or Password    (line 1726)
08-20 23:59:56  watchdog: AUTH FAILURE — refusing to kickstart; alerted operator

   --- 8h 24m. Zero log lines between 00:00 and 08:19. Port 7497 closed. ---

08-21 04:15:04  run_paper.sh: waiting for IB Gateway on 127.0.0.1:7497 ...
08-21 04:25:11  ERROR - IB Gateway not reachable on 127.0.0.1:7497 after 600s
08-21 08:19:28  Unrecognized Username or Password; event=Activated / Focused
08-21 08:19:29  ...event=Closed                                 <- a human dismissed it
08-21 08:19:54  IBC: Cold restart in progress
                IBC: Login has not completed: exiting immediately
08-21 08:20:02  IBC: Login attempt: 1
08-21 08:20:15  IBC: Login has completed
08-21 08:25:12  watchdog: port 7497 recovered
```

Verify the gap is real, not a grep artifact:

```bash
grep -cE "^2026-08-21 0[0-7]:" ~/ibc/logs/ibc-3.23.0_GATEWAY-10.43_Saturday.txt
# -> 0
```

### The credentials are fine

This is the part that misleads. The cold restart at 08:19:54 authenticated on
`Login attempt: 1` using the same credentials, from the same args, in the same
config. What fails is the **autorestart re-login path specifically** — the session
comes back `LOGGED_OUT` despite the autorestart token asserting that
"authentication will not be required", and the fallback username/password login is
then rejected.

Why IB rejects it is not established here. It needs IB support or packet-level
evidence, and it does not block the scheduling fix.

### Not the first time

The identical sequence ran two nights earlier:

```
08-17 23:55:09  Unrecognized Username or Password; event=Opened     (line 945)
08-18 07:03:01  ...event=Activated / Focused / Closed               (lines 946-950)
08-18 07:03:41  watchdog: port 7497 recovered
```

`~/ibc/logs/ALERTS.log` in full, at the time of investigation:

```
2026-08-18 04:25:11: paper run aborted — IB Gateway never came up on 7497
2026-08-21 04:25:11: paper run aborted — IB Gateway never came up on 7497
2026-08-21 04:50:03: divergence monitor aborted — paper DB never came up on 55432
```

Two occurrences, four days apart, same failure, same abort second. Both recoveries
were a human closing a modal dialog: 07:03:01 on 08-18, 08:19:29 on 08-21. **This
failure mode has no automatic recovery.**

### Why the watchdog did not fix it

`deploy/launchd/gateway_watchdog.sh` runs every 300s
(`local.algo-gateway-watchdog.plist`, `StartInterval 300`), so roughly 100 checks
ran during the 8h24m outage. Every one took the auth-failure branch at line 110
and did nothing:

- **Lines 110–111** grep the newest IBC log for `Unrecognized Username or
  Password` and refuse to kickstart. **This is correct.** It is the guard added
  after the 2026-07-01 incident put 30 rejected logins into IB and risked a
  lockout.
- **Line 114** sets `REALERT_SECS` to 12h. After the 23:59:56 alert the next
  Telegram was not due until roughly 11:59 the following day. The operator got
  exactly one message, 4h16m before the run, then silence.
- **Line 128** ran `rm -f "$MARKER"` on every pass through the auth branch —
  clearing the two-strike counter that belongs to the *kickstart* path. So when
  the auth condition finally cleared at 08:19, the watchdog restarted counting
  from zero: it logged `port 7497 down (1st check)` at 08:20:09, adding a cycle of
  downtime on top of the outage it had just waited out.

The refusal is right. The **scheduling around it** is what made the refusal
expensive: `AutoRestartTime=11:55 PM` (`~/ibc/config.ini:52`) sat 4h20m before the
04:15 run, so a failed re-login landed in the window where nobody is awake and
nothing is allowed to act.

`ColdRestartTime=08:00` (`~/ibc/config.ini:56`) is **weekly, not daily** — the
08-21 session logged "Gateway will be cold restarted at 2026/08/23 08:00", a
Sunday. On a weekday there is no automatic cold-restart backstop at all.

> **Resolved 2026-08-26 — [KAN-62](https://huiliang.atlassian.net/browse/KAN-62).**
> The two paragraphs above are now historical and describe the code as it stood
> during this incident. Three things changed:
>
> - `AutoRestartTime` is **`2:00 PM`**, recorded with its reasoning in
>   `deploy/launchd/README.md` and pinned by
>   `tests/deploy/test_gateway_watchdog.py::test_auto_restart_time_is_outside_the_scheduled_job_window`,
>   so a future edit cannot silently move it back into the overnight window.
>   A rejected re-login now surfaces in the middle of the working day with ~14h
>   before the run that depends on it.
> - The flat 12h re-alert is gone. While the auth condition holds the interval
>   tightens as 04:15 approaches — 12h / 1h / 30m / 15m — with a 15-minute floor
>   inside the final hour, so a warning always lands within 60 minutes of the
>   run rather than 4h16m before it.
> - The auth branch no longer touches `$MARKER`. The extra grace pass described
>   above cannot recur.

---

## Fault B — the Docker engine died, the GUI did not

Observed at 08:25, before any remediation:

```bash
docker ps
# -> Cannot connect to the Docker daemon at unix:///Users/huiliang/.docker/run/docker.sock.

pgrep -lf "com.docker"
# -> 827 /Library/PrivilegedHelperTools/com.docker.vmnetd      (only this)
```

| Process | State |
|---|---|
| `Docker Desktop` (Electron, PID 19847, up 10 days) | RUNNING |
| `Docker Desktop Helper` ×3 (GPU, network, renderer) | RUNNING |
| `com.docker.vmnetd` (privileged helper) | RUNNING |
| `com.docker.backend` | **ABSENT** |
| `com.docker.virtualization` | **ABSENT** |
| `vpnkit` | **ABSENT** |
| `dockerd` | **ABSENT** |

The socket file still existed (dated 11 Aug 08:07). **Any check based on "is
Docker Desktop running" reports healthy in this state.** That is the whole reason
[KAN-66](https://huiliang.atlassian.net/browse/KAN-66) specifies `docker info`
succeeding rather than a process-name match.

`docker desktop restart` could not fix it either:

```
✗ Failed to stop Docker Desktop
stopping Docker Desktop: processes still running: ... Helper (GPU) (19896)
  ... Helper (Renderer) (19948) ... Helper (19902) ... Docker Desktop (19847): context canceled
```

Recovery required killing those four PIDs and `open -a Docker`. The daemon came
back in 10s and auto-restarted 13 containers.

### What it broke

```
04:45  divergence monitor  ERROR - paper DB (docker compose up?) not reachable
                           on 127.0.0.1:55432 after 300s
05:15  db backup           ERROR - postgres container algo-poc-postgres-1 is not running
                           dead-man switch: not pinged (run exited 1)
all day  every service container down, including execution
```

Last verified-good dump: `algo_poc_20260820_051501.dump` (96K, 05:15 on 08-20).
The RPO clock ran roughly 27 hours.

### Window, and what is not known

**Known:** the engine was alive at 2026-08-20 13:38 —
`algo-poc-portfolio-accounting-1` was created then, which requires a working
daemon — and dead by 2026-08-21 04:45, per the divergence probe. Roughly a
15-hour window.

**Not known: why it died.** Nothing in
`~/Library/Containers/com.docker.docker/Data/log/host/docker-desktop.log` or the
electron logs records the exit. The host did not sleep: `uptime` showed 10 days
and `coreaudiod` held `PreventUserIdleSystemSleep` continuously for 240 hours.
Load average was 4.15 across 56 shell sessions, which is a plausible
resource-pressure story with **no evidence behind it**. Do not repeat it as a
cause.

> **Detection closed 2026-08-26 — [KAN-66](https://huiliang.atlassian.net/browse/KAN-66).**
> The cause is still unknown and this story did not try to find it; what changed
> is that a recurrence is now visible enough to investigate. The gateway
> watchdog's 300s cycle checks `docker info` — succeeding, never a process-name
> match, because the Docker Desktop processes above were *present* while the
> daemon was gone — and compares the expected compose services against what is
> actually running, so the "10 of 11 came up and portfolio-accounting is
> crash-looping" state is named rather than called healthy. One alert, 12h
> re-alert, recovery alert, and **no automatic remediation**: a dead engine
> needed process kills and an app relaunch, which is not safe to automate
> against a live trading host on a five-minute timer.
>
> The two wrappers that wait on 55432 also stopped naming only the port. When
> the daemon itself is unreachable they now say so, which is the difference
> between the operator reading "postgres did not come up" and reading "the whole
> stack is down".

---

## The interaction: a false 20-hour Error 1100

The alert that shouted loudest was the wrong one.

```
08-21 08:25:15  watchdog: IB Error 1100 sustained 72465s (port 7497 up)
                  — alerting; NOT restarting
```

Telegram rendered that as "~1207 min". It was false. A read-only probe of port
7497 at 08:30, before any remediation, showed IB fully healthy:

```
connected: True   serverVersion: 178
accounts: ['<paper account>']
accountSummary rows: 96
errors seen: [(2104, 'Market data farm connection is OK:usfarm'),
              (2158, 'Sec-def data farm connection is OK:secdefil'),
              (2106, 'HMDS data farm connection is OK:ushmds')]
historical bars: 2      2026-08-19  316.83
                        2026-08-20  311.30
```

### Why the latch lied

`gateway_watchdog.sh:39`:

```bash
CONN_MARKER="$HOME/ibc/state/gateway_connectivity_lost"
```

The file holds the epoch second of the 1100. Lines 79–97 compute
`AGE = now - LOST_AT` and alert past `CONN_SUSTAINED_SECS` (180), re-alerting every
12h. **Nothing in the watchdog removes the marker.** Per the contract at lines
37–38, removal is the execution service's job, on a 1101/1102.

The execution service runs in a container. So:

1. Execution saw a 1100 at 08-20 12:17:30 and wrote the marker.
2. Docker's engine died. Execution was gone.
3. **The only process that can clear the latch died inside the outage that set
   it.**
4. The Gateway was then cold-restarted and logged in successfully — the 1100
   belonged to a session that no longer existed — and the watchdog kept measuring
   its age against a wall clock, unbounded.

Confirmed by the fix path: bringing Docker back made execution clear the marker
itself, with no manual intervention.

```
08-21 11:10:19  watchdog: IB connectivity restored (Error 1100 cleared)
```

The generalisable lesson: **a latch whose only clearer shares a failure domain
with the fault it describes will report that fault forever.**

> **Resolved 2026-08-26 — [KAN-63](https://huiliang.atlassian.net/browse/KAN-63).**
> The watchdog no longer trusts the latch beyond the things that maintain it.
> Line 1 of the marker is still the bare loss epoch, so every existing reader is
> unaffected; the watchdog appends `gateway_pid` / `gateway_started_at` on first
> observation, being the only party that can see them. A latch whose recorded
> identity does not match the running Gateway is dropped, logged as stale, and
> not alerted on — and any outstanding alert gets its all-clear. For an
> unstamped or legacy marker the same question is asked directly: a Gateway that
> *started after* the loss was recorded cannot be in that outage. Before
> alerting on a sustained 1100 the watchdog also checks that the execution
> service is running, and reports **that** when it is not, because "the
> execution service is down" is both true and actionable where a duration
> nothing is maintaining is neither. Past 24h the reported figure is stated as a
> floor rather than a measurement. On the 08-21 timeline, guard one alone
> suppresses the 08:25 page; guard two replaces it with the correct one.

---

## Evidence damage

`equity_snapshots`, 7 portfolios per day, one row each:

| Date | | Status | Total equity |
|---|---|---|---|
| 2026-08-11 | Tue | present | 203545.94 |
| 2026-08-12 | Wed | present | 203133.46 |
| 2026-08-13 | Thu | **MISSING** | 1Password FIFO outage ([KAN-16](https://huiliang.atlassian.net/browse/KAN-16)) |
| 2026-08-14 | Fri | present | 203650.44 (manual catch-up, 10:35) |
| 2026-08-15 | Sat | present | 202979.78 |
| 2026-08-18 | Tue | **MISSING** | this login rejection, never backfilled |
| 2026-08-19 | Wed | present | 202432.50 |
| 2026-08-20 | Thu | present | 202164.02 |
| 2026-08-21 | Fri | present | 201501.36 (catch-up, 11:20) |

Three gaps total: **08-13, 08-18, 08-21**. 08-21 was closed on the day. 08-13 is
already accepted as permanent. **08-18 had been open for three days and nobody
knew.**

### Weekday 2–6 is correct — do not "fix" it

An earlier pass of this investigation flagged the missing Mondays (08-10, 08-17)
as gaps too, and called five missing days. **That was wrong**, and it is an easy
mistake to repeat.

`local.algo-paper-trading.plist` and `local.algo-divergence-monitor.plist` use
`StartCalendarInterval` with `Weekday` 2, 3, 4, 5, 6 — **Tuesday through
Saturday** SGT, not Monday through Friday. This is correct by design: the 04:15
SGT run covers the US session that closed at 04:00 SGT *that morning*, so Tue–Sat
SGT maps exactly onto US Mon–Fri. Mondays having no run and Saturdays having one
(the 08-15 row above) are both right. Anyone counting missing Mondays as evidence
gaps will over-report by two days per fortnight.

### Why 08-18 went unnoticed for three days

The abort was reported — it is in `ALERTS.log` and Telegram fired. What never
happened was follow-up, because both mechanisms designed to catch it are inert:

- **The weekly evidence digest has never run.**
  `local.algo-evidence-digest.plist` was copied into `~/Library/LaunchAgents` on
  2026-08-17 00:17 and never bootstrapped. It is absent from `launchctl list`
  while the other seven `local.algo-*` jobs are present.
  ([KAN-64](https://huiliang.atlassian.net/browse/KAN-64) — **closed
  2026-08-26**: the digest is bootstrapped and all eight jobs are loaded, and
  the 04:52 pipeline report now reconciles the plists in
  `~/Library/LaunchAgents` against `launchctl list` every morning and *alerts*
  on any that is installed but not loaded. `deploy.sh` prints the outstanding
  bootstrap commands by name. It still never runs `launchctl` itself — that
  stays a human step — it just stops the omission being silent.)
- **No dead-man switch is armed.** All six URLs declared at
  `deploy/launchd/secrets.sh:106` are absent from the login keychain, so nothing
  outside this host can report an *absent* run. Existence check, values never
  printed:

  ```bash
  for n in DEADMAN_WATCHDOG_URL ALGO_DEADMAN_PAPER_URL ALGO_DEADMAN_DIVERGENCE_URL \
           ALGO_DEADMAN_REFRESH_URL ALGO_DEADMAN_BACKUP_URL ALGO_DEADMAN_DIGEST_URL; do
    security find-generic-password -s algo-poc -a "$n" >/dev/null 2>&1 \
      && echo "PRESENT: $n" || echo "MISSING: $n"
  done
  # -> MISSING × 6
  # control: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, POSTGRES_PASSWORD all PRESENT
  ```

  ([KAN-65](https://huiliang.atlassian.net/browse/KAN-65), and see
  `dead-man-switches.md`)

So the gap was found by hand-querying `equity_snapshots` during an unrelated
incident. That is the real finding: **the evidence record can be wrong for days
and still look fine.**

### 08-18 cannot be backfilled

`scripts/run_paper.py` has no `--as-of` or `--date` flag (full CLI at `_parser`,
`run_paper.py:1217-1284`). It always runs against the present: fetches bars ending
now and stamps `equity_snapshots.date` with the current date. `equity_snapshots`
also carries a unique index `ix_equity_portfolio_date` on `(portfolio, date)`, so
a run today cannot produce a row dated 08-18.

IB still serves the data — the probe above returned daily bars well past 08-17 —
so the obstacle is the runner, not the market data. The choice between accepting
the gap and building dated replay is
[KAN-67](https://huiliang.atlassian.net/browse/KAN-67).

---

## Recovery, in order

1. **Read-only IB probe** on port 7497 with a spare `clientId`, before touching
   anything. Established that the 1100 was stale and that market data for the
   catch-up was available. No orders placed.
2. **Force-restarted Docker**: killed PIDs 19847/19896/19902/19948, `open -a
   Docker`. Daemon up in 10s, 13 containers auto-restarted. No volumes touched,
   no `-v`, no rebuild.
3. Execution came up healthy and **cleared the 1100 latch itself**; the watchdog
   sent its own all-clear at 11:10:19.
4. **Catch-up paper run** via `launchctl kickstart -k
   gui/$(id -u)/local.algo-paper-trading` — deliberately through launchd so it was
   byte-identical to the scheduled run. Completed 11:20:13, exit 0, 140 tickers,
   1 signal (SELL CSCO, trailing stop). Recovered the 08-21 evidence row.

---

## What worked

Worth recording, because the 2026-08-13 incident is the counter-example:

- **Every alert path fired correctly.** No `WARNING - cannot send alert` line
  appears in any log, so the keychain lookup and Telegram delivery both worked on
  all three alerts. The [KAN-16](https://huiliang.atlassian.net/browse/KAN-16)
  fix for the 1Password FIFO hole held.
- **The wrappers aborted cleanly** rather than half-running. `run_paper.sh` waited
  its full 600s on 7497 and exited 1; `run_divergence.sh` waited 300s on 55432 and
  exited 1. No partial state was written.
- **The watchdog's refusal to kickstart on an auth failure held**, which is what
  kept 100 checks from becoming 100 rejected logins.

The failure here was not detection. It was that no alert named a cause, and the
loudest one was false.

---

## Standing defects opened

| Ticket | Defect | Status |
|---|---|---|
| [KAN-62](https://huiliang.atlassian.net/browse/KAN-62) | The 23:55 auto-restart sits 4h20m before the run; a rejected re-login eats the window | **Closed 2026-08-26** |
| [KAN-63](https://huiliang.atlassian.net/browse/KAN-63) | The Error 1100 latch outlives the outage; its only clearer dies with the containers | **Closed 2026-08-26** |
| [KAN-64](https://huiliang.atlassian.net/browse/KAN-64) | A tracked, copied plist is not a loaded job; the evidence digest has never run | **Closed 2026-08-26** |
| [KAN-65](https://huiliang.atlassian.net/browse/KAN-65) | All six dead-man switches unarmed; no external observer exists | Open |
| [KAN-66](https://huiliang.atlassian.net/browse/KAN-66) | Nothing watches the Docker engine | **Closed 2026-08-26** |
| [KAN-67](https://huiliang.atlassian.net/browse/KAN-67) | 2026-08-18 is unrecorded and cannot be backfilled | Open |

Pre-existing, re-verified during this investigation and still live:
[KAN-61](https://huiliang.atlassian.net/browse/KAN-61) —
`portfolio-accounting` crash-looping on `trades.recommendation_id` being
`varchar(50)`. Measured against `order_intents`: max id length 60, and **122 of
131 rows exceed 50 characters**. `trades` is still 0 rows. Note the restart
counter resets across a Docker restart, so the "769 restarts" figure in that
ticket will not reproduce — use `count(*) FROM trades`, `XLEN
stream:fills:dlq`, and `State.Status` instead.

---

## Related

- `dead-man-switches.md` — the external checks that are supposed to page when this
  host goes quiet ([KAN-15](https://huiliang.atlassian.net/browse/KAN-15))
- `container-deploy.md` — bringing containers up after a merge, including
  `--force-recreate`
- `backups.md` — the daily paper-DB dump that failed here
- `rollback-playbook.md` — time-bound rollback procedure
- `../superpowers/specs/2026-08-01-gateway-watchdog-error-1100-design.md` — the
  original watchdog design, including the Error 1100 handling this incident found
  a hole in
