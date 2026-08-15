"""T6 — observability & unattended healthchecks assertion tests.

Parses/greps the checked-in compose and Prometheus config files directly, so
a regression fails CI without needing a running docker daemon or a live
Prometheus. Pattern follows tests/deploy/test_db_backup_script.py and
tests/deploy/test_message_bus_lockdown.py (T3).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import yaml

from shared.heartbeat import DEFAULT_HEARTBEAT_PATH
from shared.redis_client import DEFAULT_STREAM_MAXLEN

COMPOSE_PATH = Path("docker-compose.yml")
OBSERVABILITY_COMPOSE_PATH = Path("docker-compose.observability.yml")
PROMETHEUS_CONFIG_PATH = Path("config/prometheus.yml")
ALERT_RULES_PATH = Path("config/alert_rules.yml")

# Services whose container healthcheck reads the heartbeat *file*. The api is
# not one of them — it is probed through /healthz instead, because a FastAPI
# process answering HTTP is the same evidence for it that a fresh heartbeat
# file is for a worker loop.
HEARTBEAT_SERVICES = (
    "data-ingestion",
    "signal-generation",
    "ml-model",
    "risk-management",
    "execution",
    "notifications",
    "portfolio-accounting",
)
ALL_METRICS_SERVICES = HEARTBEAT_SERVICES + ("api",)

# KAN-15 (AC7): services that must publish algo_heartbeat_age_seconds. This is
# eight, not seven. HeartbeatStale matches job!="data-ingestion", which
# *includes* api — but api registered no collector, so no series existed and
# the rule could never fire for it however long the API hung. The alert
# believed it covered a service it did not.
HEARTBEAT_METRIC_SERVICES = HEARTBEAT_SERVICES + ("api",)

RUNNER_FILES_EXPECTING_HEARTBEAT = {
    "data-ingestion": Path("services/data_ingestion/runner.py"),
    "signal-generation": Path("services/signal_generation/runner.py"),
    "ml-model": Path("services/ml_model/runner.py"),
    "risk-management": Path("services/risk_management/runner.py"),
    "execution": Path("services/execution/runner.py"),
    "notifications": Path("services/notifications/runner.py"),
    "portfolio-accounting": Path("services/portfolio_accounting/runner.py"),
}

RUNNER_FILES_EXPECTING_SETUP_METRICS = {
    **RUNNER_FILES_EXPECTING_HEARTBEAT,
    "api": Path("services/api/app.py"),
}

RUNNER_FILES_EXPECTING_HEARTBEAT_METRIC = {
    **RUNNER_FILES_EXPECTING_HEARTBEAT,
    "api": Path("services/api/app.py"),
}


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def test_every_backend_service_calls_setup_metrics() -> None:
    for service, runner_path in RUNNER_FILES_EXPECTING_SETUP_METRICS.items():
        text = runner_path.read_text()
        assert "setup_metrics(" in text, f"{service} ({runner_path}) never calls setup_metrics()"


def test_every_worker_service_writes_a_heartbeat() -> None:
    for service, runner_path in RUNNER_FILES_EXPECTING_HEARTBEAT.items():
        text = runner_path.read_text()
        assert "write_heartbeat(" in text, f"{service} ({runner_path}) never calls write_heartbeat()"


def test_all_eight_scraped_services_publish_the_heartbeat_gauge() -> None:
    """KAN-15 AC7 — HeartbeatStale's job!="data-ingestion" matcher covers the
    api, so the api has to actually emit the series or the rule is quietly
    covering seven services while claiming eight."""
    assert set(HEARTBEAT_METRIC_SERVICES) == set(ALL_METRICS_SERVICES)
    for service, runner_path in RUNNER_FILES_EXPECTING_HEARTBEAT_METRIC.items():
        text = runner_path.read_text()
        assert "register_heartbeat_collector(" in text, (
            f"{service} ({runner_path}) never registers a heartbeat collector, so "
            "algo_heartbeat_age_seconds has no series for it and HeartbeatStale "
            "cannot fire however wedged it gets"
        )
        assert "write_heartbeat(" in text, (
            f"{service} ({runner_path}) registers a collector but never writes the "
            "file — the gauge would report inf from the first scrape"
        )


def test_api_exposes_an_unauthenticated_healthz_route() -> None:
    text = Path("services/api/app.py").read_text()
    assert '"/healthz"' in text


def test_prometheus_config_declares_rule_files() -> None:
    config = _load_yaml(PROMETHEUS_CONFIG_PATH)
    assert "alert_rules.yml" in config["rule_files"]


def test_prometheus_scrapes_every_service_including_portfolio_accounting() -> None:
    config = _load_yaml(PROMETHEUS_CONFIG_PATH)
    scraped_jobs = {job["job_name"] for job in config["scrape_configs"]}
    for service in ALL_METRICS_SERVICES:
        assert service in scraped_jobs, f"{service} is not in config/prometheus.yml scrape_configs"
    # This one was missing entirely before T6 — regression guard.
    assert "portfolio-accounting" in scraped_jobs


def test_prometheus_scrapes_redis_exporter() -> None:
    config = _load_yaml(PROMETHEUS_CONFIG_PATH)
    scraped_jobs = {job["job_name"]: job for job in config["scrape_configs"]}
    assert "redis-exporter" in scraped_jobs
    assert scraped_jobs["redis-exporter"]["static_configs"][0]["targets"] == [
        "redis-exporter:9121"
    ]


def test_alert_rules_file_parses_and_covers_the_checklist() -> None:
    rules = _load_yaml(ALERT_RULES_PATH)
    alert_names = {
        rule["alert"]
        for group in rules["groups"]
        for rule in group["rules"]
    }
    # stream-idle, no-fills-in-N-min, dlq-depth, redis-memory (checklist),
    # plus the service-down/wedged detector this thread's core fix enables.
    assert "ApprovedOrdersStreamIdle" in alert_names
    assert "NoFillsRecently" in alert_names
    assert "DeadLetterQueueBacklog" in alert_names
    assert "RedisMemoryHigh" in alert_names
    assert "ServiceMetricsEndpointDown" in alert_names
    # CRITICAL fix: wedge detection actually wired to an alert (not just
    # detectable at the container level via docker-compose.yml healthchecks).
    assert "HeartbeatStale" in alert_names
    assert "HeartbeatStaleDataIngestion" in alert_names
    # IMPORTANT fix: the passive-breach half of "risk breaches".
    assert "PassiveBreachDetected" in alert_names


def test_heartbeat_stale_alert_references_the_heartbeat_gauge() -> None:
    rules = _load_yaml(ALERT_RULES_PATH)
    heartbeat_rules = [
        rule
        for group in rules["groups"]
        for rule in group["rules"]
        if rule["alert"] in ("HeartbeatStale", "HeartbeatStaleDataIngestion")
    ]
    assert len(heartbeat_rules) == 2
    for rule in heartbeat_rules:
        assert "algo_heartbeat_age_seconds" in rule["expr"]


def test_passive_breach_alert_references_the_risk_breach_counter() -> None:
    rules = _load_yaml(ALERT_RULES_PATH)
    rule = next(
        rule
        for group in rules["groups"]
        for rule in group["rules"]
        if rule["alert"] == "PassiveBreachDetected"
    )
    assert "algo_risk_breach_total" in rule["expr"]


def test_dlq_depth_alert_matches_every_dead_letter_stream_generically() -> None:
    rules = _load_yaml(ALERT_RULES_PATH)
    dlq_rule = next(
        rule
        for group in rules["groups"]
        for rule in group["rules"]
        if rule["alert"] == "DeadLetterQueueBacklog"
    )
    # Must match the shared/redis_client.py DEAD_LETTER_SUFFIX convention
    # generically (any stream's `:dlq`), not one hardcoded stream name.
    assert ":dlq" in dlq_rule["expr"]
    assert "redis_stream_length" in dlq_rule["expr"]


def test_redis_service_sets_a_maxmemory_ceiling_with_noeviction() -> None:
    compose = _load_yaml(COMPOSE_PATH)
    command = compose["services"]["redis"].get("command")
    assert command is not None, "redis service has no command: (maxmemory not set)"
    assert "--maxmemory" in command
    assert "--maxmemory-policy" in command
    assert command[command.index("--maxmemory-policy") + 1] == "noeviction"


def _parse_maxmemory_bytes(command: list[str]) -> int:
    raw = command[command.index("--maxmemory") + 1]  # e.g. "512mb"
    units = {"kb": 1024, "mb": 1024**2, "gb": 1024**3}
    for suffix, multiplier in units.items():
        if raw.lower().endswith(suffix):
            return int(raw[: -len(suffix)]) * multiplier
    return int(raw)  # plain byte count


def test_stream_maxlen_and_redis_maxmemory_are_reconciled_with_headroom() -> None:
    """IMPORTANT fix: DEFAULT_STREAM_MAXLEN (shared/redis_client.py) and
    docker-compose.yml's redis --maxmemory were previously picked
    independently. This is the cross-file half of
    tests/shared/test_redis_client.py's
    test_default_maxlen_fits_the_maxmemory_ceiling_with_headroom — that one
    hardcodes "512mb" as a stand-in for this file's actual value; this one
    reads the real value out of docker-compose.yml so the two can't drift
    apart without a test failing on at least one side.
    """
    compose = _load_yaml(COMPOSE_PATH)
    maxmemory_bytes = _parse_maxmemory_bytes(compose["services"]["redis"]["command"])

    assumed_worst_case_entry_bytes = 1024  # see the comment above DEFAULT_STREAM_MAXLEN
    primary_stream_count = 9
    worst_case_bytes = (
        primary_stream_count * DEFAULT_STREAM_MAXLEN * assumed_worst_case_entry_bytes
    )

    assert worst_case_bytes < maxmemory_bytes * 0.5, (
        f"worst-case stream memory ({worst_case_bytes} bytes) is not comfortably "
        f"under 50% of --maxmemory ({maxmemory_bytes} bytes) — DEFAULT_STREAM_MAXLEN "
        "and docker-compose.yml's --maxmemory have drifted apart; reconcile them "
        "together (see the comment above DEFAULT_STREAM_MAXLEN in shared/redis_client.py)"
    )


def test_redis_memory_high_alert_threshold_is_below_the_rejection_point() -> None:
    """RedisMemoryHigh must page BEFORE `noeviction` starts failing writes
    at 100% of maxmemory, not after."""
    rules = _load_yaml(ALERT_RULES_PATH)
    redis_memory_rule = next(
        rule
        for group in rules["groups"]
        for rule in group["rules"]
        if rule["alert"] == "RedisMemoryHigh"
    )
    assert "> 0.8" in redis_memory_rule["expr"]


def test_every_worker_service_has_a_heartbeat_healthcheck() -> None:
    compose = _load_yaml(COMPOSE_PATH)
    for service in HEARTBEAT_SERVICES:
        healthcheck = compose["services"][service].get("healthcheck")
        assert healthcheck is not None, f"{service} has no healthcheck"
        test = healthcheck["test"]
        assert DEFAULT_HEARTBEAT_PATH in " ".join(test), (
            f"{service}'s healthcheck does not reference "
            f"shared.heartbeat.DEFAULT_HEARTBEAT_PATH ({DEFAULT_HEARTBEAT_PATH}) — "
            "healthcheck and app-side heartbeat path have drifted apart"
        )


def test_healthchecks_do_not_depend_on_curl() -> None:
    """python:3.12-slim (every service Dockerfile) does not include curl —
    a healthcheck that shells out to curl would always fail with "not
    found", silently marking the container permanently unhealthy."""
    compose = _load_yaml(COMPOSE_PATH)
    for service in ALL_METRICS_SERVICES:
        healthcheck = compose["services"][service].get("healthcheck")
        assert healthcheck is not None, f"{service} has no healthcheck"
        assert "curl" not in " ".join(healthcheck["test"])


def test_api_healthcheck_targets_the_healthz_route() -> None:
    compose = _load_yaml(COMPOSE_PATH)
    healthcheck = compose["services"]["api"]["healthcheck"]
    assert "/healthz" in " ".join(healthcheck["test"])


def test_observability_compose_has_no_hardcoded_grafana_credentials() -> None:
    text = OBSERVABILITY_COMPOSE_PATH.read_text()
    assert "GF_SECURITY_ADMIN_PASSWORD=admin" not in text
    assert "GF_AUTH_ANONYMOUS_ENABLED=true" not in text


def test_observability_compose_ports_are_loopback_bound() -> None:
    compose = _load_yaml(OBSERVABILITY_COMPOSE_PATH)
    for service_name in ("prometheus", "grafana"):
        ports = compose["services"][service_name]["ports"]
        for binding in ports:
            assert binding.startswith("127.0.0.1:"), (
                f"{service_name} port {binding!r} is not loopback-bound"
            )


def test_observability_compose_declares_redis_exporter_with_stream_checks() -> None:
    compose = _load_yaml(OBSERVABILITY_COMPOSE_PATH)
    exporter = compose["services"]["redis-exporter"]
    assert "--check-streams-pattern=stream:*" in exporter["command"]


def test_prometheus_mounts_the_alert_rules_file() -> None:
    compose = _load_yaml(OBSERVABILITY_COMPOSE_PATH)
    volumes = compose["services"]["prometheus"]["volumes"]
    assert any("alert_rules.yml" in v for v in volumes)


# ---------------------------------------------------------------------------
# IMPORTANT fix: "dashboarded" acceptance criterion — config/grafana/ had
# only datasources.yml before this; Grafana came up with zero dashboards.
# ---------------------------------------------------------------------------

DASHBOARD_JSON_PATH = Path("config/grafana/dashboards/json/algo-poc-overview.json")
DASHBOARD_PROVIDER_PATH = Path("config/grafana/dashboards.yml")


def test_grafana_mounts_dashboard_provisioning_and_json() -> None:
    compose = _load_yaml(OBSERVABILITY_COMPOSE_PATH)
    volumes = compose["services"]["grafana"]["volumes"]
    assert any("dashboards.yml" in v for v in volumes), "no dashboard provider mounted"
    assert any("dashboards/json" in v for v in volumes), "no dashboard JSON folder mounted"


def test_dashboard_provider_config_parses() -> None:
    provider = _load_yaml(DASHBOARD_PROVIDER_PATH)
    assert provider["providers"][0]["type"] == "file"


def test_committed_dashboard_json_is_valid_and_covers_money_critical_signals() -> None:
    import json

    dashboard = json.loads(DASHBOARD_JSON_PATH.read_text())
    panel_text = json.dumps(dashboard["panels"])

    assert len(dashboard["panels"]) > 0, "dashboard has no panels — an empty Grafana is not dashboarded"
    # orders, fills, kill events, risk breaches, DLQ depth, redis memory,
    # heartbeat ages — the acceptance criteria's money-critical signal list.
    assert "algo_order_lifecycle_transitions_total" in panel_text  # orders
    assert "stream:fills" in panel_text  # fills
    assert "stream:kill" in panel_text  # kill events
    assert "algo_risk_breach_total" in panel_text  # risk breaches
    assert "redis_stream_length" in panel_text  # DLQ depth
    assert "redis_memory_used_bytes" in panel_text  # redis memory
    assert "algo_heartbeat_age_seconds" in panel_text  # heartbeat ages


# ---------------------------------------------------------------------------
# KAN-14 (P1-11) — stand the observability stack up honestly.
#
# Before this story the overlay could not start for anyone following the docs
# (GRAFANA_ADMIN_PASSWORD was a hard `${...:?}` requirement absent from
# .env.example), redis-exporter had no credential for the T3-locked-down
# Redis (so every redis_stream_* series the stream-health alerts depend on was
# missing), nothing carried a restart policy, and there was no Alertmanager at
# all — alert rules evaluated into a web UI nobody watches.
# ---------------------------------------------------------------------------

ENV_EXAMPLE_PATH = Path(".env.example")
ALERTMANAGER_CONFIG_PATH = Path("config/alertmanager.yml")
ALERTMANAGER_ENTRYPOINT_PATH = Path("deploy/alertmanager/entrypoint.sh")
TESTS_WORKFLOW_PATH = Path(".github/workflows/tests.yml")

OBSERVABILITY_SERVICES = ("prometheus", "grafana", "redis-exporter", "alertmanager")

# `${VAR}`, `${VAR:-default}`, `${VAR:?message}` — the three forms compose
# interpolation supports that this repo actually uses.
_COMPOSE_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::?[-?][^}]*)?\}")


def _documented_env_vars() -> set[str]:
    """Variable names .env.example tells an operator to set."""
    return {
        line.split("=", 1)[0].strip()
        for line in ENV_EXAMPLE_PATH.read_text().splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }


def _referenced_compose_vars(path: Path) -> set[str]:
    """Every `${VAR}` compose would actually interpolate.

    Walks the parsed document rather than the raw text so the prose in these
    files (which quotes `${VAR:?}` forms when explaining them) can't register
    as a variable an operator must set.
    """

    def walk(node: object) -> set[str]:
        if isinstance(node, str):
            return set(_COMPOSE_VAR_RE.findall(node))
        if isinstance(node, dict):
            return set().union(*(walk(k) | walk(v) for k, v in node.items())) if node else set()
        if isinstance(node, list):
            return set().union(*(walk(item) for item in node)) if node else set()
        return set()

    return walk(_load_yaml(path))


def test_env_example_documents_every_variable_the_compose_files_reference() -> None:
    """AC1/AC2 — the overlay must render from .env.example alone.

    Static rather than shelling out to `docker compose config`: the suite is
    deliberately self-contained (see .github/workflows/tests.yml), so this
    reimplements the one thing that check would prove — that no `${VAR}` in
    either compose file is a variable an operator was never told about. The
    historical failure was exactly that: GRAFANA_ADMIN_PASSWORD was a hard
    `${...:?}` requirement that .env.example never mentioned, so a fresh clone
    could not start the overlay at all.
    """
    documented = _documented_env_vars()
    for compose_path in (COMPOSE_PATH, OBSERVABILITY_COMPOSE_PATH):
        undocumented = _referenced_compose_vars(compose_path) - documented
        assert not undocumented, (
            f"{compose_path} references {sorted(undocumented)}, which "
            f"{ENV_EXAMPLE_PATH} does not document — `docker compose -f "
            "docker-compose.yml -f docker-compose.observability.yml config` "
            "cannot render for anyone following the docs"
        )


def test_env_example_documents_the_grafana_admin_password() -> None:
    assert "GRAFANA_ADMIN_PASSWORD" in _documented_env_vars()


def test_redis_exporter_authenticates_against_the_locked_down_redis() -> None:
    """AC3 (config half) — docker-compose.yml runs Redis with --requirepass;
    an exporter with only REDIS_ADDR gets NOAUTH on every scrape and publishes
    none of the redis_stream_* series the DLQ/stream-idle alerts read."""
    compose = _load_yaml(OBSERVABILITY_COMPOSE_PATH)
    environment = compose["services"]["redis-exporter"]["environment"]
    assert any(entry.startswith("REDIS_PASSWORD=") for entry in environment), (
        "redis-exporter has no REDIS_PASSWORD — it cannot authenticate against "
        "the --requirepass Redis in docker-compose.yml"
    )


def test_every_observability_service_survives_a_host_reboot() -> None:
    """AC4 — every app service in docker-compose.yml is `unless-stopped`; an
    overlay without restart policies leaves monitoring down while the trading
    stack comes back up."""
    compose = _load_yaml(OBSERVABILITY_COMPOSE_PATH)
    for service in OBSERVABILITY_SERVICES:
        assert compose["services"][service].get("restart") == "unless-stopped", (
            f"{service} has no `restart: unless-stopped`"
        )


def test_alertmanager_image_is_pinned_at_or_above_v0_26() -> None:
    """AC9 — bot_token_file does not exist before v0.26, so an earlier tag
    would silently need a committed token literal instead."""
    compose = _load_yaml(OBSERVABILITY_COMPOSE_PATH)
    image = compose["services"]["alertmanager"]["image"]
    repository, _, tag = image.partition(":")
    assert repository == "prom/alertmanager"
    assert tag and tag != "latest", f"alertmanager image {image!r} is not pinned"
    major, minor = (int(part) for part in tag.lstrip("v").split(".")[:2])
    assert (major, minor) >= (0, 26), f"alertmanager {tag} predates bot_token_file"


def test_alertmanager_config_carries_no_secret_literal() -> None:
    """AC6 — the token reaches the container only via bot_token_file. A config
    that needs a secret literal to work is a config that eventually gets one
    committed (the ~/ibc hand-fork precedent)."""
    config = _load_yaml(ALERTMANAGER_CONFIG_PATH)
    telegram_configs = [
        telegram
        for receiver in config["receivers"]
        for telegram in receiver.get("telegram_configs", [])
    ]
    assert telegram_configs, "no telegram_configs receiver in config/alertmanager.yml"
    for telegram in telegram_configs:
        assert "bot_token" not in telegram, "bot_token literal in a committed config"
        assert telegram["bot_token_file"] == "/etc/alertmanager/secrets/telegram_token"

    text = ALERTMANAGER_CONFIG_PATH.read_text()
    # Telegram bot tokens look like `<8-10 digits>:<35 url-safe chars>`.
    assert not re.search(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}", text), (
        "config/alertmanager.yml contains something shaped like a Telegram bot token"
    )


def test_alertmanager_route_sends_everything_to_telegram() -> None:
    """A route that drops a severity on the floor is a silent monitor.

    KAN-15 added exactly one deliberate exception: the always-firing Watchdog
    goes to the `deadman` webhook rather than to Telegram. That route is
    asserted separately in tests/deploy/test_alert_rules.py; here we only
    require that every sub-route names a receiver that actually exists, so a
    typo cannot swallow a severity silently.
    """
    config = _load_yaml(ALERTMANAGER_CONFIG_PATH)
    receiver_names = {receiver["name"] for receiver in config["receivers"]}
    root = config["route"]
    assert root["receiver"] in receiver_names
    for child in root.get("routes", []):
        # A sub-route that matches but names a receiver we don't define would
        # swallow that severity silently.
        assert child["receiver"] in receiver_names
    assert root.get("group_by"), "root route has no group_by"


def test_alertmanager_is_reachable_only_on_loopback() -> None:
    compose = _load_yaml(OBSERVABILITY_COMPOSE_PATH)
    for binding in compose["services"]["alertmanager"]["ports"]:
        assert binding.startswith("127.0.0.1:"), f"{binding!r} is not loopback-bound"


def test_prometheus_sends_alerts_to_the_alertmanager_service() -> None:
    """AC7 — alert rules that evaluate into a web UI nobody watches are not
    monitoring."""
    config = _load_yaml(PROMETHEUS_CONFIG_PATH)
    targets = [
        target
        for alertmanager in config["alerting"]["alertmanagers"]
        for static_config in alertmanager["static_configs"]
        for target in static_config["targets"]
    ]
    assert targets == ["alertmanager:9093"]


def test_prometheus_starts_after_the_alertmanager_it_pages_through() -> None:
    compose = _load_yaml(OBSERVABILITY_COMPOSE_PATH)
    assert "alertmanager" in compose["services"]["prometheus"]["depends_on"]


def test_alertmanager_renders_its_secrets_at_container_start() -> None:
    """The committed config is a template: the entrypoint writes the token to
    the tmpfs path bot_token_file points at and substitutes the real chat_id,
    so neither value is ever on disk or in a committed file."""
    compose = _load_yaml(OBSERVABILITY_COMPOSE_PATH)
    alertmanager = compose["services"]["alertmanager"]

    entrypoint = " ".join(alertmanager["entrypoint"])
    assert "entrypoint.sh" in entrypoint

    mounted = " ".join(alertmanager["volumes"])
    assert "./config/alertmanager.yml:" in mounted
    assert "./deploy/alertmanager/entrypoint.sh:" in mounted

    # The rendered secrets live on tmpfs, never on a writable layer or volume.
    assert any(
        mount.startswith("/etc/alertmanager/secrets") for mount in alertmanager["tmpfs"]
    ), "the secrets directory is not a tmpfs mount"

    env = alertmanager["environment"]
    assert any(entry.startswith("TELEGRAM_BOT_TOKEN=") for entry in env)
    assert any(entry.startswith("TELEGRAM_CHAT_ID=") for entry in env)
    # Not `${VAR:?}`: .env.example ships these blank (optional in paper/dev
    # mode), and a `:?` here would make the whole overlay unrenderable — the
    # exact failure this story exists to remove. The entrypoint refuses to
    # start with a named error instead.
    assert ":?" not in " ".join(env), (
        "a required-variable guard on the Telegram vars would break "
        "`docker compose config` for anyone whose .env leaves them blank"
    )


def _run_entrypoint(secret_dir: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Drive the real entrypoint with a stub alertmanager binary.

    Executed rather than grepped, following tests/deploy/
    test_launchd_secrets_keychain.py: the thing worth asserting is that the
    substitution actually happens, not that the source contains the right
    words.
    """
    stub = secret_dir.parent / "fake-alertmanager"
    stub.write_text("#!/bin/sh\necho \"started: $*\"\n")
    stub.chmod(0o755)
    return subprocess.run(
        ["/bin/sh", str(ALERTMANAGER_ENTRYPOINT_PATH.resolve())],
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "ALERTMANAGER_BIN": str(stub),
            "ALERTMANAGER_CONFIG_TEMPLATE": str(ALERTMANAGER_CONFIG_PATH.resolve()),
            "ALERTMANAGER_SECRET_DIR": str(secret_dir),
            **env,
        },
    )


def test_alertmanager_entrypoint_renders_the_token_and_chat_id(tmp_path: Path) -> None:
    secret_dir = tmp_path / "secrets"
    result = _run_entrypoint(
        secret_dir,
        {"TELEGRAM_BOT_TOKEN": "123456789:AAtest-token", "TELEGRAM_CHAT_ID": "-1001234567890"},
    )
    assert result.returncode == 0, result.stderr

    token_file = secret_dir / "telegram_token"
    assert token_file.read_text() == "123456789:AAtest-token"
    # A world-readable secret on a shared tmpfs is the same class of finding
    # as a committed one.
    assert token_file.stat().st_mode & 0o077 == 0

    rendered = (secret_dir / "alertmanager.yml").read_text()
    assert "chat_id: -1001234567890" in rendered
    # The committed placeholder must not survive into the running config.
    assert "chat_id: 1\n" not in rendered
    # bot_token_file is left pointing at the token this script just wrote.
    assert "bot_token_file: /etc/alertmanager/secrets/telegram_token" in rendered
    assert f"--config.file={secret_dir / 'alertmanager.yml'}" in result.stdout


def test_alertmanager_entrypoint_refuses_to_start_as_a_silent_monitor(tmp_path: Path) -> None:
    """The 2026-08-13 outage was an alert path that failed quietly. An
    Alertmanager that comes up unable to deliver is that failure again."""
    for env, expected in (
        ({"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": "-100123"}, "TELEGRAM_BOT_TOKEN"),
        ({"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": ""}, "TELEGRAM_CHAT_ID"),
        ({"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "@mychannel"}, "must be an integer"),
    ):
        result = _run_entrypoint(tmp_path / f"secrets-{expected[:6]}", env)
        assert result.returncode != 0, f"started anyway with {env}"
        assert expected in result.stderr, result.stderr
        assert "FATAL" in result.stderr


def test_alertmanager_entrypoint_detects_a_template_that_stopped_rendering(
    tmp_path: Path,
) -> None:
    """If a future edit renames or reindents chat_id, the placeholder would
    otherwise survive and every alert would go to a stranger's chat."""
    template = tmp_path / "alertmanager.yml"
    template.write_text(
        ALERTMANAGER_CONFIG_PATH.read_text().replace("chat_id:", "chatId:")
    )
    result = _run_entrypoint(
        tmp_path / "secrets",
        {
            "TELEGRAM_BOT_TOKEN": "t",
            "TELEGRAM_CHAT_ID": "-100123",
            "ALERTMANAGER_CONFIG_TEMPLATE": str(template),
        },
    )
    assert result.returncode != 0
    assert "chat_id" in result.stderr


def test_ci_checks_the_alertmanager_config() -> None:
    """AC5 (design test #14) — a malformed route must be caught before it
    silences alerts, not after."""
    workflow = TESTS_WORKFLOW_PATH.read_text()
    assert "amtool check-config config/alertmanager.yml" in workflow


def test_ci_amtool_version_matches_the_pinned_alertmanager_image() -> None:
    """Checking the config with a different Alertmanager version than the one
    that runs it is a green CI that proves nothing."""
    compose = _load_yaml(OBSERVABILITY_COMPOSE_PATH)
    tag = compose["services"]["alertmanager"]["image"].partition(":")[2]
    workflow = TESTS_WORKFLOW_PATH.read_text()
    assert tag.lstrip("v") in workflow, (
        f"tests.yml does not pin amtool to the image tag {tag}"
    )
