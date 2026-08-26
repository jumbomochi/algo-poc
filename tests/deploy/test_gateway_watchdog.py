"""The gateway watchdog's three 2026-08-21 defects, driven end-to-end in bash.

Every test here runs the *shipped* ``deploy/launchd/gateway_watchdog.sh`` — not
a paraphrase of it — against stubbed ``nc`` / ``docker`` / ``ps`` / ``curl`` /
``security`` / ``launchctl`` binaries, and asserts on the Telegram body actually
sent. The three subjects:

**KAN-66 — nothing watched the Docker engine.** On 2026-08-20 the engine died
while Docker Desktop's Electron GUI stayed alive: ``docker ps`` failed, every
container was gone, and any check built on a process name would have reported
healthy. Three jobs failed the next morning, each accurate about its own
symptom, none naming the cause.

**KAN-63 — the Error 1100 latch outlived its outage.** The latch is written by
the execution service and can only be removed by it; execution runs in a
container; the container died inside the same fault. The watchdog then spent
20h07m re-alerting an outage that had already ended, across a full Gateway
process replacement.

**KAN-62 — the escalation half.** A flat 12h re-alert meant the operator's last
warning before the 04:15 run was 4h16m stale, and the auth branch cleared a
two-strike marker belonging to the kickstart path.

The clock is injected (``ALGO_NOW_EPOCH``) so the escalating cadence is asserted
by driving it rather than by waiting for 04:15 to come round.
"""

from __future__ import annotations

import os
import plistlib
import re
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
WATCHDOG = REPO / "deploy/launchd/gateway_watchdog.sh"
DOCKER_HEALTH = REPO / "deploy/launchd/lib/docker_health.sh"
PAPER_PLIST = REPO / "deploy/launchd/local.algo-paper-trading.plist"
README = REPO / "deploy/launchd/README.md"

# A Tuesday, so 04:15 is a real scheduled run. Naive .timestamp() is local time,
# which is the same clock the shell derives its hour-of-day from.
RUN_AT = datetime(2026, 8, 25, 4, 15, 0).timestamp()

# Every algo-poc compose service, verbatim from `docker compose config
# --services` on the operator host — hyphens, not underscores, and including the
# one-shot `migrate`. Used to build the stub's expected-vs-running comparison.
SERVICES = (
    "redis postgres migrate data-ingestion signal-generation ml-model "
    "risk-management api notifications execution portfolio-accounting"
).split()

# `migrate` runs `alembic upgrade head` at stack start and then sits at
# "Exited (0)" for the life of the stack. It is healthy in that state.
ONESHOT = "migrate"


class Host:
    """One fake operator host the watchdog can be run against, repeatedly."""

    def __init__(self, tmp_path: Path) -> None:
        self.home = tmp_path / "home"
        (self.home / "ibc" / "logs").mkdir(parents=True)
        (self.home / "ibc" / "state").mkdir(parents=True)
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.curl_log = tmp_path / "curl.log"
        self.launchctl_log = tmp_path / "launchctl.log"

        # Defaults describe a completely healthy host: port up, daemon up,
        # every service running, Gateway alive for a day.
        self.port_up = True
        self.docker_info_ok = True
        self.containers = {s: ("running", "Up 3 hours") for s in SERVICES}
        self.containers[ONESHOT] = ("exited", "Exited (0) 6 days ago")
        self.gateway_pid = "58305"
        self.gateway_etime = "24:00:00"  # 1 day
        self.ibc_log_text = "12:00:00 Login has completed\n"
        self.now = RUN_AT
        self.disable_docker = False

        self._write_stubs()

    # -- paths the watchdog uses -------------------------------------------
    @property
    def conn_marker(self) -> Path:
        return self.home / "ibc" / "state" / "gateway_connectivity_lost"

    @property
    def conn_alert_marker(self) -> Path:
        return self.home / "ibc" / ".gateway_connectivity_alerted"

    @property
    def docker_alert_marker(self) -> Path:
        return self.home / "ibc" / ".docker_stack_alerted"

    @property
    def down_marker(self) -> Path:
        return self.home / "ibc" / ".gateway_down_marker"

    @property
    def auth_marker(self) -> Path:
        return self.home / "ibc" / ".gateway_auth_failure_alerted"

    def watchdog_log(self) -> str:
        logs = sorted((self.home / "ibc" / "logs").glob("gateway_watchdog_*.log"))
        return "\n".join(p.read_text() for p in logs)

    def alerts_log(self) -> str:
        path = self.home / "ibc" / "logs" / "ALERTS.log"
        return path.read_text() if path.exists() else ""

    # -- stubs -------------------------------------------------------------
    def _stub(self, name: str, body: str) -> Path:
        path = self.bin / name
        path.write_text("#!/bin/bash\n" + body)
        path.chmod(0o755)
        return path

    def _write_stubs(self) -> None:
        # The port probe. `nc -z -G 3 127.0.0.1 7497`.
        self._stub("nc", 'exit "${STUB_PORT_UP:-0}"\n')

        # `ps -p <pid> -o etime=`. Deliberately also answers a bare `ps` with
        # the exact 2026-08-21 process table: Docker Desktop and vmnetd RUNNING
        # while the daemon is dead. Any liveness check that matched on a process
        # name would call that healthy, so the fixture makes the wrong answer
        # available and the assertion is that nothing takes it.
        self._stub(
            "ps",
            'if [ "${1:-}" = "-p" ]; then\n'
            '    [ "$2" = "$STUB_GW_PID" ] && echo "  $STUB_GW_ETIME"\n'
            "    exit 0\n"
            "fi\n"
            "cat <<'EOF'\n"
            "19847 /Applications/Docker.app/Contents/MacOS/Docker Desktop\n"
            "19851 Docker Desktop Helper (Renderer)\n"
            "  827 /Library/PrivilegedHelperTools/com.docker.vmnetd\n"
            "EOF\n",
        )

        # docker: info / ps / compose config --services.
        self._stub(
            "docker",
            "case \"${1:-}\" in\n"
            '  info) exit "${STUB_DOCKER_INFO:-0}" ;;\n'
            '  ps) [ "${STUB_DOCKER_INFO:-0}" = 0 ] || exit 1\n'
            '      case "$*" in\n'
            '        *\'{{.Label "com.docker.compose.service"}}\'*) ;;\n'
            '        *--format*) echo "STUB: unusable --format: $*" >&2; exit 1 ;;\n'
            "      esac\n"
            '      cat "$STUB_DOCKER_PS_FILE" ;;\n'
            '  compose) [ "${STUB_DOCKER_INFO:-0}" = 0 ] || exit 1\n'
            '      tr " " "\\n" <<< "$STUB_DOCKER_SERVICES" ;;\n'
            "  *) exit 1 ;;\n"
            "esac\n",
        )

        self._stub("curl", 'printf "%s\\n" "$*" >> "$STUB_CURL_LOG"\nexit 0\n')
        self._stub("osascript", "exit 0\n")

        # A keychain that resolves, so telegram() takes the real send path and
        # the assertions are on the message body rather than on a degraded
        # local-alert fallback.
        self._stub(
            "security",
            'case "$*" in\n'
            '  *TELEGRAM_BOT_TOKEN*) echo "bot-token" ;;\n'
            '  *TELEGRAM_CHAT_ID*) echo "chat-id" ;;\n'
            '  *) echo "could not be found" >&2; exit 44 ;;\n'
            "esac\n",
        )

        self._launchctl = self._stub(
            "launchctl-stub",
            'printf "%s\\n" "$*" >> "$STUB_LAUNCHCTL_LOG"\n'
            'if [ "${1:-}" = "list" ]; then\n'
            '    echo "PID	Status	Label"\n'
            '    echo "-	0	local.algo-paper-trading"\n'
            '    [ -n "$STUB_GW_PID" ] && echo "$STUB_GW_PID	0	local.ibc-gateway"\n'
            "fi\n"
            "exit 0\n",
        )

    # -- driving -----------------------------------------------------------
    def run(self) -> subprocess.CompletedProcess:
        ps_file = self.bin.parent / "docker_ps.txt"
        ps_file.write_text(
            "".join(
                f"{svc}|{state}|{status}\n"
                for svc, (state, status) in self.containers.items()
            )
        )
        # The IBC log the auth-failure branch greps. `ls -t ibc-*.txt | head -1`.
        (self.home / "ibc" / "logs" / "ibc-3.23.0_GATEWAY-10.43_Tuesday.txt").write_text(
            self.ibc_log_text
        )

        env = dict(
            os.environ,
            HOME=str(self.home),
            ALGO_DIR=str(REPO),
            ALGO_PATH_PREFIX=str(self.bin),
            ALGO_NOW_EPOCH=str(int(self.now)),
            ALGO_LAUNCHCTL_BIN=str(self._launchctl),
            ALGO_SECURITY_BIN=str(self.bin / "security"),
            ALGO_OSASCRIPT_BIN=str(self.bin / "osascript"),
            STUB_PORT_UP="0" if self.port_up else "1",
            STUB_DOCKER_INFO="0" if self.docker_info_ok else "1",
            STUB_DOCKER_PS_FILE=str(ps_file),
            STUB_DOCKER_SERVICES=" ".join(SERVICES),
            STUB_GW_PID=self.gateway_pid,
            STUB_GW_ETIME=self.gateway_etime,
            STUB_CURL_LOG=str(self.curl_log),
            STUB_LAUNCHCTL_LOG=str(self.launchctl_log),
        )
        if self.disable_docker:
            # An explicitly empty $ALGO_DOCKER_BIN is the documented "this host
            # has no container runtime" switch. Deleting the stub alone would
            # not do it: the real /usr/local/bin/docker is still on PATH.
            env["ALGO_DOCKER_BIN"] = ""
        return subprocess.run(
            [str(WATCHDOG)], capture_output=True, text=True, timeout=120,
            env=env, cwd=str(REPO),
        )

    def run_confirmed(self) -> None:
        """Two cycles, 300s apart — what the stack check needs before it pages.

        The first pass is a strike, not an alert: a `docker compose up` shows
        containers at "(health: starting)" for a few seconds and a Docker
        Desktop restart makes `docker info` fail for about half a minute, and
        paging for either is how an alert gets ignored on the day it matters.
        """
        self.run()
        self.now += 300
        self.run()

    def messages(self) -> list[str]:
        if not self.curl_log.exists():
            return []
        return [ln for ln in self.curl_log.read_text().splitlines() if ln.strip()]

    def clear_messages(self) -> None:
        self.curl_log.write_text("")

    def launchctl_calls(self) -> list[str]:
        if not self.launchctl_log.exists():
            return []
        return self.launchctl_log.read_text().splitlines()

    def age_marker(self, marker: Path, seconds: int) -> None:
        """Make `marker` look `seconds` old relative to the injected clock."""
        stamp = self.now - seconds
        os.utime(marker, (stamp, stamp))

    def gateway_uptime(self) -> int:
        """`ps -o etime=` seconds, the way the watchdog parses them."""
        days, _, rest = self.gateway_etime.rpartition("-")
        parts = [int(p) for p in rest.split(":")]
        h, m, s = ([0] + parts)[-3:]
        return (int(days) if days else 0) * 86400 + h * 3600 + m * 60 + s

    def write_conn_marker(self, *, lost_ago: int, pid: str | None = None,
                          started_ago: int | None = None) -> None:
        """Write a marker. `pid=None` leaves it unstamped (what execution
        writes); "live" stamps it for the *currently running* Gateway, derived
        from its elapsed time so the two can never disagree by construction.
        """
        body = f"{int(self.now - lost_ago)}\nwriter=execution\n"
        if pid == "live":
            pid, started_ago = self.gateway_pid, self.gateway_uptime()
        if pid is not None and started_ago is not None:
            body += f"gateway_pid={pid}\ngateway_started_at={int(self.now - started_ago)}\n"
        self.conn_marker.write_text(body)


@pytest.fixture()
def host(tmp_path) -> Host:
    return Host(tmp_path)


# ---------------------------------------------------------------------------
# KAN-66 — docker engine + stack liveness
# ---------------------------------------------------------------------------

def test_a_dead_daemon_is_unhealthy_even_with_the_desktop_processes_present(host):
    """AC2, the exact 2026-08-21 state.

    The `ps` stub reports Docker Desktop, its Renderer helper and vmnetd all
    RUNNING — the process table as verified at 08:25 that morning — while
    `docker info` fails. A check keyed on process names calls this healthy.
    """
    host.docker_info_ok = False
    host.run_confirmed()

    body = " ".join(host.messages())
    assert "docker daemon is not responding" in body, body
    assert "docker info" in body
    # And it must not have been talked out of it by the live GUI processes.
    assert "Docker Desktop" not in body


def test_a_dead_daemon_alerts_locally_as_well_as_on_telegram(host):
    """AC1: through the existing path — algo_alert_local *plus* telegram.

    A dead engine is exactly the case where the Telegram path may itself be
    unreachable, so the credential-free channel has to carry it too.
    """
    host.docker_info_ok = False
    host.run_confirmed()

    assert "docker stack unhealthy" in host.alerts_log(), host.alerts_log()
    assert host.messages()
    assert host.docker_alert_marker.exists()


def test_one_crash_looping_service_among_ten_healthy_ones_is_named(host):
    """AC3. After the 08-21 restart, 10 of 11 containers came up and
    portfolio-accounting was left crash-looping (KAN-61). A daemon-only check
    reports that stack as healthy."""
    host.containers["portfolio-accounting"] = ("restarting", "Restarting (1) 3 seconds ago")
    host.run_confirmed()

    body = " ".join(host.messages())
    assert "portfolio-accounting(restarting)" in body, body
    # The ten healthy ones must not be dragged into the message.
    assert "postgres(" not in body


def test_a_service_with_no_container_at_all_is_named(host):
    """`docker ps` cannot report what was never created, so the expected compose
    service list is what turns an absent container into a named finding."""
    del host.containers["execution"]
    host.run_confirmed()

    body = " ".join(host.messages())
    assert "execution(no-container)" in body, body


def test_an_unhealthy_healthcheck_is_named_even_while_the_container_runs(host):
    host.containers["postgres"] = ("running", "Up 2 hours (unhealthy)")
    host.run_confirmed()

    assert "postgres(unhealthy)" in " ".join(host.messages())


def test_a_one_shot_service_that_exited_cleanly_is_not_a_fault(host):
    """`migrate` runs `alembic upgrade head` at stack start and then sits at
    "Exited (0)" for the life of the stack. Reporting that as unhealthy would
    fire an alert every five minutes forever, which is worse than no alert."""
    assert host.containers[ONESHOT] == ("exited", "Exited (0) 6 days ago")
    host.run()

    assert host.messages() == [], host.messages()


def test_a_one_shot_that_exited_NON_zero_is_still_a_fault(host):
    """A failed migration is exactly the thing worth paging about, and the
    exemption above must not swallow it."""
    host.containers[ONESHOT] = ("exited", "Exited (1) 4 minutes ago")
    host.run_confirmed()

    assert "migrate(exited)" in " ".join(host.messages())


def test_the_container_scan_uses_a_format_the_real_docker_can_execute(host):
    """`docker ps` renders `.Labels` as a comma-joined STRING, so
    `{{index .Labels "x"}}` dies with "cannot index slice/array with type
    string" — silently returning no containers, i.e. a stack that always looks
    empty. The stub rejects any --format the real CLI could not run, so this
    asserts the shipped template is the working one rather than re-stating it.
    """
    host.containers["execution"] = ("exited", "Exited (1) 20 hours ago")
    host.run_confirmed()

    assert "execution(exited)" in " ".join(host.messages()), (
        "the docker ps template returned nothing usable"
    )


def test_a_transient_unhealthy_cycle_does_not_page(host):
    """The first observation is a strike, not a page.

    `docker compose up` shows containers at "(health: starting)" for a few
    seconds. An alert that fires on every deploy is one the operator learns to
    skim, which is the only failure mode this check cannot afford.
    """
    host.containers["api"] = ("running", "Up 2 seconds (health: starting)")
    host.run()

    assert host.messages() == [], host.messages()
    assert not host.docker_alert_marker.exists()
    assert "1st check" in host.watchdog_log()

    # ...and it clears without ever having said anything.
    host.containers["api"] = ("running", "Up 1 minute (healthy)")
    host.now += 300
    host.run()
    assert host.messages() == []


def test_a_sustained_fault_pages_on_the_second_cycle(host):
    """The strike is a delay, not a reprieve. The 2026-08-20 outage lasted
    roughly fifteen hours; one extra 300s cycle costs nothing real."""
    host.docker_info_ok = False
    host.run()
    assert host.messages() == []

    host.now += 300
    host.run()
    assert host.messages(), "a sustained outage never escalated past the strike"


def test_a_host_without_docker_never_accumulates_a_strike(host):
    """"Cannot ask" must never ripen into a page, or a machine with no
    container runtime alerts every five minutes forever."""
    (host.bin / "docker").unlink()
    host.disable_docker = True
    host.run_confirmed()
    host.now += 300
    host.run()

    assert host.messages() == []


def test_a_healthy_stack_produces_no_alert(host):
    """AC7: no false positives during normal operation."""
    host.run()

    assert host.messages() == []
    assert not host.docker_alert_marker.exists()
    assert host.alerts_log() == ""


def test_a_recovery_alert_fires_when_the_daemon_and_services_return(host):
    """AC4, matching the watchdog's existing recovered-alert behaviour."""
    host.docker_info_ok = False
    host.run_confirmed()
    assert host.docker_alert_marker.exists()
    host.clear_messages()

    host.docker_info_ok = True
    host.run()

    assert "recovered" in " ".join(host.messages()).lower()
    assert not host.docker_alert_marker.exists()


def test_the_stack_alert_does_not_repeat_before_twelve_hours(host):
    host.docker_info_ok = False
    host.run_confirmed()
    host.clear_messages()

    host.now += 11 * 3600
    host.age_marker(host.docker_alert_marker, 11 * 3600)
    host.run()
    assert host.messages() == [], "re-alerted early"

    host.now += 2 * 3600
    host.age_marker(host.docker_alert_marker, 13 * 3600)
    host.run()
    assert host.messages(), "12h re-alert never fired"


def test_the_stack_check_never_restarts_kills_or_opens_docker(host):
    """AC5. A dead engine needed process kills and an app relaunch; automating
    that against a live trading host on a 5-minute timer is the thing this
    story deliberately did not do."""
    # Static: the shipped code contains no such invocation.
    for path in (DOCKER_HEALTH, WATCHDOG):
        text = path.read_text()
        # Strip comments — the header explains *why* there is no restart, and
        # that prose must not be what trips or satisfies the check.
        code = "\n".join(
            ln for ln in text.splitlines() if not ln.lstrip().startswith("#")
        )
        for forbidden in (
            "docker restart", "docker kill", "docker stop", "docker compose up",
            "docker compose restart", "docker desktop restart", "open -a",
            "pkill", "killall",
        ):
            assert forbidden not in code, f"{path.name} may not run `{forbidden}`"

    # Behavioural: with the daemon dead, nothing was invoked but `docker info`.
    host.docker_info_ok = False
    host.run_confirmed()
    assert "restart" not in host.watchdog_log().lower() or "NOT restarting" in host.watchdog_log()


def test_a_missing_docker_cli_is_reported_as_unknown_not_as_a_dead_daemon(host):
    """A host with no docker at all must not be paged as an outage."""
    (host.bin / "docker").unlink()
    host.disable_docker = True
    host.run()

    assert host.messages() == []
    assert "UNKNOWN" in host.watchdog_log()


# ---------------------------------------------------------------------------
# KAN-63 — the Error 1100 latch
# ---------------------------------------------------------------------------

def test_a_latch_stamped_for_a_dead_gateway_is_dropped_with_no_alert(host):
    """AC2. The 2026-08-21 shape: the latch was raised in one Gateway session
    and read after that process had been replaced."""
    host.write_conn_marker(lost_ago=72_000, pid="12345", started_ago=100_000)
    host.run()

    assert not host.conn_marker.exists(), "stale latch was not dropped"
    assert host.messages() == [], host.messages()
    log = host.watchdog_log()
    assert "STALE Error 1100 latch dropped" in log, log
    assert "pid 12345" in log and f"pid {host.gateway_pid}" in log


def test_a_legacy_bare_epoch_latch_older_than_the_running_gateway_is_dropped(host):
    """The backstop for a marker this watchdog never got to stamp: a Gateway
    that started *after* the loss was recorded cannot be in that outage."""
    host.conn_marker.write_text(str(int(host.now - 72_000)))
    host.gateway_etime = "01:00:00"  # started an hour ago, long after the loss
    host.run()

    assert not host.conn_marker.exists()
    assert host.messages() == []
    assert "after the loss was recorded" in host.watchdog_log()


def test_dropping_a_stale_latch_clears_an_outstanding_alert(host):
    """AC5's sibling: an operator already told about a 1100 must be told it was
    never real, not left holding a page that silently stops repeating."""
    host.write_conn_marker(lost_ago=72_000, pid="12345", started_ago=100_000)
    host.conn_alert_marker.touch()
    host.run()

    body = " ".join(host.messages())
    assert "STALE" in body, body
    assert not host.conn_alert_marker.exists()


def test_a_live_1100_with_a_matching_gateway_still_alerts_as_before(host):
    """AC4 — the real-1100 path, which must not regress."""
    host.write_conn_marker(lost_ago=600, pid="live")
    host.run()

    body = " ".join(host.messages())
    assert "Error 1100" in body, body
    assert "~10 min" in body
    assert "NOT restart" in body
    assert host.conn_marker.exists(), "a live latch must survive"
    assert host.conn_alert_marker.exists()
    assert not any("kickstart" in c for c in host.launchctl_calls())


def test_the_180_second_floor_still_suppresses_a_self_healing_1100(host):
    """AC4's CONN_SUSTAINED_SECS clause: a 1100 that clears inside one cycle
    was never worth a page."""
    host.write_conn_marker(lost_ago=60, pid="live")
    host.run()

    assert host.messages() == []
    assert host.conn_marker.exists()


def test_a_live_1100_does_not_re_alert_before_twelve_hours(host):
    """AC4's 12h re-alert clause."""
    host.write_conn_marker(lost_ago=600, pid="live")
    host.run()
    host.clear_messages()

    host.age_marker(host.conn_alert_marker, 11 * 3600)
    host.run()
    assert host.messages() == []


def test_a_latch_whose_writer_is_down_reports_execution_not_a_duration(host):
    """AC3. Execution is the only thing that writes or clears the latch, so
    with execution down its age means nothing — and "the execution service is
    down" is the alert that is both true and actionable."""
    host.write_conn_marker(lost_ago=72_000, pid="live")
    host.containers["execution"] = ("exited", "Exited (1) 20 hours ago")
    host.run()

    hits = [m for m in host.messages() if "execution service is DOWN" in m]
    assert hits, host.messages()
    assert "not reporting a 1100 duration" in hits[0]
    assert "1200 min" not in hits[0]
    assert "execution service is down" in host.alerts_log()


def test_a_dead_daemon_also_means_the_latch_writer_is_down(host):
    """The 2026-08-21 case exactly: the whole engine was gone, so execution was
    too, and the watchdog reported a 20-hour 1100 instead."""
    host.write_conn_marker(lost_ago=72_000, pid="live")
    host.docker_info_ok = False
    host.run()

    assert any("execution service is DOWN" in m for m in host.messages()), host.messages()


def test_the_all_clear_still_fires_when_the_marker_is_removed(host):
    """AC5: the existing recovery path, unchanged."""
    host.conn_alert_marker.touch()
    host.run()

    assert "restored" in " ".join(host.messages())
    assert not host.conn_alert_marker.exists()


def test_a_first_observation_stamps_the_gateway_identity_onto_the_marker(host):
    """AC1. Execution runs in a container and cannot see the host Gateway's
    process identity, so the watchdog — the only party that can — records it."""
    host.write_conn_marker(lost_ago=600)
    host.run()

    text = host.conn_marker.read_text()
    assert f"gateway_pid={host.gateway_pid}" in text, text
    assert "gateway_started_at=" in text
    # Line 1 stays the bare loss epoch, so any older reader still parses it.
    assert text.splitlines()[0] == str(int(host.now - 600))


def test_a_stamped_latch_is_dropped_once_the_gateway_is_replaced(host):
    """The whole point, in two cycles: alert on a real 1100, then go quiet when
    the Gateway that raised it is gone — instead of re-alerting for 20 hours."""
    host.write_conn_marker(lost_ago=600)
    host.run()
    assert any("Error 1100" in m for m in host.messages())
    host.clear_messages()

    host.gateway_pid = "99999"           # cold restart replaced the process
    host.gateway_etime = "00:05:00"
    host.now += 72_000
    host.run()

    assert not host.conn_marker.exists()
    assert not any("lost server connectivity" in m for m in host.messages())


def test_the_reported_duration_is_bounded(host):
    """KAN-63 part 3: an unbounded, ever-growing number in an alert is the
    signal that nothing is measuring it. The 08-21 page said "~1207 min"."""
    host.gateway_etime = "3-00:00:00"    # Gateway older than the loss, so live
    host.write_conn_marker(lost_ago=200_000, pid="live")
    host.run()

    body = " ".join(host.messages())
    assert "over 24h" in body, body
    assert "3333 min" not in body


def test_an_unreadable_gateway_identity_does_not_drop_a_latch(host):
    """Absence of evidence is not evidence the latch is stale: if the Gateway's
    identity cannot be read, the safe move is to leave the latch alone and say
    so, not to silently discard a possibly-real outage."""
    host.gateway_pid = ""                # absent from `launchctl list`
    host.write_conn_marker(lost_ago=600)
    host.run()

    assert host.conn_marker.exists()
    assert "could not determine the running Gateway's identity" in host.watchdog_log()


# ---------------------------------------------------------------------------
# KAN-62 — the escalation half
# ---------------------------------------------------------------------------

AUTH_LOG = "23:55:09 Dialog: Unrecognized Username or Password\n"


def test_no_kickstart_is_issued_while_the_auth_dialog_string_is_present(host):
    """AC5. This is the 2026-07-01 lockout guard — 30 rejected logins — and it
    must not regress."""
    host.port_up = False
    host.ibc_log_text = AUTH_LOG
    host.down_marker.touch()   # a strike is already pending: without the guard
    host.run()                 # this cycle is exactly when it would kickstart

    assert not any("kickstart" in c for c in host.launchctl_calls()), host.launchctl_calls()
    assert "AUTH FAILURE" in host.watchdog_log()


def test_the_auth_branch_no_longer_clears_the_two_strike_marker(host):
    """AC4. Line 128 used to `rm -f "$MARKER"` — the counter belongs to the
    kickstart path, not to this branch."""
    host.port_up = False
    host.ibc_log_text = AUTH_LOG
    host.down_marker.touch()
    host.run()

    assert host.down_marker.exists(), "the auth branch cleared a marker it does not own"


def test_a_port_down_then_auth_then_clear_sequence_needs_no_extra_grace_pass(host):
    """AC4, behaviourally. On 2026-08-21 the auth condition cleared at 08:19 and
    the watchdog logged "port 7497 down (1st check)" at 08:20:09 — a whole extra
    cycle of downtime bolted onto the outage it had just waited out."""
    host.port_up = False
    host.run()                                   # cycle 1: first strike
    assert host.down_marker.exists()

    host.ibc_log_text = AUTH_LOG                 # cycle 2: auth failure appears
    host.now += 300
    host.run()

    host.ibc_log_text = "Login has completed\n"  # cycle 3: auth cleared, port still down
    host.now += 300
    host.run()

    assert any("kickstart" in c for c in host.launchctl_calls()), (
        "the auth cycle cost a grace pass: cycle 3 should have been the second "
        f"strike, not the first. launchctl calls: {host.launchctl_calls()}"
    )


def _auth_cycle(host: Host, *, minutes_before_run: float, alerted_ago: int | None):
    host.port_up = False
    host.ibc_log_text = AUTH_LOG
    host.now = RUN_AT - minutes_before_run * 60
    if alerted_ago is None:
        host.auth_marker.unlink(missing_ok=True)
    else:
        host.auth_marker.touch()
        host.age_marker(host.auth_marker, alerted_ago)
    host.clear_messages()
    host.run()
    return host.messages()


@pytest.mark.parametrize(
    "minutes_before_run, alerted_ago, should_alert",
    [
        # Far out: the flat 12h cadence is still correct — this is not an
        # emergency yet and nobody needs paging hourly overnight.
        (20 * 60, 6 * 3600, False),
        (20 * 60, 13 * 3600, True),
        # Inside 6h: hourly.
        (5 * 60, 30 * 60, False),
        (5 * 60, 70 * 60, True),
        # Inside 3h: every 30 minutes.
        (2 * 60, 20 * 60, False),
        (2 * 60, 40 * 60, True),
        # Inside the final hour: every 15 minutes. Under the old flat 12h rule
        # every one of these would have stayed silent.
        (45, 10 * 60, False),
        (45, 20 * 60, True),
        (10, 20 * 60, True),
    ],
)
def test_the_auth_realert_tightens_as_the_paper_run_approaches(
    host, minutes_before_run, alerted_ago, should_alert
):
    """AC3, clock-driven. On 2026-08-21 the single alert landed at 23:59:56 and
    the next was not due until noon, so the operator's last warning was 4h16m
    old when the run aborted."""
    sent = _auth_cycle(host, minutes_before_run=minutes_before_run,
                       alerted_ago=alerted_ago)
    assert bool(sent) is should_alert, sent


def test_an_auth_alert_always_lands_within_sixty_minutes_of_the_run(host):
    """AC3's guarantee, simulated over the real 300s cycle.

    The clock starts 61 minutes out with an alert *just* sent — the worst case,
    because the operator is maximally stale and nothing is due. The watchdog
    then runs every 300s up to the run time.
    """
    host.port_up = False
    host.ibc_log_text = AUTH_LOG
    host.auth_marker.touch()
    host.now = RUN_AT - 61 * 60
    host.age_marker(host.auth_marker, 0)
    host.clear_messages()

    last_alert_at = None
    for step in range(0, 62):
        host.now = RUN_AT - 61 * 60 + step * 300
        if host.now > RUN_AT:
            break
        before = len(host.messages())
        host.run()
        if len(host.messages()) > before:
            last_alert_at = host.now

    assert last_alert_at is not None, "no alert at all in the hour before the run"
    stale_minutes = (RUN_AT - last_alert_at) / 60
    assert stale_minutes <= 60, (
        f"the last warning before the run was {stale_minutes:.0f} min old; "
        "on 2026-08-21 that staleness was 4h16m and the run aborted"
    )


def test_the_auth_alert_names_how_long_is_left_before_the_run(host):
    """A page that says "in 42 min" is actionable in a way that a bare "login
    rejected" is not — the operator has to decide whether to get up."""
    sent = _auth_cycle(host, minutes_before_run=42, alerted_ago=None)
    assert sent
    assert re.search(r"4:15 paper run is in 4[12] min", sent[0]), sent


def test_recovery_from_the_auth_failure_still_alerts(host):
    host.port_up = False
    host.ibc_log_text = AUTH_LOG
    host.run()
    assert host.auth_marker.exists()
    host.clear_messages()

    host.port_up = True
    host.run()

    assert "recovered" in " ".join(host.messages()).lower()
    assert not host.auth_marker.exists()


# ---------------------------------------------------------------------------
# KAN-62 — the config half, pinned so it cannot silently move back
# ---------------------------------------------------------------------------

# The four SGT jobs in the daily chain. AutoRestartTime must not land in here,
# nor in the hours immediately before the first of them.
JOB_WINDOW_START = 4 * 60       # 04:00, ahead of the 04:15 paper run
JOB_WINDOW_END = 5 * 60 + 30    # 05:30, after the 05:15 DB backup

CONFIG_INI = Path.home() / "ibc" / "config.ini"


def _parse_ibc_time(value: str) -> int:
    """IBC writes `2:00 PM` / `11:55 PM` / `08:00`. Returns minutes past midnight."""
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*(AM|PM)?\s*$", value, re.IGNORECASE)
    assert m, f"unparseable IBC time {value!r}"
    hour, minute, meridiem = int(m.group(1)), int(m.group(2)), m.group(3)
    if meridiem:
        meridiem = meridiem.upper()
        if meridiem == "PM" and hour != 12:
            hour += 12
        if meridiem == "AM" and hour == 12:
            hour = 0
    return hour * 60 + minute


def _readme_auto_restart_time() -> str:
    """The value of record. ~/ibc/config.ini is a host file, not a repo file, so
    the repo's copy of the decision lives in README.md — and is what CI can pin.
    """
    m = re.search(r"`AutoRestartTime=([^`]+)`", README.read_text())
    assert m, (
        "deploy/launchd/README.md no longer records `AutoRestartTime=...`. "
        "KAN-62 AC1 requires the chosen value and its reason to be recorded "
        "there, because ~/ibc/config.ini is not in the repo."
    )
    return m.group(1)


def test_auto_restart_time_is_outside_the_scheduled_job_window():
    """AC1 + AC2. At 11:55 PM a rejected re-login had 4h20m to be noticed by a
    human who was asleep, and no automated path at all — the watchdog is
    forbidden from kickstarting into an auth failure and ColdRestartTime is
    weekly. That cost 2026-08-18 and 2026-08-21."""
    minutes = _parse_ibc_time(_readme_auto_restart_time())
    assert not (JOB_WINDOW_START <= minutes <= JOB_WINDOW_END), (
        f"AutoRestartTime is {_readme_auto_restart_time()}, inside the "
        f"04:00-05:30 job window"
    )
    # And it must not sit in the run-up either: a failure at 23:55 is only
    # "outside the window" in the narrowest sense.
    hours_of_slack = ((JOB_WINDOW_START - minutes) % (24 * 60)) / 60
    assert hours_of_slack >= 8, (
        f"AutoRestartTime leaves only {hours_of_slack:.1f}h before the 04:15 "
        "run. The 23:55 value left 4h20m, all of it overnight, and a rejected "
        "re-login ate two NYSE sessions of gate evidence."
    )


@pytest.mark.skipif(not CONFIG_INI.exists(), reason="operator host only (no ~/ibc/config.ini in CI)")
def test_the_live_ibc_config_agrees_with_the_recorded_value():
    """On the operator host, the file and the record must not have diverged."""
    live = None
    for line in CONFIG_INI.read_text().splitlines():
        if line.strip().startswith("AutoRestartTime="):
            live = line.split("=", 1)[1].strip()
    assert live is not None, "~/ibc/config.ini has no AutoRestartTime"
    assert _parse_ibc_time(live) == _parse_ibc_time(_readme_auto_restart_time()), (
        f"~/ibc/config.ini says AutoRestartTime={live} but "
        f"deploy/launchd/README.md records {_readme_auto_restart_time()}"
    )


def test_the_watchdogs_run_time_constants_match_the_paper_plist():
    """The escalating cadence is computed against a hardcoded 04:15. If the run
    moves, every threshold above is measured from the wrong instant."""
    with PAPER_PLIST.open("rb") as fh:
        parsed = plistlib.load(fh)
    interval = parsed["StartCalendarInterval"]
    entries = interval if isinstance(interval, list) else [interval]
    hours = {e["Hour"] for e in entries}
    minutes = {e["Minute"] for e in entries}
    assert len(hours) == 1 and len(minutes) == 1, (
        "the paper job no longer runs at one time of day; the watchdog's "
        "escalation window needs rethinking"
    )

    text = WATCHDOG.read_text()
    assert f"PAPER_RUN_HOUR={hours.pop()}" in text
    assert f"PAPER_RUN_MIN={minutes.pop()}" in text
