from __future__ import annotations

import json
from pathlib import Path

import pytest

from research.evaluation.runcard import build_run_card, write_run_card


def test_run_card_is_provenance_complete_and_deterministic(tmp_path):
    evaluation = {
        "factors": {"price_momentum_126d": {"sharpe": 1.2, "survives_multiple_testing": True}},
        "snapshot_identity": "abc123",
        "provenance": {"data_cutoff": "2026-01-30", "universe_snapshot_id": "u1",
                       "code_revision": "cr1", "input_artifact_checksum": "chk"},
        "config": {"horizon": 21},
    }
    card = build_run_card(evaluation, git_revision="deadbeef", input_checksum="chk")
    assert card["git_revision"] == "deadbeef"
    assert card["snapshot_identity"] == "abc123"
    assert card["provenance"]["data_cutoff"] == "2026-01-30"
    path = write_run_card(card, str(tmp_path))
    assert Path(path).exists()
    assert json.loads(Path(path).read_text()) == card


def test_write_run_card_refuses_output_directory(tmp_path):
    card = {
        "provenance": {"data_cutoff": "2026-01-30"},
    }
    with pytest.raises(ValueError):
        write_run_card(card, str(tmp_path / "output"))
    with pytest.raises(ValueError):
        write_run_card(card, str(tmp_path / "output" / "sub"))

    # A normal directory is allowed and the card is written successfully.
    path = write_run_card(card, str(tmp_path / "cards"))
    assert Path(path).exists()
