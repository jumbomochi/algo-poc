"""KAN-35 (D17): the ml_model service path is demoted, not deleted.

``signal-generation`` published only to ``stream:signals``, whose only consumer
was ``ml-model``, which never loaded a model — ``model_versions`` has no rows —
and so never fed the money path. ``scripts/run_paper.py`` is the only producer
of ``stream:recommendations`` that has ever mattered. The pair are removed from
the running stack; the training and registry code stays as an offline tool so a
model can be trained and evaluated before anyone decides to integrate it
(docs/decisions/ml-path-2026-09.md).
"""
from __future__ import annotations

from pathlib import Path

import yaml

from shared.config import MLModelConfig

REPO = Path(__file__).resolve().parents[2]
DEMOTED = ("ml-model", "signal-generation")


def _services(path: str) -> dict:
    return yaml.safe_load((REPO / path).read_text())["services"]


def test_compose_no_longer_starts_the_demoted_services():
    services = _services("docker-compose.yml")
    for name in DEMOTED:
        assert name not in services, name


def test_risk_management_starts_without_ml_model():
    deps = _services("docker-compose.yml")["risk-management"].get("depends_on", {})
    assert "ml-model" not in deps


def test_every_dependency_is_a_declared_service():
    """A depends_on naming an undeclared service fails `docker compose up` for
    the whole stack — including the observability overlay's prometheus."""
    base = _services("docker-compose.yml")
    overlay = _services("docker-compose.observability.yml")
    declared = set(base) | set(overlay)
    for name, svc in {**base, **overlay}.items():
        for dep in svc.get("depends_on", []) or []:
            assert dep in declared, f"{name} depends on undeclared {dep}"


def test_monitoring_does_not_expect_the_demoted_services():
    """A scrape target that can never exist is a permanent ServiceMetricsEndpointDown."""
    scrape = yaml.safe_load((REPO / "config/prometheus.yml").read_text())
    jobs = {job["job_name"] for job in scrape["scrape_configs"]}
    rules = (REPO / "config/alert_rules.yml").read_text()
    for name in DEMOTED:
        assert name not in jobs, name
        assert name not in rules, name


def test_retrain_cadence_is_not_configured():
    """AC5: nothing schedules a retrain, so a cadence key is a claim the system
    does not keep. Retraining is an operator-run offline step."""
    assert "retrain_cadence_months" not in MLModelConfig.model_fields
    cfg = yaml.safe_load((REPO / "config/default.yaml").read_text())
    assert "retrain_cadence_months" not in cfg["ml_model"]


def test_offline_training_tooling_is_kept():
    """Demoted, not deleted: models are trained and evaluated offline first."""
    from services.ml_model.registry import ModelRegistry  # noqa: F401
    from services.ml_model.trainer import ModelTrainer  # noqa: F401

    for script in ("scripts/train_signal_model.py", "scripts/retrain_model.py"):
        assert (REPO / script).is_file(), script
