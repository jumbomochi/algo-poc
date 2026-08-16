"""Guards on the container deploy runbook (docs/operations/container-deploy.md).

The runbook is executable documentation: an operator copy-pastes its commands
into a shell pointed at the live paper stack. That makes its command blocks
production artifacts, and they can rot in ways prose review does not catch:

* a compose service renamed in ``docker-compose.yml`` leaves the runbook
  addressing a service that no longer exists — ``docker compose up`` then
  errors out mid-deploy, or worse, a stale name silently matches nothing;
* a ``docker compose up`` that loses ``--force-recreate`` reproduces the
  2026-08-07 failure exactly — the build succeeds, the containers keep running
  the old image, and the deploy reports success while changing nothing;
* the cold-reboot check counts containers and launchd jobs, so adding a
  service or a plist without touching the runbook turns "all present" into a
  check that passes while something is missing.

These tests pin the runbook to the tree it describes.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

RUNBOOK = Path("docs/operations/container-deploy.md")
COMPOSE = Path("docker-compose.yml")
OPS_INDEX = Path("docs/operations/README.md")
LAUNCHD_DIR = Path("deploy/launchd")

# Compose subcommands whose positional arguments are all service names.
SERVICE_ARG_SUBCOMMANDS = {
    "build",
    "create",
    "images",
    "logs",
    "ps",
    "pull",
    "restart",
    "start",
    "stop",
    "up",
}
# Subcommands where only the FIRST positional is a service (the rest is the
# command run inside the container).
FIRST_ARG_SUBCOMMANDS = {"exec", "run"}
# Flags that consume the following token as their value.
VALUE_FLAGS = {
    "-f",
    "--file",
    "-p",
    "--project-name",
    "--env-file",
    "--tail",
    "--format",
    "--timeout",
    "-t",
    "--status",
    "--index",
    "--scale",
}


def _bash_blocks(text: str) -> list[str]:
    """Return the contents of every fenced ```bash block."""
    return re.findall(r"```bash\n(.*?)```", text, flags=re.DOTALL)


def _compose_commands(text: str) -> list[list[str]]:
    """Return the token list following each ``docker compose`` in the runbook.

    Only fenced bash blocks are scanned — prose that mentions a command is not
    something an operator pastes. Substitution/quoting punctuation is stripped
    so ``$(docker compose images --format json execution)`` parses like a bare
    call; ``$`` survives so a shell variable stays recognisable as one.
    """
    commands: list[list[str]] = []
    for block in _bash_blocks(text):
        for raw in block.splitlines():
            line = raw.split("#", 1)[0]
            line = line.translate(str.maketrans("", "", "(){}`'\"\\"))
            for fragment in re.split(r"\||&&|;", line):
                if "docker compose" not in fragment:
                    continue
                tail = fragment.split("docker compose", 1)[1]
                commands.append(tail.split())
    return commands


def _services_addressed(text: str) -> set[str]:
    """Every literal compose service name the runbook's commands address.

    Shell variables (``"$svc"`` inside a loop) are skipped — their value is not
    knowable here, and the loop's literal service list is checked wherever the
    runbook spells the names out.
    """
    services: set[str] = set()
    for tokens in _compose_commands(text):
        subcommand = None
        positionals: list[str] = []
        skip_next = False
        for token in tokens:
            if skip_next:
                skip_next = False
                continue
            if token.startswith("-"):
                if token in VALUE_FLAGS:
                    skip_next = True
                continue
            if subcommand is None:
                subcommand = token
                continue
            if token.startswith("$"):
                continue
            positionals.append(token)
        if subcommand in SERVICE_ARG_SUBCOMMANDS:
            services.update(positionals)
        elif subcommand in FIRST_ARG_SUBCOMMANDS and positionals:
            services.add(positionals[0])
    return services


def _compose_services() -> dict[str, dict]:
    return yaml.safe_load(COMPOSE.read_text())["services"]


def test_runbook_targets_service_names_that_exist_in_compose() -> None:
    """A renamed service (or the underscore spelling) must fail here.

    ``risk_management`` is the directory and the Python package; the compose
    service is ``risk-management``. Addressing the underscore form is the most
    likely way this runbook silently stops working.
    """
    addressed = _services_addressed(RUNBOOK.read_text())
    known = set(_compose_services())

    assert addressed, "runbook has no parseable `docker compose` service targets"
    assert addressed <= known, (
        f"runbook addresses services absent from {COMPOSE}: "
        f"{sorted(addressed - known)}"
    )


def test_runbook_names_both_tranche_one_services() -> None:
    """The deploy is for the two services tranche 1 changed."""
    addressed = _services_addressed(RUNBOOK.read_text())

    assert {"risk-management", "execution"} <= addressed


def test_every_container_start_in_the_runbook_forces_recreate() -> None:
    """Dropping ``--force-recreate`` is the 2026-08-07 failure, re-committed.

    ``docker compose up -d --build`` rebuilds the image and leaves the running
    container on the previous one. Every ``up`` in this runbook starts
    containers whose code just changed, so every one of them needs the flag.
    """
    offenders = [
        " ".join(tokens)
        for tokens in _compose_commands(RUNBOOK.read_text())
        if tokens
        and tokens[0] == "up"
        and "--force-recreate" not in tokens
    ]

    assert not offenders, (
        "`docker compose up` without --force-recreate in the runbook: " f"{offenders}"
    )


def test_no_pasteable_command_removes_the_postgres_volume() -> None:
    """The paper book lives in the pgdata volume and cannot be recreated.

    Only fenced command blocks are checked. Prose *warning* an operator off
    ``down -v`` is the opposite of a defect — the runbook says so explicitly.
    """
    blocks = "\n".join(_bash_blocks(RUNBOOK.read_text()))
    for banned in ("down -v", "down --volumes", "docker volume rm", "--remove-orphans"):
        assert banned not in blocks, (
            f"destructive command in a pasteable deploy block: {banned}"
        )


def test_cold_reboot_check_counts_every_long_running_container() -> None:
    """Adding a service must update the cold-reboot count, or the check lies."""
    match = re.search(r"\*\*(\d+) long-running containers\*\*", RUNBOOK.read_text())
    assert match, "runbook must state the expected container count as **N long-running containers**"

    expected = [
        name
        for name, spec in _compose_services().items()
        if str(spec.get("restart", "")) != "no"
    ]
    assert int(match.group(1)) == len(expected), (
        f"runbook says {match.group(1)} containers; compose defines "
        f"{len(expected)}: {sorted(expected)}"
    )


def test_cold_reboot_check_counts_every_launchd_job() -> None:
    """Adding a plist must update the cold-reboot count, or the check lies."""
    match = re.search(r"\*\*(\d+) launchd jobs\*\*", RUNBOOK.read_text())
    assert match, "runbook must state the expected job count as **N launchd jobs**"

    plists = sorted(p.name for p in LAUNCHD_DIR.glob("*.plist"))
    assert int(match.group(1)) == len(plists), (
        f"runbook says {match.group(1)} launchd jobs; {LAUNCHD_DIR} holds "
        f"{len(plists)}: {plists}"
    )


def test_runbook_records_the_image_hash_and_rollback_steps() -> None:
    """The two acceptance criteria that turn "we deployed" into evidence."""
    text = RUNBOOK.read_text()

    assert "docker compose images" in text, "no image-hash capture step"
    assert "docker tag" in text, "no pre-deploy retag, so no one-command rollback"


def test_operations_index_links_the_container_deploy_runbook() -> None:
    """An unlinked runbook is one an operator does not find under pressure."""
    assert "container-deploy.md" in OPS_INDEX.read_text()
