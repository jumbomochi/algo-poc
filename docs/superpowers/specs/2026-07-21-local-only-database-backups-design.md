# Local-Only Database Backups Design

## Goal

Stop the daily database-backup job from accessing iCloud Drive while preserving
the existing verified local PostgreSQL backups and all local operational safety
controls.

## Scope

The repository copy at `deploy/launchd/run_db_backup.sh` and the installed copy
at `~/ibc/run_db_backup.sh` will no longer define an iCloud destination, invoke
`rsync`, prune iCloud files, or emit an iCloud-copy failure alert.

The following behavior remains unchanged:

- daily launchd scheduling at 05:15 SGT;
- `pg_dump` creation in `~/ibc/backups/`;
- archive size and `pg_restore --list` verification;
- Telegram alerts for failures that endanger the local backup;
- 30-day retention for local dumps and backup logs;
- the existing restore procedure.

No replacement offsite-storage system will be added.

## Implementation

Remove the iCloud-specific comments, `OFFSITE_DIR`, copy block, offsite
retention, and offsite-warning message from the repository script. Update the
operations runbook and launchd documentation so they describe a local-only
backup policy and do not imply an offsite guarantee.

After repository verification, install the exact repository script over the
operational copy. The launchd plist and schedule do not change, so no unload or
reload operation is required.

## Verification

A regression test will inspect the repository backup script and assert that it
contains no iCloud path, `OFFSITE_DIR`, or `rsync` invocation while retaining
the dump, archive verification, local retention, and failure-alert controls.

Both script copies will be checked with `bash -n` and byte-compared after
installation. The backup job itself will not be run during this change because
running it would also execute its normal retention pruning.

## Safety

Existing files under `~/ibc/backups/`, the old files already present in iCloud,
and all database state remain untouched. This change removes future automated
iCloud access; it does not delete historical iCloud copies.
