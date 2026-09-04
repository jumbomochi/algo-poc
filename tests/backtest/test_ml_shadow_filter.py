"""Scoring a sleeve's buy signals without acting on them.

The trained signal-quality model passes only **17-20%** of buy signals
(`models/signal_quality_metrics.json`, purged walk-forward folds). Wiring that
straight into `run_paper.py` would cut the live book's entries by four fifths in
one step, on a model whose own metadata says it is out of sample only from
2026-08-17.

So the filter runs in shadow first: it scores every buy, records what it WOULD
have done, and suppresses nothing. The recorded decisions can then be joined
against real fills to see whether the +10pp win-rate improvement measured in
walk-forward survives contact with the live book.

The scoring itself is shared with `make_ml_filtered_signals_fn` rather than
reimplemented — a shadow that scores differently from the live filter would
measure the wrong thing.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from scripts.run_backtest import make_ml_shadow_signals_fn


SESSIONS = [date(2026, 1, 5) + timedelta(days=i) for i in range(30)]


def _bars() -> list[dict]:
    return [
        {"date": d, "open": 100.0, "high": 101.0, "low": 99.0,
         "close": 100.0 + i, "volume": 1_000_000}
        for i, d in enumerate(SESSIONS)
    ]


class _Model:
    """Stands in for a Booster. Returns a fixed score."""

    def __init__(self, score: float):
        self._score = score

    def feature_name(self):
        return ["portfolio", "signal_rank", "bar_return_5d"]

    def predict(self, df):
        return [self._score]


def _buy(ticker="AAA"):
    return {"action": "buy", "ticker": ticker, "limit_price": 100.0,
            "quantity": 5.0, "sector": "Tech", "signals": {"rank": 1}}


def _sell(ticker="AAA"):
    return {"action": "sell", "ticker": ticker, "exit_reason": "trailing_stop"}


def _fn(inner, score, threshold=0.5, recorder=None):
    return make_ml_shadow_signals_fn(
        inner, _Model(score), threshold=threshold,
        strategy_name="momentum", recorder=recorder,
    )


# --- the defining property: it changes nothing ------------------------------


def test_a_signal_the_model_would_suppress_still_passes_through() -> None:
    """Shadow mode must not alter what the book trades. A score far below the
    threshold is still returned unchanged."""
    recorded: list[dict] = []
    fn = _fn(lambda t, b: _buy(), score=0.01, recorder=recorded.append)

    out = fn("AAA", _bars())

    assert out == _buy()


def test_a_signal_the_model_would_pass_also_passes_through() -> None:
    fn = _fn(lambda t, b: _buy(), score=0.99, recorder=lambda r: None)

    assert fn("AAA", _bars()) == _buy()


def test_sells_are_never_scored() -> None:
    """The live filter passes sells unconditionally; the shadow must agree, or
    the recorded population would not match what the filter would act on."""
    recorded: list[dict] = []
    fn = _fn(lambda t, b: _sell(), score=0.01, recorder=recorded.append)

    assert fn("AAA", _bars()) == _sell()
    assert recorded == []


def test_no_signal_records_nothing() -> None:
    recorded: list[dict] = []
    fn = _fn(lambda t, b: None, score=0.01, recorder=recorded.append)

    assert fn("AAA", _bars()) is None
    assert recorded == []


# --- what it records --------------------------------------------------------


def test_it_records_the_decision_it_would_have_made() -> None:
    recorded: list[dict] = []
    _fn(lambda t, b: _buy(), score=0.01, recorder=recorded.append)("AAA", _bars())

    assert len(recorded) == 1
    assert recorded[0]["would_suppress"] is True


def test_a_passing_score_is_recorded_as_not_suppressed() -> None:
    recorded: list[dict] = []
    _fn(lambda t, b: _buy(), score=0.99, recorder=recorded.append)("AAA", _bars())

    assert recorded[0]["would_suppress"] is False


def test_the_record_carries_enough_to_join_against_a_fill() -> None:
    """A score with no identity cannot be evaluated later."""
    recorded: list[dict] = []
    _fn(lambda t, b: _buy("MSFT"), score=0.4, recorder=recorded.append)("MSFT", _bars())

    r = recorded[0]
    assert r["ticker"] == "MSFT"
    assert r["portfolio"] == "momentum"
    assert r["session"] == SESSIONS[-1]
    assert r["score"] == pytest.approx(0.4)
    assert r["threshold"] == pytest.approx(0.5)


# --- it must never break trading -------------------------------------------


def test_a_model_that_raises_does_not_stop_the_signal() -> None:
    """This runs inside the 04:15 job. A scoring failure must cost an
    observation, never a trade."""
    class _Broken(_Model):
        def predict(self, df):
            raise RuntimeError("model exploded")

    fn = make_ml_shadow_signals_fn(
        lambda t, b: _buy(), _Broken(0.5), threshold=0.5,
        strategy_name="momentum", recorder=lambda r: None,
    )

    assert fn("AAA", _bars()) == _buy()


def test_a_recorder_that_raises_does_not_stop_the_signal() -> None:
    def _boom(record):
        raise IOError("disk full")

    fn = _fn(lambda t, b: _buy(), score=0.4, recorder=_boom)

    assert fn("AAA", _bars()) == _buy()


# ---------------------------------------------------------------------------
# against the REAL model — a stub is what hid the defect below
# ---------------------------------------------------------------------------

REAL_MODEL = __import__("pathlib").Path(__file__).resolve().parents[2] / "models" / "signal_quality_model.txt"
REAL_META = REAL_MODEL.with_suffix("").with_suffix(".meta.json")


def _real_categoricals() -> list[str]:
    """The categorical feature names, from the sidecar the trainer writes —
    the same route production takes."""
    import json

    meta_path = REAL_MODEL.parent / "signal_quality_model.meta.json"
    return json.loads(meta_path.read_text()).get("categorical_features", [])


@pytest.mark.skipif(not REAL_MODEL.exists(), reason="models/ is gitignored; trained locally")
def test_the_real_model_scores_a_signal_without_raising() -> None:
    """Regression: the stub models above accept any DataFrame, so they passed
    while the live filter was broken for every trained model.

    LightGBM rejects a prediction frame whose categorical levels differ from
    training's ("train and valid dataset categorical_feature do not match").
    ``astype("category")`` on a ONE-ROW frame yields exactly one level, never
    the six the model was trained on, so `--ml-filter` raised for any real
    Booster.
    """
    import lightgbm as lgb

    from scripts.run_backtest import score_signal

    model = lgb.Booster(model_file=str(REAL_MODEL))
    score = score_signal(_buy(), _bars(), model, "momentum", _real_categoricals())

    assert 0.0 <= score <= 1.0


@pytest.mark.skipif(not REAL_MODEL.exists(), reason="models/ is gitignored; trained locally")
def test_every_live_sleeve_name_scores() -> None:
    """A sleeve whose name is not among the model's known categories must not
    raise — it scores as an unseen level."""
    import lightgbm as lgb

    from scripts.run_backtest import score_signal
    from shared.universe import ACTIVE_SLEEVES

    model = lgb.Booster(model_file=str(REAL_MODEL))

    for sleeve in ACTIVE_SLEEVES:
        assert 0.0 <= score_signal(_buy(), _bars(), model, sleeve, _real_categoricals()) <= 1.0


@pytest.mark.skipif(not REAL_MODEL.exists(), reason="models/ is gitignored; trained locally")
def test_an_unknown_sleeve_name_does_not_raise() -> None:
    import lightgbm as lgb

    from scripts.run_backtest import score_signal

    model = lgb.Booster(model_file=str(REAL_MODEL))

    assert 0.0 <= score_signal(
        _buy(), _bars(), model, "a_sleeve_added_later", _real_categoricals()
    ) <= 1.0
