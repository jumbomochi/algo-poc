from __future__ import annotations

import hashlib
import os
from datetime import date, datetime, timezone
from typing import Any

import joblib

from shared.models.ml_models import ModelVersion

# Sidecar file suffix holding the sha256 of the model file at save time.
# Kept as a plain file next to the model artifact (not a DB column) so this
# integrity check is self-contained: it doesn't require a schema migration
# and doesn't touch the ModelVersion row that a later loader-consolidation
# thread (T8) may still be reshaping.
_INTEGRITY_SUFFIX = ".sha256"


def _hash_file(path: str) -> str:
    """Return the hex sha256 digest of a file's contents, streamed in chunks."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ModelIntegrityError(RuntimeError):
    """Raised when a model file's content hash doesn't match its recorded
    integrity record, or the record is missing entirely.

    joblib.load deserializes arbitrary Python objects (it's built on
    pickle) — loading a model file that was modified after save() would be
    untrusted deserialization. This error means "refuse to load", never
    "log and continue."
    """


class ModelRegistry:
    """Manages ML model versioning, persistence, and activation.

    Saves trained models to disk and records metadata in the database.
    Supports loading the active model, activating specific versions,
    and rolling back to the previous version.
    """

    def __init__(self, db_session: Any, model_dir: str = "models/"):
        self._db = db_session
        self._model_dir = model_dir
        os.makedirs(self._model_dir, exist_ok=True)

    def save(
        self,
        model: Any,
        version: str,
        metrics: dict[str, Any],
        training_window: tuple[date, date],
    ) -> str:
        """Save a trained model to disk and record in the database.

        Args:
            model: The trained model object (e.g., LightGBM Booster).
            version: Version string (e.g., "v1.0.0").
            metrics: Dict of training metrics.
            training_window: Tuple of (start_date, end_date) for training data.

        Returns:
            Path to the saved model file.
        """
        model_path = os.path.join(self._model_dir, f"{version}.joblib")
        joblib.dump(model, model_path)

        content_hash = _hash_file(model_path)
        with open(model_path + _INTEGRITY_SUFFIX, "w") as f:
            f.write(content_hash)

        record = ModelVersion(
            version=version,
            training_window_start=training_window[0],
            training_window_end=training_window[1],
            metrics=metrics,
            model_path=model_path,
            is_active=False,
            created_at=datetime.now(timezone.utc),
        )
        self._db.add(record)
        self._db.commit()

        return model_path

    def load_active(self) -> tuple[Any, str]:
        """Load the currently active model from disk.

        Returns:
            Tuple of (model, version_string).

        Raises:
            ValueError: If no active model is found.
            ModelIntegrityError: If the model file's content hash doesn't
                match its recorded integrity record, or no record exists.
        """
        record = (
            self._db.query(ModelVersion)
            .filter_by(is_active=True)
            .first()
        )
        if record is None:
            raise ValueError("No active model found in registry")

        self._verify_integrity(record.model_path, record.version)

        model = joblib.load(record.model_path)
        return model, record.version

    def _verify_integrity(self, model_path: str, version: str) -> None:
        """Verify a model file's content hash before it is deserialized."""
        hash_path = model_path + _INTEGRITY_SUFFIX
        if not os.path.exists(hash_path):
            raise ModelIntegrityError(
                f"Model '{version}' has no integrity record ({hash_path} "
                "missing); refusing to load an unverifiable model file"
            )
        with open(hash_path) as f:
            recorded_hash = f.read().strip()

        actual_hash = _hash_file(model_path)
        if actual_hash != recorded_hash:
            raise ModelIntegrityError(
                f"Model '{version}' integrity check failed: content hash of "
                f"{model_path} does not match the recorded value — the file "
                "may have been modified or corrupted since it was saved"
            )

    def activate(self, version: str) -> None:
        """Set a specific version as the active model.

        Deactivates all other versions first.

        Args:
            version: Version string to activate.

        Raises:
            ValueError: If the version is not found.
        """
        record = (
            self._db.query(ModelVersion)
            .filter_by(version=version)
            .first()
        )
        if record is None:
            raise ValueError(f"Model version '{version}' not found")

        # Deactivate all versions
        for v in self._db.query(ModelVersion).all():
            v.is_active = False

        # Activate the requested version
        record.is_active = True
        self._db.commit()

    def rollback(self) -> str:
        """Activate the previous model version (by creation date).

        Returns:
            The version string that was activated.

        Raises:
            ValueError: If no previous version exists.
        """
        # Get all versions ordered by creation date (newest first)
        all_versions = (
            self._db.query(ModelVersion)
            .order_by(ModelVersion.created_at.desc())
            .all()
        )

        if len(all_versions) < 2:
            raise ValueError("No previous model version to roll back to")

        # Find the currently active version
        active_idx = None
        for i, v in enumerate(all_versions):
            if v.is_active:
                active_idx = i
                break

        if active_idx is None or active_idx >= len(all_versions) - 1:
            raise ValueError("No previous model version to roll back to")

        # Activate the next older version
        previous = all_versions[active_idx + 1]
        self.activate(previous.version)

        return previous.version
