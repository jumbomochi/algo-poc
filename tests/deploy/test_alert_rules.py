"""KAN-15 (P1-12) — the alert rules as a tested artifact.

Two layers, deliberately:

* ``tests/deploy/alert_rules_test.yml`` is the behavioural layer — it replays
  a quiet weekend, a no-trade day and every genuine failure mode through the
  real rules with ``promtool test rules``. That is where "does this rule fire
  when it should, and stay silent when it should" is decided.
* This file is the structural layer. It asserts the things promtool cannot:
  that the routing labels every rule needs actually exist, that the rules
  file and the test file have not drifted apart (a rule with no test case is
  a rule nobody replayed), and that CI runs promtool at the same version that
  evaluates the rules in production.

It also runs promtool itself whenever a binary is available, so a local edit
gets the behavioural check without waiting for CI. That is an *addition* to
the CI job, never a substitute: the repo's suite is deliberately
self-contained and must not depend on a binary the runner may not have.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import yaml

ALERT_RULES_PATH = Path("config/alert_rules.yml")
RULES_TEST_PATH = Path("tests/deploy/alert_rules_test.yml")
TESTS_WORKFLOW_PATH = Path(".github/workflows/tests.yml")
OBSERVABILITY_COMPOSE_PATH = Path("docker-compose.observability.yml")
ALERTMANAGER_CONFIG_PATH = Path("config/alertmanager.yml")

# Every label Alertmanager is allowed to route on. `severity` alone was all
# the rules carried before this story, so the only routing decision possible
# was "how urgent", never "who/what/where".
REQUIRED_ROUTING_LABELS = ("severity", "channel", "component")

VALID_SEVERITIES = {"critical", "warning", "info", "none"}
VALID_CHANNELS = {"telegram", "deadman"}
VALID_COMPONENTS = {
    "service-health",
    "pipeline",
    "risk",
    "infrastructure",
    "monitoring",
}

WATCHDOG = "Watchdog"


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _rules() -> list[dict]:
    return [
        rule
        for group in _load_yaml(ALERT_RULES_PATH)["groups"]
        for rule in group["rules"]
    ]


def _rule(name: str) -> dict:
    return next(rule for rule in _rules() if rule["alert"] == name)


# ---------------------------------------------------------------------------
# AC4 — routing labels
# ---------------------------------------------------------------------------


def test_every_rule_carries_the_routing_labels() -> None:
    for rule in _rules():
        labels = rule.get("labels") or {}
        missing = [key for key in REQUIRED_ROUTING_LABELS if key not in labels]
        assert not missing, (
            f"{rule['alert']} is missing routing label(s) {missing} — Alertmanager "
            "would have nothing but severity to route it on"
        )
        assert labels["severity"] in VALID_SEVERITIES, rule["alert"]
        assert labels["channel"] in VALID_CHANNELS, rule["alert"]
        assert labels["component"] in VALID_COMPONENTS, rule["alert"]


def test_only_the_watchdog_uses_the_deadman_channel() -> None:
    """The dead-man webhook is pinged every 5 minutes. Anything else routed
    there would be delivered to a machine, not to a human, and would look
    like a healthy heartbeat rather than an alert."""
    deadman = [
        rule["alert"] for rule in _rules() if rule["labels"]["channel"] == "deadman"
    ]
    assert deadman == [WATCHDOG]


def test_alertmanager_routes_the_watchdog_away_from_telegram() -> None:
    """AC5 — a permanently-firing alert delivered to Telegram would page the
    operator forever and get the bot muted, taking every real alert with it.
    The route must exist, must match on alertname, and must come first
    (Alertmanager sub-routes are first-match-wins)."""
    config = _load_yaml(ALERTMANAGER_CONFIG_PATH)
    routes = config["route"]["routes"]
    first = routes[0]
    assert any("Watchdog" in matcher for matcher in first["matchers"]), (
        f"the first sub-route does not match the Watchdog: {first['matchers']}"
    )
    assert first["receiver"] == "deadman"

    receivers = {receiver["name"]: receiver for receiver in config["receivers"]}
    assert "deadman" in receivers, "the Watchdog routes to a receiver that is not defined"
    assert receivers["deadman"]["webhook_configs"], "the deadman receiver delivers nothing"
    # The fallback the entrypoint repoints at when no URL is configured.
    assert "null" in receivers
    assert not any(
        key.endswith("_configs") for key in receivers["null"]
    ), "the null receiver is supposed to deliver nothing"


def test_the_committed_deadman_url_is_an_unresolvable_placeholder() -> None:
    """A check URL is a bearer capability — anyone holding it can forge a
    healthy ping and switch the dead-man off. It is rendered at container
    start; the committed value must be RFC 2606 `.invalid` so an unrendered
    config cannot reach a real host."""
    config = _load_yaml(ALERTMANAGER_CONFIG_PATH)
    deadman = next(r for r in config["receivers"] if r["name"] == "deadman")
    url = deadman["webhook_configs"][0]["url"]
    assert url.split("/")[2].endswith(".invalid"), url


# ---------------------------------------------------------------------------
# AC5 — the dead-man's switch
# ---------------------------------------------------------------------------


def test_a_watchdog_rule_fires_unconditionally() -> None:
    rule = _rule(WATCHDOG)
    assert rule["expr"].strip() == "vector(1)", (
        "the Watchdog must not depend on any series — a dead-man's switch that "
        "needs an input can be silenced by losing that input"
    )
    assert "for" not in rule, "a Watchdog with a `for:` is blind during its own debounce"
    assert rule["labels"]["severity"] == "none"


# ---------------------------------------------------------------------------
# AC3 / AC9 — the cadence rules no longer assume a continuous intraday system
# ---------------------------------------------------------------------------


def test_the_cadence_sensitive_rules_require_upstream_activity() -> None:
    """The retune's load-bearing property, asserted structurally so a future
    "simplification" back to a bare idle check fails here as well as in the
    promtool replay.

    A rule of the form `increase(<downstream>[w]) == 0` alone is true all
    night, all weekend, on every NYSE holiday and on every all-SKIP day. Each
    of these two rules must therefore also require that something upstream
    actually moved.
    """
    for name, upstream in (
        ("ApprovedOrdersStreamIdle", "stream:recommendations"),
        ("NoFillsRecently", "stream:approved_orders"),
    ):
        expr = _rule(name)["expr"]
        assert f'key="{upstream}"' in expr, (
            f"{name} does not gate on {upstream} — it can fire on a day when "
            "nothing was supposed to happen"
        )
        assert "> 0" in expr, f"{name} has no upstream-activity clause"


def test_the_no_fills_window_spans_a_weekend() -> None:
    """The 04:15 SGT run places orders ~15 minutes after the US close, so they
    cannot fill until the next session — and a Friday run's orders wait until
    Monday. Any window shorter than a weekend pages every Saturday."""
    assert "[3d]" in _rule("NoFillsRecently")["expr"]


def test_the_not_pager_ready_caveat_is_gone() -> None:
    """AC9 — the header used to warn that these thresholds were not safe to
    wire to a pager. KAN-14 wired them to one. Either the caveat is no longer
    true or the rules are not done; it must not be left standing as decoration.
    """
    text = ALERT_RULES_PATH.read_text()
    assert "IMPORTANT CADENCE CAVEAT" not in text
    assert "before wiring any of this to a pager" not in text
    # But the cadence itself still has to be documented, or the next person
    # retunes these back to intraday windows.
    assert "ONCE A DAY" in text


# ---------------------------------------------------------------------------
# Rules file <-> promtool test file drift
# ---------------------------------------------------------------------------


def _tested_alert_names() -> set[str]:
    suite = _load_yaml(RULES_TEST_PATH)
    return {
        case["alertname"]
        for group in suite["tests"]
        for case in group.get("alert_rule_test", [])
    }


def _alert_names_expected_to_fire() -> set[str]:
    suite = _load_yaml(RULES_TEST_PATH)
    return {
        case["alertname"]
        for group in suite["tests"]
        for case in group.get("alert_rule_test", [])
        if case.get("exp_alerts")
    }


def test_every_rule_has_a_promtool_case_that_makes_it_fire() -> None:
    """AC2. Silence is cheap to achieve by accident — a typo in a metric name
    produces a rule that never fires and never complains. Every rule must be
    driven to fire at least once by the replay."""
    defined = {rule["alert"] for rule in _rules()}
    fires = _alert_names_expected_to_fire()
    assert defined - fires == set(), (
        f"no promtool case makes {sorted(defined - fires)} fire — those rules "
        "could be silently broken and nothing would notice"
    )


def test_the_promtool_suite_only_references_rules_that_exist() -> None:
    defined = {rule["alert"] for rule in _rules()}
    assert _tested_alert_names() - defined == set()


def test_the_quiet_period_replay_covers_every_rule_but_the_watchdog() -> None:
    """AC1 — "produces zero firing alerts other than Watchdog" is only a real
    claim if the quiet scenario actually asserts on every rule."""
    suite = _load_yaml(RULES_TEST_PATH)
    quiet = next(
        group
        for group in suite["tests"]
        if "quiet weekend" in group["name"]
    )
    silent = {
        case["alertname"]
        for case in quiet["alert_rule_test"]
        if not case.get("exp_alerts")
    }
    defined = {rule["alert"] for rule in _rules()}
    assert defined - silent == {WATCHDOG}, (
        f"the quiet-weekend replay does not assert silence for "
        f"{sorted(defined - silent - {WATCHDOG})}"
    )
    firing = {
        case["alertname"]
        for case in quiet["alert_rule_test"]
        if case.get("exp_alerts")
    }
    assert firing == {WATCHDOG}


def test_the_promtool_suite_points_at_the_real_rules_file() -> None:
    suite = _load_yaml(RULES_TEST_PATH)
    resolved = [
        (RULES_TEST_PATH.parent / rule_file).resolve()
        for rule_file in suite["rule_files"]
    ]
    assert resolved == [ALERT_RULES_PATH.resolve()]


# ---------------------------------------------------------------------------
# CI wiring
# ---------------------------------------------------------------------------


def test_ci_replays_the_rules_with_promtool() -> None:
    workflow = TESTS_WORKFLOW_PATH.read_text()
    assert f"promtool test rules {RULES_TEST_PATH.as_posix()}" in workflow
    assert "promtool check rules config/alert_rules.yml" in workflow


def test_ci_promtool_version_matches_the_pinned_prometheus_image() -> None:
    """Evaluating the rules with a different Prometheus than the one that runs
    them is a green check that proves nothing — same reasoning as the amtool
    job."""
    compose = _load_yaml(OBSERVABILITY_COMPOSE_PATH)
    tag = compose["services"]["prometheus"]["image"].partition(":")[2]
    workflow = TESTS_WORKFLOW_PATH.read_text()
    assert f'PROMETHEUS_VERSION: "{tag.lstrip("v")}"' in workflow


def test_ci_checks_that_every_severity_still_reaches_a_receiver() -> None:
    """AC4's delivery half: `amtool check-config` proves the file parses, not
    that a severity still lands somewhere a human reads."""
    workflow = TESTS_WORKFLOW_PATH.read_text()
    assert "amtool config routes test" in workflow


# ---------------------------------------------------------------------------
# Behavioural, when a promtool binary happens to be available.
# ---------------------------------------------------------------------------


def test_promtool_replay_passes_when_promtool_is_available() -> None:
    """Runs the real replay locally. Not the primary gate — CI's `promtool
    test rules` job is, because this cannot fail on a runner with no promtool
    and the repo's suite must never depend on one."""
    promtool = shutil.which("promtool")
    if promtool is None:
        return
    result = subprocess.run(
        [promtool, "test", "rules", str(RULES_TEST_PATH)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
