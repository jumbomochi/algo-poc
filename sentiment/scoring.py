from __future__ import annotations

from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer


class VaderScorer:
    """Local sentiment scoring. score() returns VADER's compound in [-1, 1].

    model_name is persisted per row (score_model column) so a heavier model
    can re-score the archive later without provenance ambiguity.
    """

    model_name = "vader"

    def __init__(self) -> None:
        self._analyzer = SentimentIntensityAnalyzer()

    def score(self, text: str) -> float:
        return self._analyzer.polarity_scores(text)["compound"]
