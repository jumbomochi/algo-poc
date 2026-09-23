"""Production must not run out of the development checkout — KAN-72.

WHAT WAS TRUE
-------------
Every launchd wrapper resolved its code from one hardcoded directory::

    ALGO_DIR="${ALGO_DIR:-/Users/huiliang/GitHub/algo-poc}"

which was also the interactive development checkout. That path names a *working
tree*, not a branch, so production ran whatever branch someone last left checked
out there. On 2026-09-08 it was ``develop``, four commits ahead of ``main``.

CLAUDE.md states the intended model plainly — "**`main` is production.** The
launchd jobs, the paper book, and the live IB account all run from what is on
`main`" — so this was a configuration defect, not a documentation one.

WHY IT MATTERS
--------------
There was no promotion gate at all. Merging any PR to ``develop`` deployed to
the live host immediately; promotion to ``main`` changed nothing on the host, so
the gate was decorative; and the post-merge ``develop`` build — the check
CLAUDE.md identifies as the one that catches two individually-green PRs
conflicting once both have landed — ran *after* the code was already live.

On 2026-09-08 four PRs (KAN-65, KAN-69, KAN-70, KAN-71) went from merge to
running on the paper-trading host with no promotion step between. They were
fixes to things already broken, so the outcome was fine; the missing gate was
not noticed at the time.

It was also silently reversible by accident: ``git checkout`` of any branch in
that directory, for any reason, re-pointed production, and nothing warned.

WHY A TEST AND NOT JUST A DOC
-----------------------------
The default is a string in seven files. Repointing one of them back — or adding
an eighth wrapper that copies the old line — restores the defect with no symptom
until the next divergence between the branches. That is precisely the class of
drift this repo keeps rediscovering, so it gets an assertion rather than a
paragraph.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEPLOY_DIR = REPO / "deploy" / "launchd"
README = DEPLOY_DIR / "README.md"

#: The clone that exists only to be deployed from. Nobody develops in it, it
#: tracks main, and a promotion followed by `git pull` + deploy.sh is the only
#: thing that changes its content.
DEPLOY_CLONE = "/Users/huiliang/algo-poc-deploy"

#: The interactive checkout. Feature work happens here and in its .worktrees/,
#: which is exactly why production must not read from it.
DEV_CHECKOUT = "/Users/huiliang/GitHub/algo-poc"

_ALGO_DIR_DEFAULT = re.compile(r'^ALGO_DIR="\$\{ALGO_DIR:-([^}]*)\}"', re.MULTILINE)


def _wrappers() -> list[Path]:
    return sorted(
        p for p in DEPLOY_DIR.glob("*.sh")
        if _ALGO_DIR_DEFAULT.search(p.read_text())
    )


def test_there_are_wrappers_to_check() -> None:
    """The regex is the whole test; if it stops matching, every assertion below
    passes vacuously."""
    names = {p.name for p in _wrappers()}
    assert names >= {
        "run_paper.sh",
        "run_divergence.sh",
        "run_backtest_refresh.sh",
        "run_pipeline_report.sh",
        "gateway_watchdog.sh",
        "run_db_backup.sh",
        "run_evidence_digest.sh",
    }, f"wrappers with an ALGO_DIR default: {sorted(names)}"


def test_every_wrapper_defaults_to_the_deploy_clone() -> None:
    """AC1, AC5."""
    wrong = {
        p.name: _ALGO_DIR_DEFAULT.search(p.read_text()).group(1)
        for p in _wrappers()
        if _ALGO_DIR_DEFAULT.search(p.read_text()).group(1) != DEPLOY_CLONE
    }
    assert not wrong, (
        f"wrappers not pointed at the deploy clone {DEPLOY_CLONE}: {wrong}"
    )


def test_no_launchd_file_names_the_development_checkout() -> None:
    """AC5, stated the other way round so the failure names the actual mistake.

    Not redundant with the test above: that one only sees the column-0
    ``ALGO_DIR="${ALGO_DIR:-…}"`` form in ``deploy/launchd/*.sh``. This one
    searches every non-doc file a launchd job executes or reads — wrappers,
    ``lib/``, plists, and ``ops/launchd/`` — for the dev path in *any* form,
    including a hardcoded ``ALGO_DIR="…"`` (the sentiment wrapper's shape) or a
    fallback like ``secrets.sh``'s env-file default.
    """
    offenders = sorted(
        str(p.relative_to(REPO))
        for root in (DEPLOY_DIR, REPO / "ops")
        for p in root.rglob("*")
        if p.is_file() and p.suffix != ".md" and DEV_CHECKOUT in p.read_text()
    )
    assert not offenders, (
        f"{offenders} name the development checkout — production would run "
        "whatever branch was last left checked out there"
    )


# ---------------------------------------------------------------------------
# AC4 — the README must say which files are live on `git pull` and which need
# deploy.sh. Getting this backwards on 2026-09-08 produced a "deploy.sh ran,
# everything in sync" that was true and meaningless: it had synced against a
# checkout three commits stale, and copied nothing.
# ---------------------------------------------------------------------------


_LIVE_SECTION = "## What is live on `git pull`, and what needs `deploy.sh`"
_SOURCED_HEADING = "### Sourced by path from the tree"
_COPIED_HEADING = "### Copied to `~/ibc`"


def _subsection(section: str, heading: str) -> str:
    """One ``###`` block of the section, so a file listed in the wrong table —
    or mentioned only in passing elsewhere — does not satisfy the check."""
    start = section.index(heading)
    rest = section[start + len(heading):]
    end = rest.find("\n### ")
    return rest[: end if end != -1 else len(rest)]


def _table_names(block: str) -> set[str]:
    """The first-column file names of a markdown table, basename only."""
    return {
        Path(m).name
        for m in re.findall(r"^\| `([^`]+)` \|", block, re.MULTILINE)
    }


def _readme_section(title: str) -> str:
    text = README.read_text()
    start = text.index(title)
    rest = text[start + len(title):]
    end = rest.find("\n## ")
    return rest[: end if end != -1 else len(rest)]


def test_the_readme_documents_the_sourced_versus_copied_split() -> None:
    """AC4."""
    section = _readme_section(_LIVE_SECTION)
    assert "sourced by path" in section.lower(), section[:400]
    assert "~/ibc" in section, section[:400]


def test_the_readme_lists_every_file_deploy_sh_refuses_to_copy() -> None:
    """AC4, and the half that rots first.

    ``deploy.sh`` skips secrets.sh and deadman.sh by name and never walks
    ``lib/`` at all, so those are live the moment the clone is pulled. A README
    that omits one of them is how an operator concludes a change did not take
    effect and edits the ~/ibc decoy instead.
    """
    listed = _table_names(_subsection(_readme_section(_LIVE_SECTION), _SOURCED_HEADING))
    deploy_sh = (DEPLOY_DIR / "deploy.sh").read_text()

    skipped = set(re.findall(r'basename "\$f"\)" = "([^"]+)" \] && continue', deploy_sh))
    assert skipped, "deploy.sh's skip rules no longer match the regex"
    skipped.discard("deploy.sh")   # the deployer itself, not a sourced lib
    sourced = skipped | {p.name for p in (DEPLOY_DIR / "lib").glob("*.sh")}

    missing = sorted(sourced - listed)
    assert not missing, (
        f"the README's sourced-by-path list omits {missing} — deploy.sh does "
        "not copy them, so they go live the moment the deploy clone is pulled"
    )


def test_the_readme_lists_every_wrapper_deploy_sh_does_copy() -> None:
    """AC4, the other half. These need deploy.sh; a `git pull` alone leaves the
    running copy in ~/ibc untouched."""
    listed = _table_names(_subsection(_readme_section(_LIVE_SECTION), _COPIED_HEADING))
    deploy_sh = (DEPLOY_DIR / "deploy.sh").read_text()
    skipped = set(re.findall(r'basename "\$f"\)" = "([^"]+)" \] && continue', deploy_sh))
    skipped.add("deploy.sh")

    copied = {p.name for p in DEPLOY_DIR.glob("*.sh") if p.name not in skipped}
    missing = sorted(copied - listed)
    assert not missing, (
        f"the README's copied-to-~/ibc list omits {missing}"
    )


def test_the_readme_states_the_deployment_procedure() -> None:
    """AC3. Pull-then-deploy, in that order. Running deploy.sh first is what
    produced the meaningless "in sync" on 2026-09-08.

    Judged on the command lines themselves, not on prose or comments that
    happen to mention both — swapping the two commands must fail this.
    """
    section = _readme_section(_LIVE_SECTION)
    commands = [ln.strip() for ln in section.splitlines()]
    pull = next((i for i, ln in enumerate(commands) if ln.startswith("git pull")), -1)
    deploy = next(
        (i for i, ln in enumerate(commands)
         if ln.startswith("deploy/launchd/deploy.sh") and "--dry-run" not in ln),
        -1,
    )
    assert pull != -1, "the README does not show the `git pull` command"
    assert deploy != -1, "the README does not show the `deploy.sh` command"
    assert pull < deploy, (
        "the README must show `git pull` before `deploy.sh` — deploy.sh run "
        "against a stale clone reports 'in sync' and copies nothing"
    )


def test_the_readme_rebuilds_the_docker_services_from_the_clone_under_the_pinned_project() -> None:
    """AC3, the third half. ``services/*`` run in Docker, so neither a pull nor
    deploy.sh moves them — the 2026-09-17 fix sat undeployed that way.

    The rebuild must name the compose project. Compose defaults the project to
    the directory's basename, so ``docker compose up`` in ``algo-poc-deploy``
    creates a *new* project, ``algo-poc-deploy``, with new, empty ``pgdata`` and
    ``redisdata`` volumes — a paper book with no history. ``-p algo-poc`` is what
    keeps the clone driving the existing stack and volumes. And ``--build`` alone
    leaves running containers on the old image; ``--force-recreate`` is needed.
    """
    section = _readme_section(_LIVE_SECTION)
    assert "docker compose -p algo-poc" in section, (
        "the README must rebuild the services with `-p algo-poc`; without it, "
        "compose run from the deploy clone starts a fresh project on empty volumes"
    )
    assert "--force-recreate" in section, section[-600:]
    # And the watchdog must look for the same project the README tells the
    # operator to start, or a correct rebuild reads as a missing stack.
    docker_health = (DEPLOY_DIR / "lib" / "docker_health.sh").read_text()
    assert 'ALGO_COMPOSE_PROJECT="${ALGO_COMPOSE_PROJECT:-algo-poc}"' in docker_health


def test_the_docker_rebuild_cannot_drop_the_ib_account_pin() -> None:
    """``docker-compose.yml`` interpolates ``ALGO_IB_ACCOUNT_ID=${…:-}``, and
    the dev checkout supplies it from its 1Password ``.env``. The deploy clone
    has no ``.env`` and ``secrets.sh --export`` does not emit it (it is not a
    secret), so a rebuild from the clone that does not set it recreates
    ``execution`` with an empty pin — silently, since an empty pin reads as
    "unpinned". The README must set it and refuse to go on without it.
    """
    compose = (REPO / "docker-compose.yml").read_text()
    assert "ALGO_IB_ACCOUNT_ID=${ALGO_IB_ACCOUNT_ID:-}" in compose, (
        "compose no longer interpolates the pin this way — revisit this test"
    )
    section = _readme_section(_LIVE_SECTION)
    assert ': "${ALGO_IB_ACCOUNT_ID:?' in section, (
        "the README's docker rebuild must fail when ALGO_IB_ACCOUNT_ID is unset"
    )


def test_the_container_runbook_builds_from_the_deploy_clone() -> None:
    """The container runbook is the other place that builds risk/execution
    images. Left pointing at the dev checkout, it would make that tree — on
    whatever branch — the build source for the most safety-critical code."""
    runbook = (REPO / "docs" / "operations" / "container-deploy.md").read_text()
    assert "cd ~/GitHub/algo-poc" not in runbook
    assert DEV_CHECKOUT not in runbook
    assert "docker compose -p algo-poc" in runbook
    for bare in re.findall(r"^\s*(?:time )?docker compose (?!-p algo-poc)\S.*$", runbook, re.MULTILINE):
        raise AssertionError(f"runbook runs compose without -p algo-poc: {bare!r}")


def test_the_readme_documents_bootstrapping_the_deploy_clone() -> None:
    """The clone's host state — a venv built in it, the gitignored divergence
    baseline, the cut-over order — must not live only in a PR body."""
    section = _readme_section(_LIVE_SECTION)
    assert f"git clone --branch main" in section and DEPLOY_CLONE in section
    # Built in the clone, pinned like CI. A copied dev venv's .pth would import
    # the dev checkout's code while everything else ran from the clone.
    assert "requirements-dev.lock" in section
    # baseline_age.sh ages the pin by mtime: a plain cp resets the clock.
    assert "cp -p" in section
