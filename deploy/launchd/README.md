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
