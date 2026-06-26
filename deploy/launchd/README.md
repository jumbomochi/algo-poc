# launchd deploy artifacts

Reference copies of the macOS launchd jobs that run algo-poc on the operator's
host. The **live** copies are deployed outside the repo:

| Repo copy | Deployed to |
|---|---|
| `run_divergence.sh` | `~/ibc/run_divergence.sh` (chmod +x) |
| `local.algo-divergence-monitor.plist` | `~/Library/LaunchAgents/local.algo-divergence-monitor.plist` |
| `gateway_watchdog.sh` | `~/ibc/gateway_watchdog.sh` (chmod +x) |
| `local.algo-gateway-watchdog.plist` | `~/Library/LaunchAgents/local.algo-gateway-watchdog.plist` |

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

## IB Gateway watchdog

Runs `gateway_watchdog.sh` every **5 minutes** (`StartInterval` 300s). Checks
the API port (7497 paper); after **two consecutive** failures (~10 min down) it
`launchctl kickstart -k`s the `local.ibc-gateway` job — the fix that cleared the
stuck "Unrecognized Username or Password" login modal on 2026-06-25. The
two-strike logic rides over the legitimate ~1-min nightly auto-restart (23:55)
and cold-restart (08:00) blips instead of fighting them.

- **Logs:** `~/ibc/logs/gateway_watchdog_YYYYMMDD.log` (only logs on state
  change / action, to stay quiet), launchd stdout/stderr to
  `~/ibc/logs/gateway-watchdog-launchd.log`.
- **State marker:** `~/ibc/.gateway_down_marker` (present = one strike pending).
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
