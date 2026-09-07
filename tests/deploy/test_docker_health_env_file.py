"""``algo_docker_expected_services`` must never read the project ``.env``.

The repo ``.env`` is a named pipe, permanently and on purpose: 1Password
Environments serves it, and KAN-16 moved every shell loader off it after the
2026-08-13 outage, where reading a FIFO nothing was serving blocked and then
returned nothing. One consumer was missed.

``docker compose config`` reads the project ``.env`` to interpolate, so
``algo_docker_expected_services`` inherited that failure whole. Its output is
piped through ``2>/dev/null``, so the block ends in an empty result and the
caller takes the "could not read" branch at ``docker_health.sh:190``. From
2026-08-27 — the day after KAN-66 shipped the detector — every scheduled cycle
took that branch: 144 of 144 on 2026-09-02, against a ``StartInterval`` of 300
that should give 288. Exactly half, because launchd will not start a second
instance while the first is still blocked, so the five-minute Gateway watchdog
had quietly become a ten-minute one.

**Why the existing suite is green through all of that.** The docker stub in
``test_gateway_watchdog.py`` answers ``compose`` by echoing
``$STUB_DOCKER_SERVICES``. It never opens ``.env``, so it cannot reproduce the
one behaviour that matters here, and ``test_a_service_with_no_container_at_all_is_named``
passes against a detector that has never once run in production.

So the stub in this module is deliberately *less* convenient and more faithful:
absent ``--env-file`` it reads ``./.env`` the way the real binary does, which
means a fixture whose ``.env`` is an unserved FIFO reproduces the production
hang inside pytest. Nothing here needs a docker daemon.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DOCKER_HEALTH = REPO / "deploy/launchd/lib/docker_health.sh"
LAUNCHD = REPO / "deploy/launchd"

SERVICES = (
    "redis postgres migrate data-ingestion signal-generation ml-model "
    "risk-management api notifications execution portfolio-accounting"
).split()

# Long enough that a working call is never mistaken for a hang on a loaded CI
# box, short enough that the real defect (minutes) cannot hide under it.
BOUND_SECONDS = 20


def _fake_docker(bin_dir: Path, *, argv_log: Path, require_secret: bool = False) -> None:
    """A ``docker`` that models the one behaviour the real one has here.

    Without ``--env-file`` it reads ``./.env`` before answering, exactly as
    ``docker compose config`` does — so an unserved FIFO blocks it. With
    ``--env-file`` it does not, and answers from the environment.
    """
    secret_check = (
        '      if [ -z "${POSTGRES_PASSWORD:-}" ]; then\n'
        '        echo "required variable POSTGRES_PASSWORD is missing a value" >&2\n'
        "        exit 1\n"
        "      fi\n"
        if require_secret
        else ""
    )
    script = (
        "#!/bin/bash\n"
        f'printf "%s\\n" "$*" >> "{argv_log}"\n'
        'case "${1:-}" in\n'
        "  info) exit 0 ;;\n"
        "  compose)\n"
        # The real binary interpolates from ./.env unless told otherwise.
        '      case "$*" in\n'
        "        *--env-file*) ;;\n"
        '        *) [ -e .env ] && cat .env >/dev/null 2>&1 ;;\n'
        "      esac\n"
        f"{secret_check}"
        f'      printf "%s\\n" {" ".join(SERVICES)} ;;\n'
        "  *) exit 1 ;;\n"
        "esac\n"
    )
    path = bin_dir / "docker"
    path.write_text(script)
    path.chmod(0o755)


def _call_expected_services(project: Path, bin_dir: Path) -> subprocess.CompletedProcess:
    """Source the shipped lib and run the helper against `project`."""
    body = (
        f'ALGO_DIR="{project}"\n'
        f'ALGO_DOCKER_BIN="{bin_dir / "docker"}"\n'
        f'. "{DOCKER_HEALTH}"\n'
        "algo_docker_expected_services\n"
    )
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}")
    return subprocess.run(
        ["bash", "-c", body], capture_output=True, text=True,
        timeout=BOUND_SECONDS, env=env,
    )


@pytest.fixture()
def project(tmp_path) -> Path:
    """A project dir whose .env is a FIFO nothing is serving — the live shape."""
    d = tmp_path / "repo"
    d.mkdir()
    (d / "docker-compose.yml").write_text("services:\n  redis:\n    image: redis\n")
    os.mkfifo(d / ".env")
    return d


@pytest.fixture()
def bin_dir(tmp_path) -> Path:
    d = tmp_path / "bin"
    d.mkdir()
    return d


def test_an_unserved_env_fifo_does_not_block_the_helper(project, bin_dir, tmp_path):
    """The defect itself. Against current code this call never returns and the
    test fails on timeout — which is precisely what launchd experienced."""
    argv_log = tmp_path / "argv"
    _fake_docker(bin_dir, argv_log=argv_log)
    res = _call_expected_services(project, bin_dir)
    assert res.stdout.split() == SERVICES, res


def test_the_compose_invocation_passes_env_file(project, bin_dir, tmp_path):
    """Pin the mechanism, not just the symptom: a future refactor that drops the
    flag reintroduces a multi-minute stall on a job that must never stall."""
    argv_log = tmp_path / "argv"
    _fake_docker(bin_dir, argv_log=argv_log)
    _call_expected_services(project, bin_dir)
    calls = argv_log.read_text().splitlines() if argv_log.exists() else []
    compose = [c for c in calls if c.startswith("compose")]
    assert compose, calls
    assert all("--env-file" in c for c in compose), compose


def test_the_env_file_precedes_the_subcommand(project, bin_dir, tmp_path):
    """`--env-file` is a top-level flag. After `config` docker rejects it, which
    would turn a silent hang into a silent parse failure — no improvement."""
    argv_log = tmp_path / "argv"
    _fake_docker(bin_dir, argv_log=argv_log)
    _call_expected_services(project, bin_dir)
    compose = [c for c in argv_log.read_text().splitlines() if c.startswith("compose")]
    for call in compose:
        parts = call.split()
        assert parts.index("--env-file") < parts.index("config"), call


def test_missing_secrets_still_degrade_to_empty_rather_than_a_false_list(
    project, bin_dir, tmp_path
):
    """A locked keychain must keep the documented behaviour — 'compare nothing',
    never a false healthy — and must now do it promptly instead of by blocking."""
    argv_log = tmp_path / "argv"
    _fake_docker(bin_dir, argv_log=argv_log, require_secret=True)
    res = _call_expected_services(project, bin_dir)
    assert res.stdout.strip() == "", res.stdout


def test_no_launchd_code_runs_compose_config_without_env_file():
    """The reverse drift. Any other consumer that grows a `compose config` call
    inherits the same FIFO block, and the failure is silent by construction."""
    offenders: list[str] = []
    for path in sorted(LAUNCHD.rglob("*.sh")):
        for n, line in enumerate(path.read_text().splitlines(), 1):
            stripped = line.strip()
            # Only actual invocations. The same words appear in comments and in
            # the WARNING body that reports the failure, and flagging those
            # would make the guard unfixable.
            if stripped.startswith("#") or "_algo_docker_log" in stripped:
                continue
            if not re.search(r"(\$ALGO_DOCKER_BIN|\bdocker)\S*[\"']?\s+compose\s", stripped):
                continue
            if re.search(r"compose\s+(?:\S+\s+)*?config\b", stripped) and "--env-file" not in stripped:
                offenders.append(f"{path.relative_to(REPO)}:{n}: {stripped}")
    assert not offenders, "compose config without --env-file reads the .env FIFO:\n" + "\n".join(offenders)
