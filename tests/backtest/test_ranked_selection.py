from __future__ import annotations

import pytest

from backtest.ranked_selection import (
    ReplacementDelta,
    ReplacementPolicy,
    rank_complete_universe,
    target_deltas,
)


def test_ranking_is_independent_of_input_order():
    scores = {"A": 1.0, "B": 3.0, "C": 2.0}

    assert rank_complete_universe(scores, top_n=2) == ["B", "C"]
    assert rank_complete_universe(
        dict(reversed(list(scores.items()))), top_n=2
    ) == ["B", "C"]


def test_ranking_breaks_equal_score_ties_by_ticker():
    assert rank_complete_universe({"C": 1.0, "A": 1.0, "B": 1.0}, 2) == [
        "A",
        "B",
    ]


def test_ranking_rejects_non_positive_top_n():
    with pytest.raises(ValueError, match="top_n must be positive"):
        rank_complete_universe({"A": 1.0}, top_n=0)


def test_technical_only_policy_never_sells_on_rank_drop():
    actions = target_deltas(
        held={"A"},
        selected={"B"},
        scores={"A": 1.0, "B": 2.0},
        policy=ReplacementPolicy.TECHNICAL_ONLY,
    )

    assert actions == []


def test_weakest_policy_pairs_best_incoming_with_weakest_holding():
    actions = target_deltas(
        held={"A", "C"},
        selected={"B", "D"},
        scores={"A": 1.0, "B": 4.0, "C": 2.0, "D": 3.0},
        policy=ReplacementPolicy.WEAKEST,
    )

    assert actions == [
        ReplacementDelta(outgoing="A", incoming="B", score_improvement=3.0),
        ReplacementDelta(outgoing="C", incoming="D", score_improvement=1.0),
    ]


def test_margin_policy_replaces_only_above_threshold():
    assert target_deltas(
        held={"A"},
        selected={"B"},
        scores={"A": 1.0, "B": 1.2},
        policy=ReplacementPolicy.SCORE_MARGIN,
        score_margin=0.25,
    ) == []

    assert target_deltas(
        held={"A"},
        selected={"B"},
        scores={"A": 1.0, "B": 1.25},
        policy=ReplacementPolicy.SCORE_MARGIN,
        score_margin=0.25,
    ) == [
        ReplacementDelta(outgoing="A", incoming="B", score_improvement=0.25)
    ]


def test_target_deltas_require_scores_for_all_candidates():
    with pytest.raises(ValueError, match="Missing scores"):
        target_deltas(
            held={"A"},
            selected={"B"},
            scores={"B": 2.0},
            policy=ReplacementPolicy.WEAKEST,
        )
