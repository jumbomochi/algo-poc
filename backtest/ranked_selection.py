"""Deterministic complete-universe ranking and candidate replacement rules."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping, AbstractSet


class ReplacementPolicy(StrEnum):
    """Policies evaluated by the offline replacement validation harness."""

    TECHNICAL_ONLY = "technical_only"
    WEAKEST = "weakest"
    SCORE_MARGIN = "score_margin"


@dataclass(frozen=True)
class ReplacementDelta:
    """One rank-driven replacement from an existing to a selected position."""

    outgoing: str
    incoming: str
    score_improvement: float


def rank_complete_universe(
    scores: Mapping[str, float], top_n: int
) -> list[str]:
    """Return the deterministic top ``top_n`` tickers from a complete score map."""
    if top_n <= 0:
        raise ValueError("top_n must be positive")

    invalid = [ticker for ticker, score in scores.items() if not math.isfinite(score)]
    if invalid:
        raise ValueError(f"Non-finite scores for: {', '.join(sorted(invalid))}")

    return [
        ticker
        for ticker, _ in sorted(
            scores.items(), key=lambda item: (-float(item[1]), item[0])
        )[:top_n]
    ]


def target_deltas(
    *,
    held: AbstractSet[str],
    selected: AbstractSet[str],
    scores: Mapping[str, float],
    policy: ReplacementPolicy,
    score_margin: float = 0.0,
) -> list[ReplacementDelta]:
    """Pair selected newcomers with rank-dropped holdings under ``policy``.

    The strongest incoming candidate is paired with the weakest outgoing
    holding. At most one replacement is emitted per available incoming slot.
    Technical-only mode deliberately emits no rank-driven exits.
    """
    policy = ReplacementPolicy(policy)
    if policy is ReplacementPolicy.TECHNICAL_ONLY:
        return []
    if score_margin < 0:
        raise ValueError("score_margin must be non-negative")

    outgoing = set(held) - set(selected)
    incoming = set(selected) - set(held)
    candidates = outgoing | incoming
    missing = candidates - set(scores)
    if missing:
        raise ValueError(f"Missing scores for: {', '.join(sorted(missing))}")

    weakest_first = sorted(outgoing, key=lambda ticker: (scores[ticker], ticker))
    strongest_first = sorted(
        incoming, key=lambda ticker: (-scores[ticker], ticker)
    )

    replacements: list[ReplacementDelta] = []
    for old_ticker, new_ticker in zip(weakest_first, strongest_first, strict=False):
        improvement = float(scores[new_ticker]) - float(scores[old_ticker])
        if (
            policy is ReplacementPolicy.SCORE_MARGIN
            and improvement < score_margin
        ):
            continue
        replacements.append(
            ReplacementDelta(
                outgoing=old_ticker,
                incoming=new_ticker,
                score_improvement=improvement,
            )
        )
    return replacements
