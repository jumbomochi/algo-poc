"""Backfill ModelVersion.content_hash for rows saved before the model
integrity check existed (migration e7a1c4d92f3b_add_model_version_content_hash).

Why this exists: ModelRegistry.load_active() (services/ml_model/registry.py)
refuses to load any model whose content_hash is unset — fail-closed, by
design, since a NULL content_hash is indistinguishable from "nobody ever
verified this file." Every ModelVersion row created before that migration
landed has content_hash = NULL, so the very next load_active() call after
the migration lands raises ModelIntegrityError until this backfill runs.

Required rollout sequence (see the migration's docstring and
docs/operations/api-security.md, "Model integrity — rollout sequence"):

    1. alembic upgrade head                                   (adds the column)
    2. python -m scripts.ops.backfill_model_hashes --apply    (this script)
    3. Verify load_active() succeeds (restart ml_model, or call it directly)
       before considering the rollout complete.

Dry-run by default: computes and reports what WOULD be written, writes
nothing. Pass --apply to actually persist the computed hashes. A row whose
model file is no longer on disk is reported and skipped, never guessed at.

Usage:
    python -m scripts.ops.backfill_model_hashes                # dry run
    python -m scripts.ops.backfill_model_hashes --apply         # write
    python -m scripts.ops.backfill_model_hashes --db-url sqlite:///paper.db

This is an operator tool. It is never invoked automatically by an agent —
run it yourself, review the dry-run report first, then re-run with --apply.
"""

from __future__ import annotations

import argparse
import hashlib
import os
from dataclasses import dataclass
from typing import Any

from shared.models.ml_models import ModelVersion


def _hash_file(path: str) -> str:
    """Return the hex sha256 digest of a file's contents, streamed in chunks."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class BackfillOutcome:
    version: str
    model_path: str
    status: str  # "written" | "would_write" | "missing_file"
    content_hash: str | None = None


def find_rows_needing_backfill(db_session: Any) -> list[ModelVersion]:
    """ModelVersion rows with no recorded content_hash — backfill candidates."""
    return (
        db_session.query(ModelVersion)
        .filter(ModelVersion.content_hash.is_(None))
        .all()
    )


def backfill_content_hashes(
    db_session: Any, apply: bool = False
) -> list[BackfillOutcome]:
    """Compute (and, if ``apply``, persist) content_hash for every
    ModelVersion row missing one, provided its model file still exists.

    A row whose model file is missing is reported as ``"missing_file"`` and
    left untouched — a lost artifact is a separate problem this script
    does not try to solve or paper over.
    """
    outcomes: list[BackfillOutcome] = []
    for row in find_rows_needing_backfill(db_session):
        if not os.path.exists(row.model_path):
            outcomes.append(
                BackfillOutcome(row.version, row.model_path, "missing_file")
            )
            continue

        content_hash = _hash_file(row.model_path)
        if apply:
            row.content_hash = content_hash
            outcomes.append(
                BackfillOutcome(row.version, row.model_path, "written", content_hash)
            )
        else:
            outcomes.append(
                BackfillOutcome(row.version, row.model_path, "would_write", content_hash)
            )

    if apply:
        db_session.commit()

    return outcomes


def _print_report(outcomes: list[BackfillOutcome], apply: bool) -> None:
    mode = "APPLY" if apply else "DRY RUN"
    print(f"[{mode}] {len(outcomes)} model_versions row(s) with no content_hash")
    for outcome in outcomes:
        if outcome.status == "missing_file":
            print(f"  SKIP        {outcome.version}: model file not found at {outcome.model_path}")
        elif outcome.status == "would_write":
            print(f"  WOULD-WRITE {outcome.version}: content_hash={outcome.content_hash}")
        else:
            print(f"  WRITTEN     {outcome.version}: content_hash={outcome.content_hash}")

    if not apply and any(o.status == "would_write" for o in outcomes):
        print("\nDry run only — re-run with --apply to persist these values.")

    missing = [o for o in outcomes if o.status == "missing_file"]
    if missing:
        print(
            f"\n{len(missing)} row(s) have no model file on disk and were "
            "skipped — load_active() will keep refusing these until the "
            "artifact is restored or the row is otherwise remediated."
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db-url",
        default=None,
        help="Database URL (default: AppConfig.database.url from config/default.yaml)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Persist the computed hashes (default: dry run, report only).",
    )
    args = parser.parse_args(argv)

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from shared.config import load_config

    db_url = args.db_url or load_config("config/default.yaml").database.url
    engine = create_engine(db_url)
    session = sessionmaker(bind=engine)()
    try:
        outcomes = backfill_content_hashes(session, apply=args.apply)
        _print_report(outcomes, args.apply)
    finally:
        session.close()

    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
