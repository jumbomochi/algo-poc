from __future__ import annotations

from sentiment.scoring import VaderScorer


def test_positive_text_scores_positive():
    scorer = VaderScorer()
    assert scorer.score("Amazing earnings, this stock is a huge winner!") > 0.3


def test_negative_text_scores_negative():
    scorer = VaderScorer()
    assert scorer.score("Terrible guidance, total disaster, selling everything") < -0.3


def test_neutral_text_scores_near_zero():
    scorer = VaderScorer()
    assert abs(scorer.score("The company reported quarterly results")) < 0.3


def test_model_name():
    assert VaderScorer().model_name == "vader"
