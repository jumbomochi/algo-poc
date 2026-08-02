"""Offline factor evaluation subsystem (nested walk-forward, multiple-testing, overlap)."""

from __future__ import annotations

from research.evaluation.evaluator import EvaluationConfig, evaluate_factors
from research.evaluation.runcard import build_run_card, write_run_card

__all__ = ["EvaluationConfig", "evaluate_factors", "build_run_card", "write_run_card"]
