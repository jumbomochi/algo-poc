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


def test_no_wrapper_points_at_the_development_checkout() -> None:
    """AC5, stated the other way round so the failure names the actual mistake.

    The two assertions are not redundant: the first would also fail on a typo'd
    path, this one fails specifically on the regression — someone pointing a
    wrapper back at the tree they develop in.
    """
    offenders = [
        p.name for p in DEPLOY_DIR.glob("*.sh")
        if f"ALGO_DIR:-{DEV_CHECKOUT}" in p.read_text()
    ]
    assert not offenders, (
        f"{offenders} run production out of the development checkout — "
        "whatever branch was last left checked out there is what trades"
    )


# ---------------------------------------------------------------------------
# AC4 — the README must say which files are live on `git pull` and which need
# deploy.sh. Getting this backwards on 2026-09-08 produced a "deploy.sh ran,
# everything in sync" that was true and meaningless: it had synced against a
# checkout three commits stale, and copied nothing.
# ---------------------------------------------------------------------------


def _readme_section(title: str) -> str:
    text = README.read_text()
    start = text.index(title)
    rest = text[start + len(title):]
    end = rest.find("\n## ")
    return rest[: end if end != -1 else len(rest)]


def test_the_readme_documents_the_sourced_versus_copied_split() -> None:
    """AC4."""
    section = _readme_section("## What is live on `git pull`, and what needs `deploy.sh`")
    assert "sourced by path" in section.lower(), section[:400]
    assert "~/ibc" in section, section[:400]


def test_the_readme_lists_every_file_deploy_sh_refuses_to_copy() -> None:
    """AC4, and the half that rots first.

    ``deploy.sh`` skips secrets.sh and deadman.sh by name and never walks
    ``lib/`` at all, so those are live the moment the clone is pulled. A README
    that omits one of them is how an operator concludes a change did not take
    effect and edits the ~/ibc decoy instead.
    """
    section = _readme_section("## What is live on `git pull`, and what needs `deploy.sh`")
    deploy_sh = (DEPLOY_DIR / "deploy.sh").read_text()

    skipped = set(re.findall(r'basename "\$f"\)" = "([^"]+)" \] && continue', deploy_sh))
    skipped.discard("deploy.sh")   # the deployer itself, not a sourced lib
    sourced = skipped | {p.name for p in (DEPLOY_DIR / "lib").glob("*.sh")}

    missing = sorted(name for name in sourced if name not in section)
    assert not missing, (
        f"the README's sourced-by-path list omits {missing} — deploy.sh does "
        "not copy them, so they go live the moment the deploy clone is pulled"
    )


def test_the_readme_lists_every_wrapper_deploy_sh_does_copy() -> None:
    """AC4, the other half. These need deploy.sh; a `git pull` alone leaves the
    running copy in ~/ibc untouched."""
    section = _readme_section("## What is live on `git pull`, and what needs `deploy.sh`")
    deploy_sh = (DEPLOY_DIR / "deploy.sh").read_text()
    skipped = set(re.findall(r'basename "\$f"\)" = "([^"]+)" \] && continue', deploy_sh))
    skipped.add("deploy.sh")

    copied = sorted(p.name for p in DEPLOY_DIR.glob("*.sh") if p.name not in skipped)
    missing = [name for name in copied if name not in section]
    assert not missing, (
        f"the README's copied-to-~/ibc list omits {missing}"
    )


def test_the_readme_states_the_deployment_procedure() -> None:
    """AC3. Pull-then-deploy, in that order. Running deploy.sh first is what
    produced the meaningless "in sync" on 2026-09-08."""
    section = _readme_section("## What is live on `git pull`, and what needs `deploy.sh`")
    pull = section.find("git pull")
    deploy = section.find("deploy.sh", pull if pull != -1 else 0)
    assert pull != -1, "the README does not name the pull step"
    assert deploy > pull, (
        "the README must show `git pull` before `deploy.sh` — deploy.sh run "
        "against a stale clone reports 'in sync' and copies nothing"
    )
