from __future__ import annotations

import os
import tempfile
from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from services.ml_model.registry import ModelRegistry
from services.ml_model.trainer import ModelTrainer
from shared.models.ml_models import ModelVersion


@pytest.fixture()
def model_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture()
def trained_model():
    np.random.seed(42)
    n = 300
    features = pd.DataFrame({
        "support_proximity": np.random.randn(n),
        "support_strength": np.random.randn(n),
        "support_trend": np.random.randn(n),
        "valuation": np.random.randn(n),
        "quality": np.random.randn(n),
        "growth": np.random.randn(n),
        "earnings_surprise": np.random.randn(n),
        "news_sentiment": np.random.randn(n),
        "insider_activity": np.random.randn(n),
    })
    targets = pd.Series(np.random.choice(["sell", "hold", "buy"], n))
    trainer = ModelTrainer()
    model, _ = trainer.train(features, targets)
    return model


@pytest.fixture()
def mock_db():
    """Create a mock DB session that tracks ModelVersion objects."""
    session = MagicMock()
    session._versions = []  # internal store for test assertions

    def add_side_effect(obj):
        session._versions.append(obj)

    session.add.side_effect = add_side_effect

    def query_side_effect(model_class):
        q = MagicMock()

        def filter_by_side_effect(**kwargs):
            f = MagicMock()
            if "is_active" in kwargs and kwargs["is_active"]:
                active = [v for v in session._versions if v.is_active]
                f.first.return_value = active[0] if active else None
                f.all.return_value = active
                f.update = MagicMock()
            elif "version" in kwargs:
                matches = [v for v in session._versions if v.version == kwargs["version"]]
                f.first.return_value = matches[0] if matches else None
                f.all.return_value = matches
                f.update = MagicMock()
            else:
                f.first.return_value = session._versions[0] if session._versions else None
                f.all.return_value = session._versions
                f.update = MagicMock()
            return f

        def order_by_side_effect(*args):
            ob = MagicMock()
            ob.filter_by = filter_by_side_effect
            sorted_versions = sorted(
                session._versions,
                key=lambda v: v.created_at,
                reverse=True,
            )
            ob.all.return_value = sorted_versions
            ob.first.return_value = sorted_versions[0] if sorted_versions else None

            def ob_filter_by(**kwargs):
                ff = MagicMock()
                if "is_active" in kwargs:
                    filtered = [v for v in sorted_versions if v.is_active == kwargs["is_active"]]
                    ff.all.return_value = filtered
                    ff.first.return_value = filtered[0] if filtered else None
                else:
                    ff.all.return_value = sorted_versions
                    ff.first.return_value = sorted_versions[0] if sorted_versions else None
                return ff

            ob.filter_by = ob_filter_by
            return ob

        q.filter_by = filter_by_side_effect
        q.order_by = order_by_side_effect
        q.all.return_value = session._versions
        return q

    session.query.side_effect = query_side_effect
    return session


class TestModelRegistry:
    def test_save_creates_model_file(self, trained_model, model_dir, mock_db):
        registry = ModelRegistry(mock_db, model_dir)
        version = "v1.0.0"
        metrics = {"accuracy": 0.85}
        window = (date(2024, 1, 1), date(2024, 6, 30))

        path = registry.save(trained_model, version, metrics, window)

        assert os.path.exists(path)
        assert version in path

    def test_save_records_in_db(self, trained_model, model_dir, mock_db):
        registry = ModelRegistry(mock_db, model_dir)
        version = "v1.0.0"
        metrics = {"accuracy": 0.85}
        window = (date(2024, 1, 1), date(2024, 6, 30))

        registry.save(trained_model, version, metrics, window)

        mock_db.add.assert_called_once()
        mock_db.commit.assert_called_once()
        saved = mock_db._versions[0]
        assert saved.version == version
        assert saved.metrics == metrics
        assert saved.training_window_start == window[0]
        assert saved.training_window_end == window[1]

    def test_load_active_returns_model_and_version(self, trained_model, model_dir, mock_db):
        registry = ModelRegistry(mock_db, model_dir)
        version = "v1.0.0"
        metrics = {"accuracy": 0.85}
        window = (date(2024, 1, 1), date(2024, 6, 30))

        registry.save(trained_model, version, metrics, window)
        # Manually set active for mock
        mock_db._versions[0].is_active = True

        loaded_model, loaded_version = registry.load_active()

        assert loaded_model is not None
        assert loaded_version == version

    def test_load_active_raises_when_no_active(self, model_dir, mock_db):
        registry = ModelRegistry(mock_db, model_dir)

        with pytest.raises(ValueError, match="No active model"):
            registry.load_active()

    def test_activate_sets_version_active(self, trained_model, model_dir, mock_db):
        registry = ModelRegistry(mock_db, model_dir)

        # Save two versions
        registry.save(trained_model, "v1.0.0", {"accuracy": 0.80},
                       (date(2024, 1, 1), date(2024, 3, 31)))
        registry.save(trained_model, "v2.0.0", {"accuracy": 0.85},
                       (date(2024, 1, 1), date(2024, 6, 30)))

        registry.activate("v2.0.0")

        # Check only v2 is active
        v2 = [v for v in mock_db._versions if v.version == "v2.0.0"][0]
        assert v2.is_active is True

    def test_activate_raises_for_unknown_version(self, model_dir, mock_db):
        registry = ModelRegistry(mock_db, model_dir)

        with pytest.raises(ValueError, match="not found"):
            registry.activate("v99.0.0")

    def test_rollback_activates_previous(self, trained_model, model_dir, mock_db):
        registry = ModelRegistry(mock_db, model_dir)

        registry.save(trained_model, "v1.0.0", {"accuracy": 0.80},
                       (date(2024, 1, 1), date(2024, 3, 31)))
        registry.save(trained_model, "v2.0.0", {"accuracy": 0.85},
                       (date(2024, 1, 1), date(2024, 6, 30)))

        registry.activate("v2.0.0")
        rolled_back = registry.rollback()

        assert rolled_back == "v1.0.0"

    def test_rollback_raises_when_no_previous(self, trained_model, model_dir, mock_db):
        registry = ModelRegistry(mock_db, model_dir)
        registry.save(trained_model, "v1.0.0", {"accuracy": 0.80},
                       (date(2024, 1, 1), date(2024, 3, 31)))
        registry.activate("v1.0.0")

        with pytest.raises(ValueError, match="No previous"):
            registry.rollback()
