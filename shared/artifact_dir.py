"""Where an operator artifact goes so it outlives the tree that wrote it.

Ops tooling is routinely run from a throwaway git worktree — it is how a script
on ``develop`` gets exercised before the promotion reaches ``main``. Both of the
artifact writers resolved their default directory in a way that put the file
*inside* that worktree, and ``git worktree remove`` then deleted it:

* ``scripts/ops/backfill_restored_equity.py`` defaulted to its own
  ``_REPO_ROOT``, i.e. the **script's** repo, so it wrote into the worktree even
  when the caller stood somewhere else;
* ``scripts/reconcile_paper.py`` defaulted to the **relative**
  ``output/reconciliation``, so it wrote wherever the caller happened to be.

Opposite mechanisms, same outcome. On 2026-09-16 that destroyed the audit
artifact for an equity-series backfill — the record of a deliberate rewrite of
gate evidence, gone minutes after it was written, and reconstructible only
because every input happened to survive elsewhere.

The fix is not to guess less but to resolve better: an artifact written from a
linked worktree belongs under the MAIN worktree, at the same relative path.
The database, the book and the evidence are all shared; only the checkout is
disposable, so the artifact must not live in the disposable half.

An explicitly-supplied directory is always honoured. This governs defaults.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

__all__ = ["durable_artifact_dir", "main_worktree_root"]


def _git(*args: str, cwd: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    value = out.stdout.strip()
    return value or None


def main_worktree_root(start: Path) -> Path | None:
    """The main worktree's root, or ``None`` if ``start`` is not in a repo.

    In a LINKED worktree ``--git-dir`` is ``<main>/.git/worktrees/<name>`` while
    ``--git-common-dir`` is ``<main>/.git``; in the main worktree the two are the
    same path. That difference is the only reliable signal — a ``.worktrees/``
    path component is this repo's convention, not git's, and would miss a
    worktree created anywhere else.
    """
    cwd = start if start.is_dir() else start.parent
    if not cwd.exists():
        return None
    common = _git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=cwd)
    if common is None:
        return None
    return Path(common).parent


def durable_artifact_dir(candidate: Path | str, *, explicit: bool = False) -> Path:
    """Relocate ``candidate`` out of a linked worktree, if it is in one.

    Returns the path unchanged when it is explicit, when the tree is not a
    linked worktree, or when anything cannot be determined — a path that merely
    might be wrong is far better than refusing to write an audit record at all.
    """
    resolved = Path(candidate).expanduser().resolve()
    if explicit:
        return resolved

    cwd = resolved if resolved.is_dir() else resolved.parent
    while not cwd.exists() and cwd != cwd.parent:
        cwd = cwd.parent

    git_dir = _git("rev-parse", "--path-format=absolute", "--git-dir", cwd=cwd)
    common = _git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=cwd)
    if git_dir is None or common is None or Path(git_dir) == Path(common):
        return resolved  # not a repo, or already the main worktree

    toplevel = _git("rev-parse", "--show-toplevel", cwd=cwd)
    if toplevel is None:
        return resolved
    try:
        relative = resolved.relative_to(Path(toplevel).resolve())
    except ValueError:
        return resolved  # outside the worktree; the caller meant somewhere else
    return Path(common).parent / relative
