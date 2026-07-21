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
        "RETENTION_DAYS=30",
        'find "$BACKUP_DIR"',
        'find "$LOG_DIR"',
        'telegram "❌ Paper-DB backup FAILED:',
    )
    for marker in required:
        assert marker in text
