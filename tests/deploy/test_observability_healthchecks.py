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
from shared.redis_client import DEFAULT_STREAM_MAXLEN

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
