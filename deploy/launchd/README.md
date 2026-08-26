# launchd deploy artifacts

Reference copies of the macOS launchd jobs that run algo-poc on the operator's
host. The **live** copies are deployed outside the repo:

| Repo copy | Deployed to |
|---|---|
| `run_paper.sh` | `~/ibc/run_paper.sh` (chmod +x) — 04:15 SGT daily paper run |
| `run_divergence.sh` | `~/ibc/run_divergence.sh` (chmod +x) |
| `local.algo-divergence-monitor.plist` | `~/Library/LaunchAgents/local.algo-divergence-monitor.plist` |
| `gateway_watchdog.sh` | `~/ibc/gateway_watchdog.sh` (chmod +x) |
| `local.algo-gateway-watchdog.plist` | `~/Library/LaunchAgents/local.algo-gateway-watchdog.plist` |
| `run_backtest_refresh.sh` | `~/ibc/run_backtest_refresh.sh` (chmod +x) — Tue 05:00 SGT weekly baseline refresh |
| `local.algo-backtest-refresh.plist` | `~/Library/LaunchAgents/local.algo-backtest-refresh.plist` |
| `run_db_backup.sh` | `~/ibc/run_db_backup.sh` (chmod +x) — 05:15 SGT daily paper-DB pg_dump (RPO ≤ 1 day) |
| `local.algo-db-backup.plist` | `~/Library/LaunchAgents/local.algo-db-backup.plist` |
| `run_pipeline_report.sh` | `~/ibc/run_pipeline_report.sh` (chmod +x) — 04:52 SGT Tue–Sat pipeline report + Telegram heartbeat |
| `local.algo-pipeline-report.plist` | `~/Library/LaunchAgents/local.algo-pipeline-report.plist` |

`run_paper.sh` and `run_divergence.sh` export
`ALGO_DATABASE_URL=postgresql://algo:<pw>@localhost:55432/algo_poc` (plus the
authenticated redis URL) — the dockerized paper DB/redis on their machine-local
override ports. The stock `config/default.yaml` URLs (localhost:5432 / 6379)
point at nothing on this host and carry no auth; this was why every nightly
paper run failed from April through July 2026, and why a **stale deployed copy**
still using the old `algo:algo` creds broke the 2026-08-11 cold boot.

These are tracked here so the wiring is version-controlled and survives a
machine rebuild. **The repo copy is canonical** — deploy with `deploy.sh`
(below); do not hand-edit the deployed `~/ibc` copies (hand-editing is what
drifted).

## Secrets: the macOS login keychain

Every wrapper gets its credentials from **`secrets.sh`**, sourced by path from
this directory. The store of record is the macOS login keychain:

```
service = algo-poc          (override with $ALGO_KEYCHAIN_SERVICE)
account = the variable name (POSTGRES_PASSWORD, REDIS_PASSWORD,
                             TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, API_KEYS)
```

```bash
deploy/launchd/secrets.sh --check              # presence only, never values
deploy/launchd/secrets.sh --import             # interactive; value never hits argv
deploy/launchd/secrets.sh --import-from-env F  # bulk from a plaintext env file
eval "$(deploy/launchd/secrets.sh --export)"   # for docker compose / a shell
```

`secrets.sh` is deliberately **not** deployed to `~/ibc` — it is sourced from
the repo so there is exactly one copy of the lookup logic and it cannot drift
the way the hand-copied wrappers did. `deadman.sh` (below) and
`lib/telegram.sh` (next section) are excluded for the same reason.

The dead-man ping URLs are **optional** accounts
(`$ALGO_OPTIONAL_SECRET_NAMES`): `DEADMAN_WATCHDOG_URL`,
`ALGO_DEADMAN_PAPER_URL`, `ALGO_DEADMAN_DIVERGENCE_URL`,
`ALGO_DEADMAN_REFRESH_URL`, `ALGO_DEADMAN_BACKUP_URL` and
`ALGO_DEADMAN_DIGEST_URL`. `--import` prompts for them and `--check` reports
them, but their absence does not make `--check` exit non-zero — that status
means "the stack cannot authenticate", which is a different problem from "no
external check is watching this host".

## Sending alerts: `lib/telegram.sh`

Every wrapper sends Telegram alerts through the single `telegram()` defined in
**`lib/telegram.sh`**, sourced by path from the repo right after `secrets.sh`:

```bash
ALGO_JOB_LABEL="divergence monitor"
. "$ALGO_DIR/deploy/launchd/lib/telegram.sh"
telegram "🚨 something happened"
```

Until KAN-43 each of the six wrappers carried its own hand-copied copy, and
they had already drifted: four raised `algo_alert_local` when no credential
resolved and two did not, so on a locked keychain those two failed silently —
the exact hole KAN-16 was opened to close. One copy means a fix (a timeout, a
retry) lands everywhere at once.

It lives under `lib/` so `deploy.sh`'s `"$SRC"/*.sh` glob cannot reach it: like
`secrets.sh` and `deadman.sh` it is **never** deployed to `~/ibc`, because a
copy there would be a decoy an operator could edit with no effect.

`telegram()` always returns 0 — a job's own verdict outranks the delivery of
its notification, and no wrapper's exit code may depend on Telegram.

**After changing any wrapper, run `deploy/launchd/deploy.sh`** to resync
`~/ibc`. No plist changed for KAN-43, so no `launchctl` reload is needed. To
confirm delivery end-to-end afterwards, use
`python scripts/ops/send_test_alert.py`.

## Dead-man switches: `deadman.sh`

Every alert the wrappers send is sent by this host, about this host, so none of
them can fire when the Mac is off, asleep, or was booted after the job's slot —
which is exactly what "the run never happened" looks like from the inside. That
went unnoticed for two days on 2026-08-13/14 (the paper run) and for three
weeks to 2026-08-18 (the weekly refresh). A wrapper sources `deadman.sh`, pings
its own external check on a healthy run, and the **checker** pages when the
ping stops arriving. The ping is fire-and-forget by construction and cannot
fail the run; its outcome is written to the day's log as `dead-man switch: …`.

**Coverage — every scheduled job either pings or says why not.** This is a
policy, not a per-job judgement call, and `tests/deploy/test_deadman_ping.py`
enforces it: a new wrapper with neither reddens the suite.

| Wrapper | Check | Pings when |
|---|---|---|
| `run_paper.sh` | `ALGO_DEADMAN_PAPER_URL` | exit 0 |
| `run_divergence.sh` | `ALGO_DEADMAN_DIVERGENCE_URL` | the run reached a **verdict** — exits 0, 1, 3, 4. Not exit 2 (nothing was judged). Withholding the ping for a real breach would saturate the check for the whole episode, exactly when telling "did not run" from "ran and found something" matters most. |
| `run_backtest_refresh.sh` | `ALGO_DEADMAN_REFRESH_URL` | exit 0 only. Every abort path (missing snapshot, gateway down, timeout, failed backtest) routes through `refresh_exit()`, so a new early exit cannot become a healthy beat by omission. |
| `run_db_backup.sh` | `ALGO_DEADMAN_BACKUP_URL` | a verified, readable dump exists |
| `run_evidence_digest.sh` | `ALGO_DEADMAN_DIGEST_URL` | the digest was **delivered** (pinged from `scripts/ops/evidence_digest.py`, which is the only thing that knows the send succeeded) |
| `run_pipeline_report.sh` | — | **is** a dead-man: its whole output is a daily message, so a missed run shows up as a missing report. The jobs it reports on carry their own checks. |
| `gateway_watchdog.sh` | — | `StartInterval`, so it has no slot to miss; a dead watchdog surfaces as an unreachable Gateway in the paper run and the refresh, both of which alert and both of which ping. The host-wide case belongs to `DEADMAN_WATCHDOG_URL`. |

Suggested periods: ~26 h for the daily checks, ~8 days for the weekly refresh
(one missed Tuesday pages; a late-finishing run does not).

Full setup, cadence guidance and the delivery drill:
[`docs/operations/dead-man-switches.md`](../../docs/operations/dead-man-switches.md).

### Missed calendar slots — what launchd does, and what covers it

**launchd does not re-fire a `StartCalendarInterval` job whose slot passed
while the host was down.** It runs the job at the next matching time. For the
weekly refresh that means a full week; on 2026-08-11 the Mac booted at 07:59,
two hours after the 05:00 slot, and the Tuesday refresh simply did not happen
until 08-18 — silently, because a script that never starts cannot alert.

(`StartInterval` jobs behave differently: launchd starts them shortly after
boot, so `gateway_watchdog.sh` self-heals across a downtime.)

**The policy is to accept this and rely on the dead-man switches**, rather than
adding a catch-up guard. A catch-up run is worse than the gap it closes: the
refresh holds IB's historical-data pacing budget for up to six hours, so a job
that fires at an arbitrary post-boot time could still be running into the next
04:15 paper run and starve it of data. The failure the catch-up would prevent
is one late baseline; the failure it would introduce is a missed trading day.

What covers it instead, in order of how fast it speaks:

1. The job's **dead-man check** pages once its period lapses — for the refresh,
   ~8 days after the last successful Tuesday.
2. The **divergence monitor's staleness check** (exit 4) fires on the first
   daily run after the baseline passes `--max-baseline-age-days` (default 14,
   i.e. two missed refreshes). This one is independent of *which* job failed:
   it reads the age of the artifact actually being scored against, so it also
   catches causes nobody has enumerated.

Neither is instant, and that is the accepted trade: the baseline going a week
stale is a nuisance, and both signals arrive well before it becomes a risk.

### Why not a plaintext `.env`, and why not 1Password

On **2026-08-12 10:51** 1Password Environments replaced the repo's `.env` with a
named pipe it serves from the desktop app. The wrappers read credentials with
`grep '^POSTGRES_PASSWORD=' .env`; against an app-backed FIFO that nothing is
serving, that open **blocks ~60s and then returns nothing**:

| Job | Starts | `.env` reads | Aborted at | Delay |
|---|---|---|---|---|
| `run_paper.sh` | 04:15:00 | 2 | 04:17:01 | ~121s |
| `run_divergence.sh` | 04:45:00 | 1 | 04:46:02 | ~62s |

Both died before doing any work on 2026-08-13 and 2026-08-14. Nobody was told,
because `gateway_watchdog.sh` gated its Telegram alerting on
`[ -f "$ENV_FILE" ]` — **false for a FIFO** — so the alert path short-circuited
to *success*. With the operator at the keyboard and 1Password unlocked the pipe
serves instantly, so every hand-test passed.

A 1Password **service-account token** was considered and rejected: launchd
cannot do interactive auth, so the token would itself be plaintext on disk, and
unlike these loopback-only Postgres/Redis passwords it is a *network* credential
usable from any machine — a wider blast radius for no gain. (Its `op://`
references also cannot address a 1Password *Environment*; they only resolve
against classic vault items.)

A keychain item is encrypted at rest, grants nothing off this box, and is
readable non-interactively by a *user* LaunchAgent: verified that
`security find-generic-password` returns the value with no controlling TTY,
closed stdin and a stripped environment. The login keychain is `no-timeout` on
this host, so screen lock and sleep do **not** relock it.

**The one constraint:** a launchd *user agent* needs a logged-in GUI session.
After a reboot with nobody logging in the keychain stays locked — but Docker
Desktop and IB Gateway would be down too, so this adds no new requirement. That
case is reported as `LOCKED`, distinct from a missing secret, because the
operator action differs.

`.env` is still accepted as a fallback **only when it is a regular file**. If it
exists and is not one, every wrapper now fails in under a second with a named
error instead of hanging — see
`tests/deploy/test_launchd_secrets_keychain.py`, whose FIFO test blocks and
times out if that guard is ever removed.

## Deploying / syncing

`deploy/launchd/deploy.sh` is the one command that pushes these wrappers +
plists to their live locations. It replaces the manual per-file `cp`:

```bash
deploy/launchd/deploy.sh --dry-run   # show what would change, write nothing
deploy/launchd/deploy.sh             # copy changed *.sh -> ~/ibc, *.plist -> ~/Library/LaunchAgents
```

It skips unchanged files, prints a diff of each change, and — for any plist it
touched — prints the `launchctl bootout/bootstrap` reload commands for you to
run (launchctl is a human step, CLAUDE.md). Each wrapper also self-checks at
startup and logs a loud `WARNING - … differs from repo canonical` line if it
was launched from a drifted copy, so drift surfaces the same morning instead of
failing silently at 04:15.

### A copied plist is not a loaded job (KAN-64)

`deploy.sh` copies plists and **prints** the bootstrap commands; it must never
run them, because `launchctl bootout/bootstrap` is reserved for a human
(CLAUDE.md, and `test_launchd_deploy_hardening.py` enforces it). That leaves a
gap between "tracked" and "running" that nothing checked:
`local.algo-evidence-digest.plist` sat in `~/Library/LaunchAgents` from
2026-08-17 and **never fired once**, because nobody ran the printed commands.
Both existing guards stayed green the whole time — the plist really was tracked,
and `deploy.sh` really did refuse to run launchctl. Two Monday digests were
missed, and the 2026-08-18 evidence gap went unnoticed for three days as a
direct result: the digest is what should have surfaced it.

`lib/launchd_wiring.sh` reconciles the three sets — plists in the repo, plists
installed in `~/Library/LaunchAgents`, and labels present in `launchctl list` —
and reports both directions:

- **installed but NOT LOADED** — launchd will never run it. This is
  alert-worthy, not a log line, because the failure mode is silence by
  construction.
- **loaded but not in `deploy/launchd/`** — the job-level equivalent of the
  per-wrapper `cmp -s "$0" "$CANON"` drift guard, which only ever covered
  scripts.

It is called from two places. `deploy.sh` calls it so its reload hint names the
labels that are *actually* outstanding rather than only the ones whose file
happened to change — it still only ever reads `launchctl list`, and still only
prints bootout/bootstrap. And the **04:52 pipeline report** calls it every day,
because the check has to live in a job that is verifiably running: it cannot
live in pytest (CI has no launchd, and a test that shelled out to `launchctl`
would fail there or be skipped — the same blind spot in a new costume), and it
obviously cannot live in the evidence digest, which is the job that was not
loaded.

Scope is `local.algo-*`. `local.ibc-gateway` is deliberately excluded: its plist
belongs to IBC rather than this repo, and its failure mode is not silent — an
unloaded Gateway job means port 7497 goes unreachable, which the watchdog, the
04:15 run and the Tuesday refresh all already alert on.

## Daily divergence monitor

Runs `scripts/divergence_monitor.py` at **04:45 SGT, Tue–Sat** — ~30 min after
the 04:15 `local.algo-paper-trading` job has written that day's
`equity_snapshots` row. See [divergence-monitor.md](../../docs/operations/divergence-monitor.md).

- **Logs:** `~/ibc/logs/divergence_YYYYMMDD.log` (auto-pruned after 30 days),
  launchd stdout/stderr to `~/ibc/logs/divergence-launchd.log`.
- **Prometheus textfile:** `~/ibc/metrics/divergence.prom`. node_exporter is not
  installed yet — once it is, point its textfile collector at `~/ibc/metrics/`
  (or change `PROM_FILE` in the wrapper to the collector dir).
- **Exit codes:** 0 = OK/WARNING, 1 = BREACH (alert), 2 = hard error (page),
  3 = baseline not comparable → the monitor is **BLIND** (every report forced to
  `NO_DATA`; regenerate the baseline per
  [backtest-baseline.md](../../docs/operations/backtest-baseline.md) — do NOT
  read exit 3 as OK), 4 = baseline **STALE** (KAN-56): the verdicts are real,
  but the artifact they were scored against is older than
  `--max-baseline-age-days` (default 14 — two missed weekly refreshes). Read it
  as "the refresh is broken", not as "divergence is bad"; the fix is upstream,
  in `run_backtest_refresh.sh`. Age is taken from the artifact's filename
  stamp, falling back to its mtime, so restoring or copying `output/` cannot
  make a stale baseline look fresh.
- **Alerting (KAN-43):** exit 1, 2, 3 and 4 each send **exactly one** Telegram
  message; exit 0 sends nothing, so a healthy day never trains you to ignore
  the channel. The body is rendered by
  [`scripts/ops/divergence_alert.py`](../../scripts/ops/divergence_alert.py)
  from the monitor's own JSON report (`output/divergence_YYYYMMDD.json`), so a
  BREACH names the breaching sleeves and their divergence, and a BLIND run
  names which like-for-like requirement the baseline failed. If rendering
  fails the wrapper falls back to a generic message — never to silence. A
  failed send never changes the wrapper's exit code. Bodies are capped below
  Telegram's 4096-char limit, and any password in a logged DSN is redacted
  before it leaves the host.
- **Attributing artifacts to the run:** the wrapper stamps `RUN_STARTED` and
  the log's byte offset just before invoking the monitor, and passes both to
  the renderer. A report older than the run start is treated as absent — exit 1
  does not prove a breach (the monitor ends in `sys.exit(main())`, so an
  uncaught exception exits 1 too), and an earlier same-day run's report must
  never be narrated as this one's.
- **Test-only env hooks:** `ALGO_DIR`, `ALGO_PYTHON` and
  `ALGO_DIVERGENCE_REPORT` exist so the test suite can drive the wrapper
  end-to-end against stubs. launchd starts jobs with an empty environment, so
  production always takes the defaults. **Never export them in a login shell** —
  a manual run would then use whatever tree, interpreter, or report path they
  point at, and `ALGO_PYTHON` swaps the interpreter for the monitor too.

### Install / reload

```bash
cp deploy/launchd/run_divergence.sh ~/ibc/run_divergence.sh
chmod +x ~/ibc/run_divergence.sh
cp deploy/launchd/local.algo-divergence-monitor.plist \
   ~/Library/LaunchAgents/local.algo-divergence-monitor.plist

# (re)load
launchctl bootout   gui/$(id -u)/local.algo-divergence-monitor 2>/dev/null
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.algo-divergence-monitor.plist

# verify (status 0, no PID = loaded and idle until schedule)
launchctl list | grep local.algo-divergence-monitor

# run once now to test
~/ibc/run_divergence.sh; echo "exit $?"
```

### Uninstall

```bash
launchctl bootout gui/$(id -u)/local.algo-divergence-monitor
rm ~/Library/LaunchAgents/local.algo-divergence-monitor.plist
```

## IB Gateway watchdog (hardened 2026-07-05)

Runs `gateway_watchdog.sh` every **5 minutes** (`StartInterval` 300s). Checks
the API port (7497 paper); after **two consecutive** failures (~10 min down) it
`launchctl kickstart -k`s the `local.ibc-gateway` job. The two-strike logic
rides over the legitimate ~1-min nightly auto-restart (23:55) and weekly
cold-restart blips instead of fighting them.

**Auth-failure refusal:** before any kickstart the newest IBC gateway log is
checked for `Unrecognized Username or Password` / `Too many failed login
attempts`. If present, the watchdog **refuses to restart** (restarting loops
failed logins into an IB rate-limit — the 2026-07-01 incident: 30 rejected
attempts), alerts, and waits for a human re-login. It alerts again on recovery.

**Escalating re-alert while the login is rejected (KAN-62).** The refusal is
right; the flat 12h re-alert around it was not. On 2026-08-20 the auto-restart's
re-login was rejected at 23:55, the watchdog alerted once at 23:59:56, and the
next message was not due until roughly noon the following day — so the
operator's last warning arrived **4h16m before the 04:15 run**, which aborted at
04:25 and cost a session of gate evidence. The same sequence had already cost
2026-08-18. The interval now tightens as the run approaches:

| Time until the 04:15 paper run | Re-alert every |
|---|---|
| more than 6h | 12h |
| 6h–3h | 1h |
| 3h–1h | 30 min |
| under 1h | 15 min |

The 15-minute floor is what makes the guarantee hold: the job runs every 300s,
so inside the final hour an alert is always either newer than 15 minutes or
re-sent — **a warning always lands within 60 minutes of the run**. The auth
branch also no longer runs `rm -f "$MARKER"`: that marker is the *kickstart*
path's two-strike counter and this branch does not own it. Clearing it meant
that when the auth condition cleared on 2026-08-21 at 08:19 the watchdog
restarted counting from zero, adding a whole extra cycle of downtime.

### `AutoRestartTime` — why it is 2:00 PM, not 11:55 PM

`~/ibc/config.ini:52` is **`AutoRestartTime=2:00 PM`** (SGT). This is the other
half of KAN-62 and it is a host config file, not a repo file, so it is recorded
here: a future edit that moves it back inside the job window is the failure
being guarded against, and
`tests/deploy/test_gateway_watchdog.py::test_auto_restart_time_is_outside_the_scheduled_job_window`
pins it.

The daily chain is 04:15 (paper run), 04:45 (divergence), 04:52 (pipeline
report), 05:15 (DB backup), all SGT. At 23:55 a rejected re-login had 4h20m to
be noticed by a human who was asleep, and no automated path at all — the
watchdog is forbidden from kickstarting into an auth failure, and
`ColdRestartTime=08:00` is **weekly, not daily** (the 08-21 session logged "cold
restarted at 2026/08/23 08:00", a Sunday), so on a weekday there is no automatic
backstop. 2:00 PM SGT puts a failed re-login in the middle of the operator's
working day and roughly **14 hours** ahead of the next run that depends on it,
and it is clear of both the job window and IB's own overnight reset.

**Telegram alerts** (best-effort) are sent on: auth-failure refusal, kickstart
action, and recovery. Credentials come from the login keychain
(`TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`) via `secrets.sh`. A credential that
cannot be read is now **logged and raised locally**, never silently skipped —
the old `[ -f "$ENV_FILE" ] || return 0` guard turned a dead alert path into a
success and hid the 2026-08-13/14 outage for two days.

When there is no token to send with (locked keychain), `algo_alert_local` still
fires: it appends to `~/ibc/logs/ALERTS.log` — one persistent file, not a
per-day log a failed run never creates — and raises a desktop notification. It
needs no credential, so the "nothing can alert" hole is closed.

- **Logs:** `~/ibc/logs/gateway_watchdog_YYYYMMDD.log` (only logs on state
  change / action, to stay quiet), launchd stdout/stderr to
  `~/ibc/logs/gateway-watchdog-launchd.log`.
- **State markers:** `~/ibc/.gateway_down_marker` (one strike pending),
  `~/ibc/.gateway_auth_failure_alerted` (alert already sent; cleared on
  recovery), `~/ibc/.gateway_connectivity_alerted` (Error 1100 alert sent),
  `~/ibc/.docker_stack_alerted` (stack-liveness alert sent).
- **For live:** change `PORT=7497` to `7496` in the script.

> Note: when the Gateway is kickstarted, in-flight IB API sessions drop.
> `ib_insync` in the execution service reconnects automatically, but verify
> after any watchdog-triggered restart.

### Error 1100: a latch is trusted only as far as its writer (KAN-63)

`~/ibc/state/gateway_connectivity_lost` is written by the **execution service**
on IB Error 1100 (the API port stays open during a 1100, so the port check is
blind to it) and can only be removed by the execution service, on a 1101/1102.
Execution runs in a container — so the clearer and the outage share a failure
domain. When the docker engine died on 2026-08-20 the latch froze, and the
watchdog spent **20h07m re-alerting a growing outage that had already ended**,
across a Gateway cold restart that fixed everything. The 08:25 alert claimed
"~1207 min" while a direct probe of 7497 found IB entirely healthy.

Three guards, all in the watchdog:

1. **The latch cannot outlive its Gateway session.** Line 1 of the marker is
   still the bare loss epoch (any older reader still parses it with `head -1`);
   the watchdog appends `gateway_pid` / `gateway_started_at` on first
   observation, because only the host can see them. If the running Gateway no
   longer matches, the latch is dropped, the drop is logged, no 1100 alert is
   sent, and any outstanding alert gets its all-clear. For an unstamped or
   legacy marker the backstop is the same question asked directly: a Gateway
   that *started after* the loss was recorded cannot be in that outage.
2. **A latch whose writer is down is not evidence.** Before alerting on a
   sustained 1100 the watchdog asks whether the `execution` service is actually
   running. If it is not, the alert says so — which is both true and actionable
   — instead of quoting a duration nothing is maintaining.
3. **The reported duration is bounded.** Past 24h the message says the number is
   a floor rather than a measurement, because an unbounded, ever-growing figure
   in an alert is itself the signal that nothing is measuring it.

### Docker engine + stack liveness (KAN-66)

The same 300s cycle now checks that the container runtime everything else
depends on is actually alive. On 2026-08-20 the docker **engine** died while
Docker Desktop's Electron GUI stayed up: `docker ps` failed, every container was
gone, the socket file was still there (dated ten days earlier), and the app
looked healthy. Three jobs failed the next morning, each alerting accurately
about its own symptom — port 55432, a missing postgres container, a 1100 latch —
and none naming the cause.

- The check is **`docker info` succeeding**, never a process-name match: the
  Docker Desktop processes were present and the daemon was not.
- `docker info` alone is not enough. After the restart, 10 of 11 containers came
  up and `portfolio-accounting` was left crash-looping (KAN-61), which a
  daemon-only check calls healthy. The expected compose services are compared
  against what is running, and anything not running or not healthy is **named**.
- **It never remediates.** A dead engine needed process kills and an app
  relaunch; that is not something to automate against a live trading host on a
  five-minute timer. `lib/docker_health.sh` contains no restart, kill or open
  invocation and a test asserts it stays that way.
- One alert, 12h re-alert while unresolved, and a recovery alert when the daemon
  and every service return — the same discipline as the other watchdog alerts.
- `run_paper.sh` and `run_divergence.sh` both wait on 55432 and both abort
  correctly, but they named the *port*. When the daemon itself is unreachable
  they now say so: "the docker daemon is NOT RESPONDING … the whole stack is
  down, not just this port" is actionable in a way that "55432 not reachable
  after 300s" is not.

### Install / reload

```bash
cp deploy/launchd/gateway_watchdog.sh ~/ibc/gateway_watchdog.sh
chmod +x ~/ibc/gateway_watchdog.sh
cp deploy/launchd/local.algo-gateway-watchdog.plist \
   ~/Library/LaunchAgents/local.algo-gateway-watchdog.plist
launchctl bootout   gui/$(id -u)/local.algo-gateway-watchdog 2>/dev/null
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.algo-gateway-watchdog.plist
launchctl list | grep local.algo-gateway-watchdog
```

## Weekly backtest refresh

Runs `run_backtest_refresh.sh` every **Tuesday 05:00 SGT** — full 10yr
backtest so the [divergence monitor](../../docs/operations/divergence-monitor.md)
baseline stays current (the monitor scores against the artifact named by
`divergence.baseline_pin`, not the newest one — re-pinning is deliberate, see
docs/operations/backtest-baseline.md;
without refreshes the live equity dates never overlap the baseline and every
portfolio reads `NO_DATA`).

**Why Tuesday, not Monday:** IBKR's historical-data farm is routinely dead
from Saturday night until the US Monday open (observed 2026-07-05/06 — 26
consecutive failed probes across the weekend). By Tuesday 05:00 SGT the US
Monday session has closed and the farms are warm. The job also runs safely
alongside the 04:15/04:45 jobs (backtest uses IB clientId 10).

- **Telegram**: ✅ with the headline metrics on success, ❌ on failure or when
  the Gateway is unreachable (baseline going stale is a silent risk otherwise).
- **Dead-man**: pings `$ALGO_DEADMAN_REFRESH_URL` on success only. A missed
  Tuesday — the host down at 05:00, which is not re-fired; see [Missed calendar
  slots](#missed-calendar-slots--what-launchd-does-and-what-covers-it) —
  produces no alert from this job at all, so the external check is the only
  thing that can report it.
- **If it does go stale anyway:** the divergence monitor exits 4 and Telegrams
  once the baseline passes `--max-baseline-age-days` (default 14). Do not
  silence that by re-running the refresh by hand without
  `--universe-snapshots`; see KAN-23.
- **Logs:** `~/ibc/logs/backtest_refresh_YYYYMMDD.log` (pruned after 90 days).
- **Pruning:** baseline JSONs older than 90 days are deleted (~64 MB each;
  only the newest is ever used).

## Daily paper-DB backup

Runs `run_db_backup.sh` at **05:15 SGT every day** — a `pg_dump` (custom
format) of the dockerized `algo_poc` DB, taken after the 04:15 paper run and
04:45 divergence monitor so each dump contains that day's rows. RPO ≤ 1 day.
Added after the 2026-07-10 incident where an agent wiped the paper book via
`run_paper.py --reset` with nothing to restore from.

- **Dumps:** `~/ibc/backups/algo_poc_<YYYYmmdd_HHMMSS>.dump`, pruned after 30
  days. Every dump is verified with `pg_restore --list` before success.
- **Storage:** local-only; the job does not access iCloud Drive or other
  offsite storage.
- **Telegram**: ❌ on any failure (container down, dump error, unreadable
  archive). Success is logged only.
- **Logs:** `~/ibc/logs/db_backup_YYYYMMDD.log` (pruned after 30 days).
- **Restore runbook:** [backups.md](../../docs/operations/backups.md).

## Daily pipeline report

Runs `run_pipeline_report.sh` at **04:52 SGT, Tue–Sat** — after the 04:15
paper run and 04:45 divergence monitor. One log per day with the whole
pipeline's state: paper-run tail, risk-gate BUY/SELL/SKIP counts, divergence
result, execution-service activity (last 2h), resting IB orders (clientId 54),
and the last 7 days of equity snapshots.

Sends a one-line **Telegram summary every run** — deliberately a positive
heartbeat, not failure-only: the 2026-07-07 incident showed a single missed
alert can silently cost paper-record days, so *no morning message = something
is wrong*. Replaces the ad-hoc scratchpad watcher that did not survive
reboots.

The message leads with the facts that come from the database rather than from
log text (KAN-30), because a grep count can report a healthy number of orders
while the pipeline never received one, and a halt leaves no line in the paper
log at all:

```
halt: clear · fills:2 · rejected: risk 1 / broker 0 | ✅ paper run OK | divergence: Divergence monitor OK | 2 resting orders | today's snapshot ✓
🛑 HALT (circuit_breaker): drawdown -6.2% over 3 days · fills:0 · rejected: risk 0 / broker 0 | ...
```

An active halt leads the line, so it cannot be missed at the end of one.
Fills are `execution_fills` rows and rejections are `order_intents` in
`RISK_REJECTED` / `SUBMISSION_FAILED`, both counted since local midnight and
rendered by `scripts/ops/pipeline_report_summary.py`. If that read fails the
segment becomes `⚠️ halt/fills/rejections UNKNOWN (DB read failed)` and the
message is still sent — a reassuring `halt: clear` the job cannot substantiate
would be worse than admitting it does not know. The BUY/SELL/SKIP greps remain
in the log body for diagnosis.

- **Logs:** `~/ibc/logs/pipeline_report_YYYYMMDD.log` (pruned after 30 days),
  launchd stdout/stderr to `~/ibc/logs/pipeline-report-launchd.log`.
- **launchd scripts and PATH:** any job script that calls the `docker` CLI
  must export `PATH` including `/usr/local/bin` — launchd's default PATH
  does not have it (`run_db_backup.sh` and `run_pipeline_report.sh` both do
  this).
