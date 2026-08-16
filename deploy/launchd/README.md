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

Two further accounts are **optional** (`$ALGO_OPTIONAL_SECRET_NAMES`):
`DEADMAN_WATCHDOG_URL` and `ALGO_DEADMAN_PAPER_URL`. `--import` prompts for
them and `--check` reports them, but their absence does not make `--check`
exit non-zero — that status means "the stack cannot authenticate", which is a
different problem from "no external check is watching this host".

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

`run_paper.sh` sources `deadman.sh` and pings `$ALGO_DEADMAN_PAPER_URL` **only
on a successful run**. Every alert the wrappers send is sent by this host,
about this host, so none of them can fire when the Mac is off — which is
exactly what "the 04:15 run never happened" looks like from the inside, and
what went unnoticed for two days on 2026-08-13/14. The ping is fire-and-forget
by construction and cannot fail the run; its outcome is written to the day's
log as `dead-man switch: …`.

Full setup, cadence guidance and the delivery drill:
[`docs/operations/dead-man-switches.md`](../../docs/operations/dead-man-switches.md).

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
  read exit 3 as OK).
- **Alerting (KAN-43):** exit 1, 2 and 3 each send **exactly one** Telegram
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
attempts), sends **one Telegram alert**, and waits for a human re-login. It
alerts again on recovery.

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
  recovery).
- **For live:** change `PORT=7497` to `7496` in the script.

> Note: when the Gateway is kickstarted, in-flight IB API sessions drop.
> `ib_insync` in the execution service reconnects automatically, but verify
> after any watchdog-triggered restart.

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
baseline stays current (it auto-picks the newest `output/backtest_multi_*.json`;
without refreshes the live equity dates never overlap the baseline and every
portfolio reads `NO_DATA`).

**Why Tuesday, not Monday:** IBKR's historical-data farm is routinely dead
from Saturday night until the US Monday open (observed 2026-07-05/06 — 26
consecutive failed probes across the weekend). By Tuesday 05:00 SGT the US
Monday session has closed and the farms are warm. The job also runs safely
alongside the 04:15/04:45 jobs (backtest uses IB clientId 10).

- **Telegram**: ✅ with the headline metrics on success, ❌ on failure or when
  the Gateway is unreachable (baseline going stale is a silent risk otherwise).
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
