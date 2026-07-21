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

Both `run_paper.sh` and `run_divergence.sh` export
`ALGO_DATABASE_URL=postgresql://algo:algo@localhost:55432/algo_poc` — the
dockerized paper DB on its machine-local override port. The stock
`config/default.yaml` URL (localhost:5432) points at nothing on this host;
this was why every nightly paper run failed from April through July 2026.

These are tracked here so the wiring is version-controlled and survives a
machine rebuild. If you edit a deployed copy, sync it back here (and vice-versa).

## Daily divergence monitor

Runs `scripts/divergence_monitor.py` at **04:45 SGT, Tue–Sat** — ~30 min after
the 04:15 `local.algo-paper-trading` job has written that day's
`equity_snapshots` row. See [divergence-monitor.md](../../docs/operations/divergence-monitor.md).

- **Logs:** `~/ibc/logs/divergence_YYYYMMDD.log` (auto-pruned after 30 days),
  launchd stdout/stderr to `~/ibc/logs/divergence-launchd.log`.
- **Prometheus textfile:** `~/ibc/metrics/divergence.prom`. node_exporter is not
  installed yet — once it is, point its textfile collector at `~/ibc/metrics/`
  (or change `PROM_FILE` in the wrapper to the collector dir).
- **Exit codes:** 0 = OK/WARNING, 1 = BREACH (alert), 2 = hard error (page). The
  wrapper logs the appropriate level; real alert/page channels are stubbed until
  `notifications` are enabled in `config/default.yaml`.

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
action, and recovery. Credentials are read from the repo's gitignored `.env`
(`TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`).

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

- **Logs:** `~/ibc/logs/pipeline_report_YYYYMMDD.log` (pruned after 30 days),
  launchd stdout/stderr to `~/ibc/logs/pipeline-report-launchd.log`.
- **launchd scripts and PATH:** any job script that calls the `docker` CLI
  must export `PATH` including `/usr/local/bin` — launchd's default PATH
  does not have it (`run_db_backup.sh` and `run_pipeline_report.sh` both do
  this).
