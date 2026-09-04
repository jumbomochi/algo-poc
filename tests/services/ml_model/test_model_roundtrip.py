"""A model written by the retrainer must be loadable by the registry.

`scripts/retrain_model.py` writes the model with LightGBM's native
``Booster.save_model`` to a ``.txt`` and records a ``ModelVersion`` row pointing
at it. ``ModelRegistry.load_active`` reads that row and calls ``joblib.load``.
The two cannot interoperate, so the model the retrainer promotes can never be
loaded through its own registry.

Two faults are stacked here and the second is the one usually named:

1. the retrainer records **no ``content_hash``**, and ``_verify_integrity``
   refuses to load a row without one — so this fires first;
2. ``joblib.load`` on a LightGBM ``.txt`` fails even once a hash exists.

Neither is reachable today: ``load_active`` is only called by
``MLServiceRunner``, ``stream:signals`` is empty, and there are no
``model_versions`` rows at all — and every one of those failures is swallowed by
``_handle_signal``'s ``except Exception``, which logs a warning and leaves the
signal buffered. The defect is latent, not benign: it fires the day a model is
first registered and the docker path is fed.

These tests use a real LightGBM Booster rather than a stub, because a stub would
round-trip through joblib happily and prove nothing about the actual formats.
"""
from __future__ import annotations

from datetime import date

import lightgbm as lgb
import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from services.ml_model.registry import ModelIntegrityError, ModelRegistry
from shared.models.base import Base
from shared.models.ml_models import ModelVersion


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'ml.db'}")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def _booster() -> lgb.Booster:
    """A genuine 3-class Booster, matching what ModelTrainer.train returns."""
    rng = np.random.default_rng(0)
    x = pd.DataFrame(rng.normal(size=(60, 3)), columns=["a", "b", "c"])
    y = rng.integers(0, 3, size=60)
    return lgb.train(
        {"objective": "multiclass", "num_class": 3, "verbosity": -1},
        lgb.Dataset(x, label=y),
        num_boost_round=3,
    )


def test_a_native_lightgbm_file_round_trips_through_the_registry(db, tmp_path) -> None:
    """The property the retrainer needs and does not have: write with
    save_model, load through the registry."""
    model = _booster()
    path = tmp_path / "signal_quality_v1.txt"
    model.save_model(str(path))

    db.add(ModelVersion(
        version="v1", training_window_start=date(2026, 1, 1),
        training_window_end=date(2026, 6, 1), metrics={},
        model_path=str(path), is_active=True,
        created_at=date(2026, 6, 2), content_hash=ModelRegistry.hash_model_file(str(path)),
    ))
    db.commit()

    loaded, version = ModelRegistry(db, str(tmp_path)).load_active()

    assert version == "v1"
    assert loaded.predict(pd.DataFrame([[0.1, 0.2, 0.3]], columns=["a", "b", "c"])).shape == (1, 3)


def test_a_joblib_file_still_round_trips(db, tmp_path) -> None:
    """The registry's own save() path must keep working — this is a widening,
    not a swap."""
    registry = ModelRegistry(db, str(tmp_path))
    path = registry.save(_booster(), "v2", {}, (date(2026, 1, 1), date(2026, 6, 1)))
    db.query(ModelVersion).filter_by(version="v2").one().is_active = True
    db.commit()

    loaded, version = registry.load_active()

    assert version == "v2"
    assert path.endswith(".joblib")
    assert loaded.predict(pd.DataFrame([[0.1, 0.2, 0.3]], columns=["a", "b", "c"])).shape == (1, 3)


def test_a_row_without_a_content_hash_is_still_refused(db, tmp_path) -> None:
    """The integrity guard is not weakened by widening the format support: an
    unverifiable model file is still refused, whatever its extension."""
    path = tmp_path / "signal_quality_v3.txt"
    _booster().save_model(str(path))
    db.add(ModelVersion(
        version="v3", training_window_start=date(2026, 1, 1),
        training_window_end=date(2026, 6, 1), metrics={},
        model_path=str(path), is_active=True, created_at=date(2026, 6, 2),
    ))
    db.commit()

    with pytest.raises(ModelIntegrityError):
        ModelRegistry(db, str(tmp_path)).load_active()


def test_a_tampered_native_file_is_refused(db, tmp_path) -> None:
    """Widening the loader must not create a path that skips the hash check."""
    path = tmp_path / "signal_quality_v4.txt"
    _booster().save_model(str(path))
    good = ModelRegistry.hash_model_file(str(path))
    path.write_text(path.read_text() + "\n# tampered\n")

    db.add(ModelVersion(
        version="v4", training_window_start=date(2026, 1, 1),
        training_window_end=date(2026, 6, 1), metrics={},
        model_path=str(path), is_active=True, created_at=date(2026, 6, 2),
        content_hash=good,
    ))
    db.commit()

    with pytest.raises(ModelIntegrityError):
        ModelRegistry(db, str(tmp_path)).load_active()


# ---------------------------------------------------------------------------
# the retrainer's own row
# ---------------------------------------------------------------------------


def test_the_retrainer_records_a_content_hash() -> None:
    """Without one, _verify_integrity refuses every model the retrainer
    promotes — the first of the two stacked faults, and the one that fires."""
    source = (
        __import__("pathlib").Path(__file__).resolve().parents[3]
        / "scripts" / "retrain_model.py"
    ).read_text()

    assert "content_hash" in source, (
        "retrain_model.py records no content_hash on the ModelVersion row it "
        "writes, so ModelRegistry.load_active refuses to load it"
    )


def test_the_retrainer_hashes_the_file_it_actually_recorded() -> None:
    """A hash of a different file is worse than none: it would pass the
    integrity check while describing the wrong bytes."""
    source = (
        __import__("pathlib").Path(__file__).resolve().parents[3]
        / "scripts" / "retrain_model.py"
    ).read_text()

    assert "hash_model_file(versioned_path)" in source, (
        "the recorded hash must be of versioned_path — the same path stored in "
        "ModelVersion.model_path"
    )
