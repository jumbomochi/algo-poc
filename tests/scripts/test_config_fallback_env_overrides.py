"""A failed config load must not silently point a script at the wrong database.

Both `run_paper.py` and `divergence_monitor.py` compute their `--db-url` /
`--redis-url` defaults from `load_config("config/default.yaml")`, falling back
to a hardcoded `algo:algo@localhost:5432` when that raises (missing file,
unreadable, bad YAML, or simply a different working directory).

`shared/config.py` maps `ALGO_DATABASE_URL -> database.url` and
`ALGO_REDIS_URL -> redis.url`, so the environment already wins on the happy
path. Before the fallback honoured those variables too, the two paths
disagreed: a config-load failure sent the daily run at a *different* database
than every successful run, while the launchd wrappers exported the correct one.
Post-T3 that surfaces as an auth error rather than an obvious misconfiguration,
and pre-T3 it would have written to whatever answered on 5432.

These tests pin the agreement in both directions: the environment wins when
set, and the hardcoded default is used only when nothing is set.
"""

from __future__ import annotations

import argparse
import importlib
from types import ModuleType

import pytest

ENV_VARS = ("ALGO_DATABASE_URL", "ALGO_REDIS_URL")


def _default_after_failed_config_load(
    module: ModuleType, dest: str, monkeypatch: pytest.MonkeyPatch
) -> str:
    """Return the parser default for `dest` when config loading fails.

    Forces the `except` branch by making the module's `load_config` raise, then
    reads the default off the real parser the script builds — so the assertion
    covers the production code path rather than a re-implementation of it.
    `parse_args` is intercepted because `main()` would otherwise continue into
    the run itself.
    """
    monkeypatch.setattr(
        module,
        "load_config",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("forced config load failure")
        ),
    )

    captured: dict[str, str] = {}

    def capture_and_stop(self: argparse.ArgumentParser, *args, **kwargs):
        captured["value"] = self.get_default(dest)
        raise SystemExit(0)

    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", capture_and_stop)

    with pytest.raises(SystemExit):
        module.main()

    return captured["value"]


@pytest.fixture(params=["scripts.run_paper", "scripts.divergence_monitor"])
def script_module(request: pytest.FixtureRequest) -> ModuleType:
    """Both scripts share the fallback, so both must satisfy the same contract."""
    return importlib.import_module(request.param)


class TestDatabaseUrlFallback:
    def test_env_override_wins_when_config_cannot_be_loaded(
        self, script_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        expected = "postgresql://someone:secret@localhost:55432/algo_poc"
        monkeypatch.setenv("ALGO_DATABASE_URL", expected)

        assert (
            _default_after_failed_config_load(script_module, "db_url", monkeypatch)
            == expected
        )

    def test_hardcoded_default_used_only_when_env_is_unset(
        self, script_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for name in ENV_VARS:
            monkeypatch.delenv(name, raising=False)

        default = _default_after_failed_config_load(
            script_module, "db_url", monkeypatch
        )

        assert default == "postgresql://algo:algo@localhost:5432/algo_poc"


class TestRedisUrlFallback:
    """Only run_paper.py takes a Redis URL; divergence_monitor.py does not."""

    def test_env_override_wins_when_config_cannot_be_loaded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        module = importlib.import_module("scripts.run_paper")
        expected = "redis://:secret@localhost:56379/0"
        monkeypatch.setenv("ALGO_REDIS_URL", expected)

        assert (
            _default_after_failed_config_load(module, "redis_url", monkeypatch)
            == expected
        )

    def test_hardcoded_default_used_only_when_env_is_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        module = importlib.import_module("scripts.run_paper")
        for name in ENV_VARS:
            monkeypatch.delenv(name, raising=False)

        default = _default_after_failed_config_load(
            module, "redis_url", monkeypatch
        )

        assert default == "redis://localhost:6379/0"
