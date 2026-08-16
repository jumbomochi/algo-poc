# Container Deploy Runbook

How to put merged code into the running `risk-management` and `execution`
containers, and how to prove it landed.

Merging to `main` does not deploy anything. The services run from images built
on this host; until those images are rebuilt **and the containers recreated on
them**, `main` and production are different code. On 2026-08-07 a bring-up ran
`docker compose up -d --build`, the build succeeded, and the containers kept
running the previous image — the fix looked deployed for hours and was not.
Everything below exists to make that outcome impossible to reach silently.

Scope: the two money-path services. The same steps work for any service — swap
the names — but recreating `redis` has a consequence of its own (see
[Why `--no-deps`](#why---no-deps)).

---

## Preconditions

| # | Check | Why |
|---|---|---|
| 1 | Run the whole runbook in **bash**, not zsh (`bash` then proceed) | `deploy/launchd/secrets.sh` is bash-only, and the exports it sets must survive into the `docker compose` calls |
| 2 | 1Password unlocked, and the login keychain unlocked | `.env` on this host is a **1Password-served FIFO**, not a file. Compose reads it for interpolation; if nothing is serving it, the read blocks ~60s and returns empty (the 2026-08-12 outage) |
| 3 | Deploy outside the scheduled window and outside NYSE hours | The launchd block runs 04:15–05:15 local (paper run 04:15 Tue–Sat, divergence 04:45, pipeline report 04:52, backtest refresh Tue 05:00, backup 05:15; the evidence digest sits apart at Mon 08:00). Recreating `execution` mid-session drops the IB connection |
| 4 | Stack already up: `docker compose ps` shows every service healthy | These steps recreate two containers in place; they do not bring up a cold stack |
| 5 | Working tree is the commit you intend to ship (`git log -1`, `git status`) | The image is built from the working tree, not from a ref |

```bash
cd ~/GitHub/algo-poc
bash                       # secrets.sh is bash-only
. deploy/launchd/secrets.sh
algo_load_secrets POSTGRES_PASSWORD REDIS_PASSWORD || echo "$ALGO_SECRETS_ERROR"
git log -1 --oneline && git status --short
docker compose ps
```

Pre-flight that the FIFO is actually being served — this must return in about a
second, not after a minute:

```bash
time docker compose config --quiet && echo "interpolation OK"
```

A hang here means 1Password is not serving `.env`. Stop and fix that first: a
deploy that proceeds with empty interpolation recreates `execution` with an
**empty `ALGO_IB_ACCOUNT_ID`**, silently removing the account pin and leaving
the `DU`/`U` prefix guard as the only thing between the bot and the live
account. Step 4 below catches it after the fact; this catches it before.

---

## Step 1 — Record the current state (before touching anything)

Two things get recorded: the image each container is running, and the
environment each container was built with. Both are compared again in step 4.

```bash
STAMP=$(date +%Y%m%dT%H%M%S)
EVID=~/ibc/deploys/$STAMP
mkdir -p "$EVID"

: > "$EVID/images-before.txt"          # truncate, so a re-run does not append
for svc in risk-management execution; do
  cid=$(docker compose ps -q "$svc")
  printf '%s %s\n' "$svc" "$(docker inspect --format '{{.Image}}' "$cid")" >> "$EVID/images-before.txt"
  docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$cid" \
    | sed -E 's/:[^:@/]+@/:***@/' | sort > "$EVID/env-$svc-before.txt"
done
cat "$EVID/images-before.txt"
```

Evidence lives in `~/ibc/deploys/`, deliberately not in `~/ibc/logs/` — the
backup job prunes that tree by name glob and mtime, and deploy evidence should
outlive a 30-day retention window.

The `sed` masks the password inside `ALGO_DATABASE_URL` / `ALGO_REDIS_URL`.
**Do not remove it** — these files are the evidence you paste into the ticket.

Now tag the images currently in use, so a rollback is a retag rather than a
rebuild from an older commit under pressure:

```bash
docker tag algo-poc-risk-management:latest algo-poc-risk-management:pre-tranche1
docker tag algo-poc-execution:latest       algo-poc-execution:pre-tranche1
docker image inspect --format '{{.Id}}' \
  algo-poc-risk-management:pre-tranche1 algo-poc-execution:pre-tranche1
```

Those two IDs must match `images-before.txt`. Tags are cheap and shared — this
adds a name to the existing image, it does not copy anything.

> Rolling a second time? Pick a fresh tag suffix (`:pre-<date>`) rather than
> overwriting `:pre-tranche1`; an overwritten rollback tag points at the very
> image you are trying to roll back from.

## Step 2 — Build

```bash
docker compose build risk-management execution
```

Build only. Nothing is running the new image yet, so this step is safe to do
early and safe to abandon.

## Step 3 — Recreate

```bash
docker compose up -d --force-recreate --no-deps risk-management execution
```

Both flags are load-bearing:

- **`--force-recreate`** is the whole point. `up -d` alone (even with
  `--build`) considers a container with an unchanged *configuration* to be
  up-to-date and leaves it running the old image. This is the 2026-08-07
  failure. Never run the `up` in this runbook without it.
- <a id="why---no-deps"></a>**`--no-deps`** stops the force-recreate from
  cascading. Without it, compose brings up each service's dependency chain too
  — `migrate`, `ml-model`, and through them `postgres` and `redis` — and
  `--force-recreate` recreates those as well. **Redis has no volume**
  (`docker-compose.yml` declares only `pgdata`): recreating it discards every
  stream, including any unacked entry on `stream:approved_orders` or
  `stream:fills`. Consumer groups are recreated at service startup, so the
  damage is invisible in `docker compose ps` — you would simply lose whatever
  was in flight.

## Step 4 — Verify the running image actually changed

```bash
: > "$EVID/images-after.txt"
for svc in risk-management execution; do
  cid=$(docker compose ps -q "$svc")
  printf '%s %s\n' "$svc" "$(docker inspect --format '{{.Image}}' "$cid")" >> "$EVID/images-after.txt"
  docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$cid" \
    | sed -E 's/:[^:@/]+@/:***@/' | sort > "$EVID/env-$svc-after.txt"
done
diff "$EVID/images-before.txt" "$EVID/images-after.txt"   # expect: a difference on BOTH lines
```

Three assertions, all of which must hold:

1. **The hash changed** for both services. `diff` exits 1 and shows two changed
   lines. Exit 0 (no output) means nothing was recreated — the deploy did not
   happen, whatever the build said.
2. **The new hash is the image just built**, not some other image:

   ```bash
   docker image inspect --format '{{.Id}}' algo-poc-risk-management:latest algo-poc-execution:latest
   ```

   These two IDs must equal the two in `images-after.txt`.
3. **The environment is unchanged.** Empty output from both:

   ```bash
   diff "$EVID/env-risk-management-before.txt" "$EVID/env-risk-management-after.txt"
   diff "$EVID/env-execution-before.txt"       "$EVID/env-execution-after.txt"
   ```

   A line appearing here means compose interpolated differently than last time.
   The one that matters most is `ALGO_IB_ACCOUNT_ID=DUN551088` turning into
   `ALGO_IB_ACCOUNT_ID=` — the account pin, dropped because `.env` was not
   served (precondition 2). Roll back and fix the environment before retrying.

Once all three hold, name the deployed images so the rollback drill below is a
retag in both directions rather than a rebuild:

```bash
docker tag algo-poc-risk-management:latest algo-poc-risk-management:tranche1
docker tag algo-poc-execution:latest       algo-poc-execution:tranche1
```

Note the ordering trap: `docker compose images --quiet risk-management
execution` prints hashes in **container-name order, not argument order**, so
zipping its output against your service list silently pairs each hash with the
wrong service. Address one service at a time, as above, or read the service
name back out of the JSON:

```bash
docker compose images --format json risk-management
```

## Step 5 — Smoke test

```bash
docker compose ps risk-management execution        # both Up and (healthy)
docker compose logs --tail=30 risk-management
docker compose logs --tail=30 execution
```

Expected within ~30 seconds of the recreate:

| Service | Log line | Proves |
|---|---|---|
| `risk-management` | `Portfolio state loaded from DB` (with `nav`, `open_positions`) | Postgres reachable, book loaded |
| `risk-management` | `Risk service consumer groups created` → `Risk management service started` | Redis reachable, main loop entered |
| `execution` | `Connected to IB` (with `accounts`) | IB Gateway reachable, and the account list is the one you expect |
| `execution` | `Execution service consumer groups created` → `Execution service started` | Consuming `stream:approved_orders` |

`(healthy)` is itself a real signal here: both healthchecks read the heartbeat
file that each service's main loop touches every iteration
(`shared/heartbeat.py`), with a 120s staleness threshold. A container that
starts but wedges goes `(unhealthy)` within ~2 minutes. Compose will **not**
restart it for you — that is a deliberate choice documented in
`docker-compose.yml` — so check it rather than assuming.

**What the immediate smoke test cannot prove.** Two of the signals worth having
are not observable on a quiet book:

- The risk service's 30-minute periodic scan
  (`passive_scan_interval_minutes: 30`) logs `Passive scan completed` **only
  when it finds a breach** (`services/risk_management/runner.py:1780`). A clean
  scan is silent. What you can confirm is that the driver is being reached:
  `maybe_run_periodic_checks` is called in the same loop iteration as
  `write_heartbeat` (`runner.py:2106-2112`), so a container that stays
  `(healthy)` past the 30-minute mark has necessarily run the scan.
- `execution` logs `Processing approved order` only when an approved order
  arrives, which on a quiet stack is never.

Both get their real proof at the next paper run (step 7). Do not manufacture
traffic on the live paper stack to satisfy a smoke test.

## Step 6 — Prove the rollback (once, on one service)

Do this immediately after a successful deploy, while nothing is at stake. A
rollback path first exercised during an incident is not a rollback path.

```bash
docker tag algo-poc-risk-management:pre-tranche1 algo-poc-risk-management:latest
docker compose up -d --force-recreate --no-deps risk-management
docker inspect --format '{{.Image}}' "$(docker compose ps -q risk-management)"   # == the pre- hash
```

Then return to the deployed image and confirm the hash is the new one again:

```bash
docker tag algo-poc-risk-management:tranche1 algo-poc-risk-management:latest
docker compose up -d --force-recreate --no-deps risk-management
docker inspect --format '{{.Image}}' "$(docker compose ps -q risk-management)"
```

Record both hashes. Both directions are retag + recreate — no rebuild, which is
the point: under pressure you must not be waiting on a Docker build. And both
`up` commands carry `--force-recreate`, because retagging `:latest` changes
nothing about a container that is already running.

## Step 7 — Next scheduled paper run

The deploy is not finished until one full run has gone through the new code.
The morning after (04:15 local, Tue–Sat):

```bash
tail -50 ~/ibc/logs/paper_trading_$(date +%Y%m%d).log
docker compose logs --since=6h execution | grep "Processing approved order"
docker compose logs --since=6h risk-management | grep -iE "error|traceback"
```

Confirm the run completed, reconciliation reported `ok`, and the digest arrived
on Telegram. `Processing approved order` in the execution log is the proof that
the recreated container is consuming `stream:approved_orders` — a container
that starts but does not process is a silent failure the healthcheck only
catches later. On a day the model recommends nothing there is legitimately
nothing to consume; in that case the run's own log, not the container log, is
the evidence.

A reconciliation `major` or a missing digest after a deploy is a rollback
trigger, not a thing to investigate for an hour first — roll back (step 6),
then investigate.

---

## Cold-reboot verification [OPERATOR]

Run once per tranche, after the deploy has settled. This is the scenario the
2026-08-11 incident actually exercised: everything comes back on its own, or it
does not.

1. Reboot the host.
2. Log in (the login keychain unlocks on login; launchd jobs and Docker Desktop
   both need it) and wait ~3 minutes for the stack to settle.
3. Check all three layers — expect **10 long-running containers** (all
   healthy), **7 launchd jobs**, and zero drift:

```bash
docker compose ps
launchctl list | grep local.algo
deploy/launchd/deploy.sh --dry-run    # expect "Everything is already in sync."
```

Every app service and both infrastructure services carry
`restart: unless-stopped`, so the containers should return without any command.
`migrate` is `restart: "no"` by design and correctly does **not** appear in
`docker compose ps` afterwards — it runs to completion and exits.

If Docker Desktop itself did not start, no container returns: enable *Start
Docker Desktop when you sign in* in its settings. That is a host setting, not
something this repo controls.

> The job count moves. It was 6 until KAN-29 added
> `local.algo-evidence-digest.plist` (Mondays 08:00), and a job that exists in
> `deploy/launchd/` is not yet a job on this host: `deploy.sh` copies the
> plist, but **bootstrapping it is a separate manual step** — `deploy.sh`
> prints the exact `launchctl bootout`/`bootstrap` lines for every plist it
> changed, and running them is a human step by policy (CLAUDE.md). So a
> freshly-promoted plist shows up in `launchctl list` only after you have run
> those lines once. If the count here is short by one right after a
> promotion, that is the reason — check `deploy.sh --dry-run` first.
>
> `tests/deploy/test_container_deploy.py` pins the number above to the plists
> on disk, so it cannot quietly go stale. It already caught one: this runbook
> said 6 for about an hour before KAN-29 landed.

---

## Evidence to record on the ticket

Paste these into the issue (they are already masked; check before pasting):

```
commit deployed      : <git log -1 --oneline>
images before        : <images-before.txt>
images after         : <images-after.txt>
built image ids      : <docker image inspect ... :latest>
env diff             : <empty, or the lines that changed>
rollback drill       : <pre- hash observed, then post- hash observed>
cold reboot          : <containers / jobs / deploy.sh drift>
first paper run after: <date, completed?, reconciliation status>
```

---

## Rollback

Retag and recreate — one command per service, no rebuild:

```bash
docker tag algo-poc-risk-management:pre-tranche1 algo-poc-risk-management:latest
docker tag algo-poc-execution:pre-tranche1       algo-poc-execution:latest
docker compose up -d --force-recreate --no-deps risk-management execution
```

The paper book is unaffected by a container swap: all state is in Postgres and
the `pgdata` volume is never touched here. **Never** reach for `docker compose
down -v` or `docker volume rm` to "clean up" a bad deploy — that destroys the
paper trading history, which cannot be recreated (see CLAUDE.md).

Rolling back the *mode* (live → paper) is a different procedure entirely; see
`rollback-playbook.md`.

---

## Deliberately not automated

- **No CI/CD to this host.** A single-operator Mac does not need it, and an
  auto-deploy on merge would add a failure mode — code reaching the money path
  without anyone present — in exchange for saving five minutes.
- **No restart-on-unhealthy.** Compose leaves an unhealthy container running so
  the wedge is visible. Changing that hides the failure this repo spent two
  incidents learning to see.
