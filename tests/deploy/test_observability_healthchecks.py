"""T6 — observability & unattended healthchecks assertion tests.

Parses/greps the checked-in compose and Prometheus config files directly, so
a regression fails CI without needing a running docker daemon or a live
Prometheus. Pattern follows tests/deploy/test_db_backup_script.py and
tests/deploy/test_message_bus_lockdown.py (T3).
"""

from __future__ import annotations

from pathlib import Path

import yaml

from shared.heartbeat import DEFAULT_HEARTBEAT_PATH

COMPOSE_PATH = Path("docker-compose.yml")
OBSERVABILITY_COMPOSE_PATH = Path("docker-compose.observability.yml")
PROMETHEUS_CONFIG_PATH = Path("config/prometheus.yml")
ALERT_RULES_PATH = Path("config/alert_rules.yml")

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
