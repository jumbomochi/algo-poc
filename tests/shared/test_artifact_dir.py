"""An operator artifact must outlive the tree that wrote it.

Running ops tooling from a throwaway git worktree is normal here — it is how a
script on ``develop`` gets exercised before the promotion reaches ``main``. Both
artifact writers resolved their default in a way that put the file INSIDE that
worktree, by opposite mechanisms:

* ``backfill_restored_equity.py`` defaulted to its own ``_REPO_ROOT`` — the
  SCRIPT's repo — so it wrote into the worktree even when the caller stood
  elsewhere;
* ``reconcile_paper.py`` defaulted to the RELATIVE ``output/reconciliation``, so
  it wrote wherever the caller happened to be.

On 2026-09-16 that destroyed the audit artifact for an equity-series backfill —
the record of a deliberate rewrite of gate evidence — minutes after it was
written, and it survived only because every input happened to exist elsewhere
and it could be reconstructed.

The database, the book and the evidence are shared; only the checkout is
disposable. These tests pin that an artifact never lands in the disposable half.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from shared.artifact_dir import durable_artifact_dir, main_worktree_root


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True,
                   capture_output=True, text=True)


@pytest.fixture()
def repo(tmp_path) -> Path:
    """A real repo with one commit — worktrees need a commit to branch from."""
    root = tmp_path / "main"
    root.mkdir()
    _git("init", "-q", "-b", "main", cwd=root)
    _git("config", "user.email", "t@example.com", cwd=root)
    _git("config", "user.name", "t", cwd=root)
    (root / "seed.txt").write_text("seed\n")
    _git("add", "seed.txt", cwd=root)
    _git("commit", "-qm", "seed", cwd=root)
    return root


@pytest.fixture()
def linked(repo) -> Path:
    """A linked worktree — the disposable kind."""
    path = repo / ".worktrees" / "scratch"
    _git("worktree", "add", "-q", "--detach", str(path), cwd=repo)
    return path


# ---------------------------------------------------------------------------
# The defect this exists for
# ---------------------------------------------------------------------------


def test_a_default_inside_a_linked_worktree_is_relocated(repo, linked):
    """The 2026-09-16 case: `git worktree remove` would have deleted it."""
    candidate = linked / "output" / "reconciliation"
    assert durable_artifact_dir(candidate) == repo / "output" / "reconciliation"


def test_the_relative_path_is_preserved_when_relocating(repo, linked):
    """It moves trees, not directories: a nested path keeps its shape."""
    candidate = linked / "output" / "deep" / "nested"
    assert durable_artifact_dir(candidate) == repo / "output" / "deep" / "nested"


def test_the_artifact_survives_removing_the_worktree(repo, linked):
    """End to end, against a real `git worktree remove` — the operation that
    destroyed the original."""
    target = durable_artifact_dir(linked / "output" / "reconciliation")
    target.mkdir(parents=True, exist_ok=True)
    artifact = target / "equity-backfill-20260916T114718Z.json"
    artifact.write_text('{"portfolio": "sector_rotation"}\n')

    _git("worktree", "remove", "--force", str(linked), cwd=repo)

    assert not linked.exists(), "the worktree really was removed"
    assert artifact.exists(), "the artifact must outlive it"


# ---------------------------------------------------------------------------
# What must NOT be relocated
# ---------------------------------------------------------------------------


def test_the_main_worktree_is_left_alone(repo):
    candidate = repo / "output" / "reconciliation"
    assert durable_artifact_dir(candidate) == candidate


def test_an_explicit_directory_is_always_honoured(repo, linked):
    """The operator naming a path outranks this. Silently writing somewhere
    other than where you were told is its own kind of lost artifact."""
    candidate = linked / "output" / "reconciliation"
    assert durable_artifact_dir(candidate, explicit=True) == candidate


def test_a_path_outside_the_worktree_is_left_alone(repo, linked, tmp_path):
    """An absolute path elsewhere means the caller meant elsewhere."""
    outside = (tmp_path / "elsewhere").resolve()
    assert durable_artifact_dir(outside) == outside


def test_a_path_in_no_repo_at_all_is_left_alone(tmp_path):
    """Refusing to write an audit record would be far worse than writing it to
    a path that is merely not ideal."""
    plain = (tmp_path / "loose" / "artifacts").resolve()
    assert durable_artifact_dir(plain) == plain


# ---------------------------------------------------------------------------
# Detection is by git, not by this repo's naming convention
# ---------------------------------------------------------------------------


def test_a_worktree_outside_the_dot_worktrees_convention_is_still_detected(
    repo, tmp_path
):
    """`.worktrees/` is this repo's habit, not git's. Matching on the path
    would miss a worktree created anywhere else."""
    elsewhere = tmp_path / "somewhere-else"
    _git("worktree", "add", "-q", "--detach", str(elsewhere), cwd=repo)

    candidate = elsewhere / "output" / "reconciliation"
    assert durable_artifact_dir(candidate) == repo / "output" / "reconciliation"


def test_main_worktree_root_finds_the_main_tree_from_a_linked_one(repo, linked):
    assert main_worktree_root(linked) == repo


def test_main_worktree_root_is_none_outside_a_repo(tmp_path):
    assert main_worktree_root(tmp_path / "not-a-repo") is None


def test_a_directory_that_does_not_exist_yet_still_resolves(repo, linked):
    """Artifact dirs are created on write, so the path is routinely absent when
    it is resolved."""
    candidate = linked / "output" / "not" / "created" / "yet"
    assert durable_artifact_dir(candidate) == repo / "output" / "not" / "created" / "yet"
