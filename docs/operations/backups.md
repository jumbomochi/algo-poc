# Paper-DB Backups

Daily `pg_dump` of the dockerized paper database (`algo_poc` on the
`algo-poc-postgres-1` container), giving an **RPO of at most 1 day**. Added
after the 2026-07-10 incident in which an agent session wiped all paper
trading state via `run_paper.py --reset` with no backup to restore from.

## What runs

| | |
|---|---|
| Job | `local.algo-db-backup` (launchd), 05:15 SGT daily |
| Script | `~/ibc/run_db_backup.sh` (repo copy: `deploy/launchd/run_db_backup.sh`) |
| Dumps | `~/ibc/backups/algo_poc_<YYYYmmdd_HHMMSS>.dump` (pg_dump custom format, compressed) |
| Retention | 30 days, pruned by the job itself |
| Logs | `~/ibc/logs/db_backup_<YYYYmmdd>.log`, launchd stdout to `db-backup-launchd.log` |
| Alerts | Telegram on ANY failure (container down, dump error, unreadable archive). Success is logged only. |

The 05:15 slot is deliberately after the 04:15 paper run and 04:45 divergence
monitor, so each dump contains that trading day's snapshots and positions.

Every dump is verified with `pg_restore --list` before the job reports
success — an unreadable archive counts as a failed backup.

There is a second, event-driven layer: `run_paper.py --reset` writes a JSON
dump of all four state tables to `output/paper_state_pre_reset_<ts>.json`
before deleting anything, and refuses to run at all when stdin is not a TTY.

## Restore

```bash
# 1. Pick a dump
ls -lt ~/ibc/backups/

# 2. Stop writers (paper services) so nothing races the restore
docker compose stop risk-management execution

# 3. Restore into the running postgres container.
#    --clean --if-exists drops and recreates objects from the dump.
docker exec -i algo-poc-postgres-1 \
    pg_restore -U algo -d algo_poc --clean --if-exists --no-owner \
    < ~/ibc/backups/algo_poc_YYYYmmdd_HHMMSS.dump

# 4. Sanity-check, then restart services
docker exec algo-poc-postgres-1 psql -U algo -d algo_poc \
    -c "SELECT portfolio, MAX(date) FROM equity_snapshots GROUP BY 1;"
docker compose start risk-management execution
```

To restore a single table (e.g. only `equity_snapshots`), add
`--table=equity_snapshots --data-only` and truncate the table first.

## Limitations / future work

- Backups live on the same disk as the database. This protects against the
  observed failure mode (accidental deletion), not machine loss. If offsite
  coverage is wanted, rsync `~/ibc/backups/` to iCloud Drive or object
  storage as a follow-up.
- RPO is 1 day: intra-day writes since 05:15 are not covered (no WAL
  archiving). Acceptable while the paper book is rebuilt nightly from the
  04:15 run.
