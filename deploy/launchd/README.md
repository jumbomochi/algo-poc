# launchd deploy artifacts

Reference copies of the macOS launchd jobs that run algo-poc on the operator's
host. The **live** copies are deployed outside the repo:

| Repo copy | Deployed to |
|---|---|
| `run_divergence.sh` | `~/ibc/run_divergence.sh` (chmod +x) |
| `local.algo-divergence-monitor.plist` | `~/Library/LaunchAgents/local.algo-divergence-monitor.plist` |

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
