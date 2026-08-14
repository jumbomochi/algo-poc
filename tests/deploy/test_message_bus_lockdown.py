"""T3 — message-bus lockdown assertion tests.

The committed docker-compose.yml is the thing a fresh clone actually runs;
these tests parse and grep it (and its companion example files) directly, so
a regression here fails CI without needing a running docker daemon. Pattern
follows tests/deploy/test_db_backup_script.py (plain-text assertions on a
checked-in ops file).
"""

from __future__ import annotations

from pathlib import Path

import yaml


COMPOSE_PATH = Path("docker-compose.yml")
ENV_EXAMPLE_PATH = Path(".env.example")
OVERRIDE_EXAMPLE_PATH = Path("docker-compose.override.yml.example")
GITIGNORE_PATH = Path(".gitignore")

LOCKED_DOWN_SERVICES_WITH_DB = (
    "migrate",
    "data-ingestion",
    "signal-generation",
    "ml-model",
    "risk-management",
    "execution",
    "api",
    "notifications",
    "portfolio-accounting",
)
LOCKED_DOWN_SERVICES_WITH_REDIS = (
    "data-ingestion",
    "signal-generation",
    "ml-model",
    "risk-management",
    "execution",
    "api",
    "notifications",
    "portfolio-accounting",
)


def _load_compose() -> dict:
    return yaml.safe_load(COMPOSE_PATH.read_text())


def test_compose_file_has_no_hardcoded_weak_credentials():
    text = COMPOSE_PATH.read_text()
    # The old committed defaults — must not survive the lockdown.
    assert "algo:algo" not in text
    assert "POSTGRES_PASSWORD: algo" not in text
    assert "redis://redis:6379/0" not in text  # unauthenticated form


def test_postgres_password_is_required_with_no_default():
    compose = _load_compose()
    pg_password = compose["services"]["postgres"]["environment"]["POSTGRES_PASSWORD"]
    # `${VAR:?...}` is the compose idiom for "required, fail loudly if unset".
    assert pg_password.startswith("${POSTGRES_PASSWORD:?")


def test_redis_requires_auth_via_requirepass():
    compose = _load_compose()
    redis_service = compose["services"]["redis"]
    command = redis_service["command"]
    assert "--requirepass" in command
    requirepass_value = command[command.index("--requirepass") + 1]
    assert requirepass_value.startswith("${REDIS_PASSWORD:?")
    # The healthcheck must authenticate too, or it stops reflecting reality
    # once auth is on and silently reports "healthy" forever.
    healthcheck_test = redis_service["healthcheck"]["test"]
    assert "-a" in healthcheck_test


def test_postgres_redis_and_api_ports_are_loopback_bound():
    compose = _load_compose()
    services = compose["services"]
    for service_name in ("postgres", "redis", "api"):
        ports = services[service_name]["ports"]
        assert ports, f"{service_name} defines no ports"
        for binding in ports:
            assert binding.startswith("127.0.0.1:"), (
                f"{service_name} port {binding!r} is not loopback-bound"
            )


def test_every_service_database_url_requires_postgres_password():
    compose = _load_compose()
    for service_name in LOCKED_DOWN_SERVICES_WITH_DB:
        env = compose["services"][service_name]["environment"]
        db_url_entries = [e for e in env if e.startswith("ALGO_DATABASE_URL=")]
        assert db_url_entries, f"{service_name} has no ALGO_DATABASE_URL"
        assert "${POSTGRES_PASSWORD:?" in db_url_entries[0]


def test_every_service_redis_url_requires_redis_password():
    compose = _load_compose()
    for service_name in LOCKED_DOWN_SERVICES_WITH_REDIS:
        env = compose["services"][service_name]["environment"]
        redis_url_entries = [e for e in env if e.startswith("ALGO_REDIS_URL=")]
        assert redis_url_entries, f"{service_name} has no ALGO_REDIS_URL"
        assert "${REDIS_PASSWORD:?" in redis_url_entries[0]
        # Auth goes in the URL's userinfo slot: redis://:<password>@host:port/db
        assert "=redis://:${REDIS_PASSWORD" in redis_url_entries[0]


def test_env_example_documents_required_secrets():
    text = ENV_EXAMPLE_PATH.read_text()
    assert "POSTGRES_PASSWORD=" in text
    assert "REDIS_PASSWORD=" in text
    # It's a template, not a real secret — must ship with empty values.
    for line in text.splitlines():
        if line.startswith("POSTGRES_PASSWORD=") or line.startswith("REDIS_PASSWORD="):
            assert line.split("=", 1)[1].strip() == "", (
                f".env.example must not ship a filled-in secret: {line!r}"
            )


def test_override_example_preserves_loopback_binding_and_documents_migration():
    text = OVERRIDE_EXAMPLE_PATH.read_text()
    assert "127.0.0.1:" in text
    # The critical, easy-to-miss operational nuance: POSTGRES_PASSWORD alone
    # does not rotate the password on an already-initialized volume.
    assert "ALTER USER" in text
    assert "brand-new volume" in text.lower()


LAUNCHD_SCRIPTS_WITH_DB_ONLY = (
    Path("deploy/launchd/run_divergence.sh"),
    Path("ops/launchd/run_sentiment_collect.sh"),
)
LAUNCHD_SCRIPT_WITH_DB_AND_REDIS = Path("deploy/launchd/run_paper.sh")


def test_launchd_scripts_no_longer_hardcode_the_default_password():
    for script in (*LAUNCHD_SCRIPTS_WITH_DB_ONLY, LAUNCHD_SCRIPT_WITH_DB_AND_REDIS):
        text = script.read_text()
        assert "algo:algo" not in text, f"{script} still hardcodes the old default password"
        # Since 2026-08-14 the credential comes from the macOS login keychain
        # via the shared loader rather than a grep against .env (which blocked
        # ~60s per read once .env became a 1Password FIFO). What must hold is
        # that the password is *obtained*, not how.
        assert "algo_load_secrets POSTGRES_PASSWORD" in text, (
            f"{script} does not load POSTGRES_PASSWORD through the shared loader"
        )
        assert "${POSTGRES_PASSWORD}" in text, f"{script} does not use POSTGRES_PASSWORD in its DSN"


def test_run_paper_loads_redis_password_through_the_shared_loader():
    text = LAUNCHD_SCRIPT_WITH_DB_AND_REDIS.read_text()
    assert "algo_load_secrets POSTGRES_PASSWORD REDIS_PASSWORD" in text
    assert "redis://localhost:56379/0" not in text  # old unauthenticated form
    assert 'redis://:${REDIS_PASSWORD}@localhost:56379/0' in text


def test_launchd_scripts_fail_loudly_when_the_password_cannot_be_loaded():
    for script in (*LAUNCHD_SCRIPTS_WITH_DB_ONLY, LAUNCHD_SCRIPT_WITH_DB_AND_REDIS):
        text = script.read_text()
        assert "if ! algo_load_secrets" in text, f"{script} does not branch on a failed load"
        # The reason must reach the operator; a bare "not found" is what made
        # the 2026-08-13/14 aborts look like a config typo.
        assert "$ALGO_SECRETS_ERROR" in text, f"{script} discards the failure reason"
        assert "exit" in text


def test_env_and_override_example_files_are_not_gitignored():
    gitignore = GITIGNORE_PATH.read_text().splitlines()
    # `.env*` covers the whole class: a hand-made `.env copy` held the same live
    # credentials and was previously committable under a name `.env` missed.
    # The negation keeps the tracked example files trackable.
    assert ".env*" in gitignore
    assert "!.env.example" in gitignore
    assert "docker-compose.override.yml" in gitignore
    assert "docker-compose.override.yml.example" not in gitignore
    assert ENV_EXAMPLE_PATH.exists()
    assert OVERRIDE_EXAMPLE_PATH.exists()
