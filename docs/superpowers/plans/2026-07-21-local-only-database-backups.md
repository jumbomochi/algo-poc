# Local-Only Database Backups Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove every automated iCloud access from the daily database-backup job while preserving verified local backups, alerts, retention, scheduling, and restore behavior.

**Architecture:** Keep the existing launchd job and local backup script, but delete the offsite-copy stage entirely. A static regression test defines the local-only contract; after repository verification, the tested script is installed byte-for-byte at `~/ibc/run_db_backup.sh` without running the job.

**Tech Stack:** Bash, launchd, pytest, Markdown operations documentation.

## Global Constraints

- Do not run `scripts/run_paper.py --reset` or any destructive database command.
- Do not delete or overwrite existing files under `~/ibc/backups/`, `~/ibc/logs/`, or the existing iCloud backup folder.
- Do not run the backup script during this change because its normal execution prunes expired local backups and logs.
- Do not unload or disable the launchd job.
- Do not add replacement offsite storage.
- Preserve the 05:15 SGT schedule, local archive verification, Telegram failure alerts, and 30-day local retention.

---

### Task 1: Make the repository backup policy local-only

**Files:**
- Create: `tests/deploy/test_db_backup_script.py`
- Modify: `deploy/launchd/run_db_backup.sh`
- Modify: `docs/operations/backups.md`
- Modify: `deploy/launchd/README.md`

**Interfaces:**
- Consumes: the existing `deploy/launchd/run_db_backup.sh` launchd entry point.
- Produces: a local-only backup script whose operational copy remains `~/ibc/run_db_backup.sh`.

- [ ] **Step 1: Write the failing local-only contract test**

```python
from __future__ import annotations

from pathlib import Path


SCRIPT = Path("deploy/launchd/run_db_backup.sh")


def test_database_backup_is_local_only() -> None:
    text = SCRIPT.read_text()

    forbidden = (
        "OFFSITE_DIR",
        "com~apple~CloudDocs",
        "iCloud",
        "rsync ",
        "offsite",
    )
    for marker in forbidden:
        assert marker not in text


def test_database_backup_retains_local_safety_controls() -> None:
    text = SCRIPT.read_text()

    required = (
        'BACKUP_DIR="$HOME/ibc/backups"',
        "pg_dump",
        "pg_restore --list",
        'RETENTION_DAYS=30',
        'find "$BACKUP_DIR"',
        'find "$LOG_DIR"',
        'telegram "❌ Paper-DB backup FAILED:',
    )
    for marker in required:
        assert marker in text
```

- [ ] **Step 2: Run the contract test and verify RED**

Run: `pytest tests/deploy/test_db_backup_script.py -v`

Expected: `test_database_backup_is_local_only` fails because the script still contains `OFFSITE_DIR`, the iCloud path, `rsync`, and offsite comments.

- [ ] **Step 3: Remove only the iCloud stage from the repository script**

Delete the introductory offsite-copy comments, the `OFFSITE_DIR` assignment, and the entire block beginning with `# Offsite copy to iCloud Drive` and ending after its `fi`. Leave the local retention block unchanged:

```bash
# Retention: prune local dumps and job logs older than RETENTION_DAYS.
find "$BACKUP_DIR" -name "algo_poc_*.dump" -mtime "+$RETENTION_DAYS" -delete 2>/dev/null
find "$LOG_DIR" -name "db_backup_*.log" -mtime "+$RETENTION_DAYS" -delete 2>/dev/null

exit 0
```

- [ ] **Step 4: Update the backup runbook**

In `docs/operations/backups.md`, remove the Offsite row and the `## Offsite copy (iCloud Drive)` section. Add this limitation:

```markdown
- Backups are local to this Mac. No offsite copy is currently configured, so
  machine loss can remove both the database and its backups. This is accepted
  temporarily; the backup job must not be granted Full Disk Access merely to
  copy files into iCloud Drive.
```

- [ ] **Step 5: Clarify the launchd README**

Under `## Daily paper-DB backup` in `deploy/launchd/README.md`, add:

```markdown
- **Storage:** local-only; the job does not access iCloud Drive or other
  offsite storage.
```

- [ ] **Step 6: Verify the repository change**

Run: `pytest tests/deploy/test_db_backup_script.py -v`

Expected: `2 passed`.

Run: `bash -n deploy/launchd/run_db_backup.sh`

Expected: exit 0 with no output.

Run: `rg -n -i "icloud|offsite|rsync|CloudDocs|OFFSITE_DIR" deploy/launchd/run_db_backup.sh docs/operations/backups.md deploy/launchd/README.md`

Expected: only the intentional documentation statements that no offsite storage is configured; no match in `run_db_backup.sh`.

Run: `pytest`

Expected: the full suite passes.

- [ ] **Step 7: Commit the repository change**

```bash
git add tests/deploy/test_db_backup_script.py deploy/launchd/run_db_backup.sh docs/operations/backups.md deploy/launchd/README.md
git commit -m "ops: keep database backups local only"
```

### Task 2: Install the tested local-only script

**Files:**
- Source: `deploy/launchd/run_db_backup.sh`
- Update: `~/ibc/run_db_backup.sh`

**Interfaces:**
- Consumes: the repository script verified in Task 1.
- Produces: the exact script invoked by `local.algo-db-backup` at 05:15 SGT.

- [ ] **Step 1: Inspect metadata without reading or altering backup contents**

Run: `ls -l deploy/launchd/run_db_backup.sh ~/ibc/run_db_backup.sh`

Expected: both paths exist; the installed script is executable.

- [ ] **Step 2: Install the repository script**

Use the patch/edit mechanism authorized for `~/ibc/run_db_backup.sh` to make its contents byte-identical to `deploy/launchd/run_db_backup.sh`. Preserve executable mode. Do not invoke the script.

- [ ] **Step 3: Verify the installed copy**

Run: `bash -n ~/ibc/run_db_backup.sh`

Expected: exit 0 with no output.

Run: `cmp -s deploy/launchd/run_db_backup.sh ~/ibc/run_db_backup.sh`

Expected: exit 0.

Run: `test -x ~/ibc/run_db_backup.sh`

Expected: exit 0.

Run: `rg -n -i "icloud|offsite|rsync|CloudDocs|OFFSITE_DIR" ~/ibc/run_db_backup.sh`

Expected: no matches.

- [ ] **Step 4: Confirm repository and operational state**

Run: `git status --short --branch`

Expected: clean working tree on `main`; the installed script is outside Git and does not appear.

Do not run the backup job. Its next normal 05:15 SGT execution will create and verify a local dump without attempting iCloud access.
