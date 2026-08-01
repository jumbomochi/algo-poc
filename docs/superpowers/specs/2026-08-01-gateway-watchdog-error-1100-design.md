# Gateway watchdog: detect a sustained Error 1100

**Date:** 2026-08-01
**Status:** Approved, pending implementation

## Problem

The IB Gateway watchdog (`deploy/launchd/gateway_watchdog.sh`, deployed to
`~/ibc/gateway_watchdog.sh`, run every 300 s by `local.algo-gateway-watchdog`)
recovers two failure modes today:

1. **Port down** (Gateway dead/stuck) → two-strike `launchctl kickstart`.
2. **Auth rejection** in the IBC log → refuse to restart, alert every 12 h.

It is blind to a third: **Error 1100 — "connectivity between IB and the
Gateway has been lost."** During a 1100 the Gateway process stays alive and the
API port (7497) stays **open**, so `nc -z` reads "up" and the watchdog does
nothing while no market data flows and no orders route.

1100 cannot be grepped. It is an API-level event delivered over the socket to
connected clients — it is not in the IBC wrapper log (GUI events only), and the
Gateway's own logs are encrypted (`.ibgzenc`). The only components that observe
a 1100 are the always-on `ib_insync` clients, chiefly the `execution` service.

## Decisions (locked with the user)

- **Detection mechanism:** the app observes the API event and writes a marker
  file; the watchdog reads it. (Not an active per-cycle probe.)
- **Action on a sustained 1100:** **alert only, no kickstart.** 1100 is usually
  transient and auto-recovers (1102) within seconds to minutes; restarting the
  Gateway would disrupt a session about to self-heal and risk re-entering the
  login flow.

## Design

### Component 1 — Observer (`services/execution/ib_executor.py`)

`IBExecutor` holds the most persistent IB session, so it is the observer.

- Subscribe to `ib.errorEvent` on every `connect()` (ib_insync recreates the
  `IB` instance per connect, so the handler must be re-attached each time).
- Handler `_on_ib_error(reqId, errorCode, errorString, contract)`:
  - **1100** → write the connectivity-lost marker, contents = the epoch seconds
    at which connectivity was lost.
  - **1101 / 1102** (connectivity restored — data lost / data maintained) →
    delete the marker.
- On a **successful (re)connect** (`managedAccounts()` returned) → also delete
  the marker. A healthy session proves connectivity and clears any stale marker
  left by a socket that dropped without emitting a 1102.
- The handler is **best-effort**: all marker I/O is wrapped so a failure is
  logged and swallowed — it must never disturb order routing. The handler is
  synchronous (ib_insync events are sync), and marker I/O is a tiny local write.

Marker path inside the container comes from `ALGO_GATEWAY_STATE_DIR`
(default `/var/algo/state`), file name `gateway_connectivity_lost`.

### Component 2 — Transport (bind mount, `docker-compose.yml`)

`execution` has no volume today. Add one dedicated state dir so the container
hands the marker to the host watchdog:

```yaml
  execution:
    volumes:
      - ${ALGO_GATEWAY_STATE_DIR_HOST:-${HOME}/ibc/state}:/var/algo/state
    environment:
      - ALGO_GATEWAY_STATE_DIR=/var/algo/state
```

Host `~/ibc/state/` keeps the marker under the same tree as the watchdog's
other markers, but out of IBC's credential/backup/log internals. The host dir
is created by the watchdog (`mkdir -p`) and, defensively, by the app before
writing.

### Component 3 — Watchdog reads the marker (`gateway_watchdog.sh`)

Inside the **existing port-UP branch only**, before it exits:

- `CONN_MARKER="$HOME/ibc/state/gateway_connectivity_lost"`,
  `CONN_ALERT_MARKER="$HOME/ibc/.gateway_connectivity_alerted"`,
  `CONN_SUSTAINED_SECS=180`.
- If `CONN_MARKER` exists and its epoch is **≥ 180 s old** (sustained past one
  ~300 s poll cycle): Telegram-alert, then **re-alert every 12 h** using
  `CONN_ALERT_MARKER` (same cadence/idiom as the auth path). **No kickstart.**
- If `CONN_MARKER` is absent and `CONN_ALERT_MARKER` exists (we had alerted):
  send a "✅ connectivity restored" message and remove `CONN_ALERT_MARKER`.
- When the **port is down**, the existing port-down path owns recovery; the
  connectivity marker is not consulted and is cleared by the app's next healthy
  reconnect.

The 180 s threshold guards against alerting on a marker that is only seconds
old; with 300 s polling, any 1100 that survives one poll cycle is alerted
(≈5–10 min), while a 1100 that self-heals (1102) before the next tick removes
the marker and is never alerted.

## Out of scope

- Farm-status codes (2103 / 2105 / 2107 / 2108).
- Kickstart-on-1100.
- Observer in `data_ingestion` — `execution`'s persistent session is
  sufficient (1100 is broadcast to all connected clients).

## Known limitation

If the `execution` container is itself down during a 1100, there is no
observer. Acceptable: a fully dead/stuck Gateway is already covered by the
port-down two-strike path; this feature closes only the "port up, server link
down" gap.

## Tests

- **Python (pytest, `tests/services/execution/`):** unit-test the error handler
  — 1100 writes the marker with an epoch, 1101/1102 clear it, a successful
  connect clears it, and a marker-I/O failure is swallowed (order routing
  unaffected). Isolate the marker path via a tmp dir.
- **Shell:** the watchdog change is a small addition to an existing branch;
  verify by `shellcheck` and by hand-tracing the four states
  (no marker / fresh marker / sustained marker / recovered). Factor the
  age-vs-threshold decision so it reads plainly.
