# Native Factor Evaluation (Phase 3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an offline evaluator that measures whether each registered factor predicts forward returns — nested purged/embargoed walk-forward, multiple-testing control, overlap attribution — and emits one immutable, reproducible run card, without touching any trading path.

**Architecture:** A new dependency-light `research/evaluation/` subpackage of small, independently-tested units, driven by a thin CLI. It reuses the Phase 1-2 factor panel/engine to obtain factor scores and provenance, ranks each factor into a top-quantile long-only book measured as excess return over the equal-weight universe, and gates significance with Deflated Sharpe + Benjamini–Hochberg FDR. Shadow candidate records supply the baseline selection set for overlap cohorts.

**Tech Stack:** Python 3.12, dataclasses, pandas, NumPy, pytest. No new third-party runtime dependency (normal CDF/inverse-CDF implemented with `math`).

## Global Constraints

- Canonical design: `docs/superpowers/specs/2026-08-02-native-factor-evaluation-design.md`.
- `research/evaluation/` must not import `services.execution`, `services.risk_management`, `ib_insync`, `ibapi`, `redis`, `shared.redis_client`, `shared.schemas.messages`, `backtest.runner`, or `scripts.run_paper`.
- Offline only: no DB writes to operational tables, no Redis publish, no trading-path file changes.
- Factor edge is measured as top-quantile **long-only** excess return over the **equal-weight universe** (never long-short). Information Coefficient is a secondary diagnostic.
- Point-in-time: values at date `t` use only observations available by `t`; overlapping horizons handled by purge + embargo.
- Selection (the quantile cutoff) happens only inside inner folds; each outer-test span is scored exactly once.
- Determinism: identical frozen bars + seed → byte-identical run card. No wall-clock is read into the run card.
- Scope: individual factors only. No factor combinations, no Research Validation Score, no candidate generation (Phase 4+).
- No new third-party runtime dependency.

## File Map

**New package files**
- `research/evaluation/__init__.py` — public exports.
- `research/evaluation/forward_returns.py` — `forward_excess_returns`.
- `research/evaluation/folds.py` — `InnerFold`, `OuterFold`, `nested_walk_forward`.
- `research/evaluation/portfolio.py` — `PortfolioSeries`, `quantile_long_only`.
- `research/evaluation/metrics.py` — `sharpe`, `max_drawdown`, `annualized_turnover`, `sharpe_stats`, `probabilistic_sharpe`, `ic_summary`, `norm_cdf`.
- `research/evaluation/multiple_testing.py` — `inv_norm`, `expected_max_sharpe`, `benjamini_hochberg`, `MultipleTestingVerdict`, `control`.
- `research/evaluation/overlap.py` — `OverlapReport`, `baseline_selections_from_records`, `attribute`.
- `research/evaluation/runcard.py` — `build_run_card`, `write_run_card`.
- `research/evaluation/evaluator.py` — `evaluate_factors` orchestration.
- `scripts/run_factor_evaluation.py` — CLI.

**New tests**
- `tests/research/evaluation/test_forward_returns.py`
- `tests/research/evaluation/test_folds.py`
- `tests/research/evaluation/test_portfolio.py`
- `tests/research/evaluation/test_metrics.py`
- `tests/research/evaluation/test_multiple_testing.py`
- `tests/research/evaluation/test_overlap.py`
- `tests/research/evaluation/test_runcard.py`
- `tests/research/evaluation/test_evaluator.py`
- `tests/scripts/test_run_factor_evaluation.py`

**Modified files**
- `tests/research/test_architecture.py` — extend the boundary scan to `research/evaluation/`.

---

### Task 1: Forward Excess Returns

**Files:**
- Create: `research/evaluation/__init__.py`
- Create: `research/evaluation/forward_returns.py`
- Test: `tests/research/evaluation/__init__.py`
- Test: `tests/research/evaluation/test_forward_returns.py`

**Interfaces:**
- Consumes: `research.factors.contracts.FactorPanel` (has `.field("close")`).
- Produces: `forward_excess_returns(panel: FactorPanel, horizon: int) -> pd.DataFrame` (date × ticker, excess of equal-weight universe forward return; trailing rows without a `t+horizon` bar are `NaN`).

- [ ] **Step 1: Write the failing test**

```python
# tests/research/evaluation/test_forward_returns.py
from __future__ import annotations

from datetime import date

import pandas as pd

from research.evaluation.forward_returns import forward_excess_returns
from research.factors.panel import build_factor_panel


def _bars(closes: dict[str, list[float]], start=date(2026, 1, 5)):
    days = pd.bdate_range(start, periods=max(len(v) for v in closes.values()))
    return {
        ticker: [
            {"date": days[i].date(), "open": c, "high": c, "low": c, "close": c, "volume": 1_000}
            for i, c in enumerate(series)
        ]
        for ticker, series in closes.items()
    }


def test_excess_is_relative_to_equal_weight_universe():
    # A doubles (+100%), B flat (0%). Universe mean over 1-day fwd = +50% on day 0.
    panel = build_factor_panel(_bars({"A": [10, 20], "B": [10, 10]}))
    excess = forward_excess_returns(panel, horizon=1)
    assert excess.loc[panel.field("close").index[0], "A"] == 0.5
    assert excess.loc[panel.field("close").index[0], "B"] == -0.5
    # last row has no t+1 bar
    assert excess.iloc[-1].isna().all()


def test_future_mutation_cannot_change_earlier_forward_returns():
    base = forward_excess_returns(build_factor_panel(_bars({"A": [10, 11, 12, 13, 14], "B": [10, 10, 10, 10, 10]})), horizon=1)
    mutated = _bars({"A": [10, 11, 12, 13, 99999.0], "B": [10, 10, 10, 10, 10]})
    after = forward_excess_returns(build_factor_panel(mutated), horizon=1)
    # rows strictly before (len-1-horizon) index cannot see the mutated final close
    pd.testing.assert_frame_equal(base.iloc[:2], after.iloc[:2])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/research/evaluation/test_forward_returns.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'research.evaluation'`.

- [ ] **Step 3: Write minimal implementation**

```python
# research/evaluation/__init__.py
"""Offline factor evaluation subsystem (nested walk-forward, multiple-testing, overlap)."""
```

```python
# tests/research/evaluation/__init__.py
```

```python
# research/evaluation/forward_returns.py
from __future__ import annotations

import pandas as pd

from research.factors.contracts import FactorPanel


def forward_excess_returns(panel: FactorPanel, horizon: int) -> pd.DataFrame:
    """h-day forward return per ticker, minus the equal-weight universe forward return.

    Causal: the value anchored at date t uses close[t] and close[t+horizon] only.
    Rows without a t+horizon bar are NaN.
    """
    if horizon < 1:
        raise ValueError("horizon must be at least 1")
    close = panel.field("close")
    forward = close.shift(-horizon) / close - 1.0
    universe = forward.mean(axis=1, skipna=True)
    return forward.sub(universe, axis=0)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/research/evaluation/test_forward_returns.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add research/evaluation/__init__.py research/evaluation/forward_returns.py tests/research/evaluation/__init__.py tests/research/evaluation/test_forward_returns.py
git commit -m "feat: add forward excess return computation for factor evaluation"
```

---

### Task 2: Nested Purged/Embargoed Walk-Forward Folds

**Files:**
- Create: `research/evaluation/folds.py`
- Test: `tests/research/evaluation/test_folds.py`

**Interfaces:**
- Produces:
  - `InnerFold` (frozen dataclass) with `train: tuple[int, int]`, `validate: tuple[int, int]` (half-open `[start, end)` index ranges).
  - `OuterFold` (frozen dataclass) with `train: tuple[int, int]`, `test: tuple[int, int]`, `inner: tuple[InnerFold, ...]`.
  - `nested_walk_forward(n_dates: int, n_outer: int, n_inner: int, horizon: int, embargo: int) -> list[OuterFold]`.
- Semantics: `gap = horizon + embargo`; train indices always end at least `gap` before the test/validate start; inner folds live strictly inside the outer-train span.

- [ ] **Step 1: Write the failing test**

```python
# tests/research/evaluation/test_folds.py
from __future__ import annotations

import pytest

from research.evaluation.folds import nested_walk_forward


def test_no_test_index_appears_in_training_and_gap_is_respected():
    folds = nested_walk_forward(n_dates=100, n_outer=3, n_inner=2, horizon=5, embargo=5)
    assert len(folds) == 3
    gap = 10
    for outer in folds:
        tr_start, tr_end = outer.train
        te_start, te_end = outer.test
        train_idx = set(range(tr_start, tr_end))
        test_idx = set(range(te_start, te_end))
        assert train_idx.isdisjoint(test_idx)
        assert tr_end <= te_start - gap  # purge + embargo before test
        for inner in outer.inner:
            it_start, it_end = inner.train
            iv_start, iv_end = inner.validate
            assert it_start >= tr_start and iv_end <= tr_end  # inner inside outer-train
            assert it_end <= iv_start - gap


def test_insufficient_history_raises():
    with pytest.raises(ValueError, match="not enough dates"):
        nested_walk_forward(n_dates=5, n_outer=3, n_inner=2, horizon=5, embargo=5)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/research/evaluation/test_folds.py -v`
Expected: FAIL — `ModuleNotFoundError` / `nested_walk_forward` undefined.

- [ ] **Step 3: Write minimal implementation**

```python
# research/evaluation/folds.py
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class InnerFold:
    train: tuple[int, int]
    validate: tuple[int, int]


@dataclass(frozen=True)
class OuterFold:
    train: tuple[int, int]
    test: tuple[int, int]
    inner: tuple[InnerFold, ...]


def _segment_bounds(n: int, n_segments: int) -> list[tuple[int, int]]:
    size = n // n_segments
    bounds: list[tuple[int, int]] = []
    start = 0
    for i in range(n_segments):
        end = n if i == n_segments - 1 else start + size
        bounds.append((start, end))
        start = end
    return bounds


def _inner_folds(start: int, end: int, n_inner: int, gap: int) -> list[InnerFold]:
    length = end - start
    if length <= 0:
        return []
    segs = _segment_bounds(length, n_inner + 1)
    folds: list[InnerFold] = []
    for j in range(n_inner):
        v_start = start + segs[j + 1][0]
        v_end = start + segs[j + 1][1]
        it_end = max(start, v_start - gap)
        folds.append(InnerFold(train=(start, it_end), validate=(v_start, v_end)))
    return folds


def nested_walk_forward(
    n_dates: int, n_outer: int, n_inner: int, horizon: int, embargo: int
) -> list[OuterFold]:
    if n_dates < (n_outer + 1) * (n_inner + 1):
        raise ValueError("not enough dates for the requested outer/inner folds")
    gap = horizon + embargo
    segments = _segment_bounds(n_dates, n_outer + 1)
    outer_folds: list[OuterFold] = []
    for k in range(n_outer):
        test_start, test_end = segments[k + 1]
        train_end = max(0, test_start - gap)
        inner = _inner_folds(0, train_end, n_inner, gap)
        outer_folds.append(
            OuterFold(train=(0, train_end), test=(test_start, test_end), inner=tuple(inner))
        )
    return outer_folds
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/research/evaluation/test_folds.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add research/evaluation/folds.py tests/research/evaluation/test_folds.py
git commit -m "feat: add nested purged embargoed walk-forward folds"
```

---

### Task 3: Top-Quantile Long-Only Portfolio and IC

**Files:**
- Create: `research/evaluation/portfolio.py`
- Test: `tests/research/evaluation/test_portfolio.py`

**Interfaces:**
- Consumes: aligned `scores` and `forward` frames (date × ticker), from Tasks 1 and the engine.
- Produces:
  - `PortfolioSeries` (frozen dataclass) with `returns: pd.Series`, `turnover: pd.Series`, `ic: pd.Series` (all indexed by rebalance date).
  - `quantile_long_only(scores: pd.DataFrame, forward: pd.DataFrame, quantile: float, rebalance: int, min_names: int = 5) -> PortfolioSeries`.
- Semantics: each `rebalance`-th date, hold the top `quantile` fraction by score, equal-weighted long-only; series value is the held book's mean forward excess return; `turnover` is set-symmetric-difference over held count; `ic` is Spearman rank correlation of score vs forward across the valid universe.

- [ ] **Step 1: Write the failing test**

```python
# tests/research/evaluation/test_portfolio.py
from __future__ import annotations

import pandas as pd

from research.evaluation.portfolio import quantile_long_only


def _frame(rows: dict[str, list[float]]):
    idx = pd.to_datetime(["2026-01-05", "2026-01-06", "2026-01-07"])
    return pd.DataFrame(rows, index=idx)


def test_top_quantile_book_return_and_monotone_ic():
    # Higher score -> higher forward return, so IC should be +1 each date.
    scores = _frame({"A": [3.0, 3.0, 3.0], "B": [2.0, 2.0, 2.0], "C": [1.0, 1.0, 1.0], "D": [0.0, 0.0, 0.0]})
    forward = _frame({"A": [0.30, 0.30, 0.30], "B": [0.20, 0.20, 0.20], "C": [0.10, 0.10, 0.10], "D": [0.00, 0.00, 0.00]})
    series = quantile_long_only(scores, forward, quantile=0.5, rebalance=1, min_names=4)
    # top 50% of 4 names = A,B -> mean forward 0.25
    assert round(series.returns.iloc[0], 6) == 0.25
    assert round(series.ic.iloc[0], 6) == 1.0
    assert series.turnover.iloc[0] == 1.0  # first rebalance: everything is new


def test_skips_dates_below_min_coverage():
    scores = _frame({"A": [1.0, 1.0, 1.0], "B": [2.0, 2.0, 2.0]})
    forward = _frame({"A": [0.1, 0.1, 0.1], "B": [0.2, 0.2, 0.2]})
    series = quantile_long_only(scores, forward, quantile=0.5, rebalance=1, min_names=5)
    assert len(series.returns) == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/research/evaluation/test_portfolio.py -v`
Expected: FAIL — `quantile_long_only` undefined.

- [ ] **Step 3: Write minimal implementation**

```python
# research/evaluation/portfolio.py
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class PortfolioSeries:
    returns: pd.Series
    turnover: pd.Series
    ic: pd.Series


def quantile_long_only(
    scores: pd.DataFrame,
    forward: pd.DataFrame,
    quantile: float,
    rebalance: int,
    min_names: int = 5,
) -> PortfolioSeries:
    if not 0.0 < quantile <= 1.0:
        raise ValueError("quantile must be in (0, 1]")
    if rebalance < 1:
        raise ValueError("rebalance must be at least 1")

    dates = scores.index
    rets: list[float] = []
    turns: list[float] = []
    ics: list[float] = []
    index: list[pd.Timestamp] = []
    prev_held: set[str] = set()

    for i in range(0, len(dates), rebalance):
        day = dates[i]
        score_row = scores.loc[day].dropna()
        fwd_row = forward.loc[day].dropna()
        valid = score_row.index.intersection(fwd_row.index)
        if len(valid) < min_names:
            continue
        score_row = score_row[valid]
        fwd_row = fwd_row[valid]
        k = max(1, int(len(score_row) * quantile))
        held = set(score_row.sort_values(ascending=False).index[:k])
        rets.append(float(fwd_row[list(held)].mean()))
        denom = max(1, len(held))
        turns.append(len(held.symmetric_difference(prev_held)) / denom)
        ics.append(float(score_row.rank().corr(fwd_row.rank())))
        index.append(day)
        prev_held = held

    return PortfolioSeries(
        returns=pd.Series(rets, index=index, dtype=float),
        turnover=pd.Series(turns, index=index, dtype=float),
        ic=pd.Series(ics, index=index, dtype=float),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/research/evaluation/test_portfolio.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add research/evaluation/portfolio.py tests/research/evaluation/test_portfolio.py
git commit -m "feat: add top-quantile long-only factor portfolio and IC"
```

---

### Task 4: Return-Series Metrics

**Files:**
- Create: `research/evaluation/metrics.py`
- Test: `tests/research/evaluation/test_metrics.py`

**Interfaces:**
- Produces:
  - `norm_cdf(x: float) -> float`.
  - `sharpe(returns: pd.Series, periods_per_year: float) -> float`.
  - `max_drawdown(returns: pd.Series) -> float`.
  - `annualized_turnover(turnover: pd.Series, periods_per_year: float) -> float`.
  - `sharpe_stats(returns: pd.Series) -> dict` with keys `n`, `sr` (per-period), `skew`, `kurt`.
  - `probabilistic_sharpe(sr: float, n: int, skew: float, kurt: float, sr_star: float) -> float`.
  - `ic_summary(ic: pd.Series) -> dict` with keys `mean`, `t_stat`, `p_value`, `hit_rate`.

- [ ] **Step 1: Write the failing test**

```python
# tests/research/evaluation/test_metrics.py
from __future__ import annotations

import math

import pandas as pd

from research.evaluation.metrics import (
    annualized_turnover,
    ic_summary,
    max_drawdown,
    norm_cdf,
    probabilistic_sharpe,
    sharpe,
    sharpe_stats,
)


def test_norm_cdf_known_points():
    assert round(norm_cdf(0.0), 6) == 0.5
    assert round(norm_cdf(1.96), 3) == 0.975


def test_sharpe_and_drawdown():
    r = pd.Series([0.01, 0.01, 0.01, 0.01])
    assert sharpe(r, periods_per_year=252) > 0
    dd = max_drawdown(pd.Series([0.1, -0.5, 0.0]))
    assert round(dd, 3) == -0.5


def test_probabilistic_sharpe_rises_with_stronger_track_record():
    strong = probabilistic_sharpe(sr=0.3, n=252, skew=0.0, kurt=3.0, sr_star=0.0)
    weak = probabilistic_sharpe(sr=0.02, n=252, skew=0.0, kurt=3.0, sr_star=0.0)
    assert strong > weak
    assert 0.0 <= strong <= 1.0


def test_ic_summary_perfectly_positive_series():
    summary = ic_summary(pd.Series([0.2, 0.3, 0.25, 0.28]))
    assert summary["mean"] > 0
    assert summary["hit_rate"] == 1.0
    assert 0.0 <= summary["p_value"] <= 1.0


def test_annualized_turnover():
    assert annualized_turnover(pd.Series([0.5, 0.5]), periods_per_year=12) == 6.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/research/evaluation/test_metrics.py -v`
Expected: FAIL — module/functions undefined.

- [ ] **Step 3: Write minimal implementation**

```python
# research/evaluation/metrics.py
from __future__ import annotations

import math

import numpy as np
import pandas as pd


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def sharpe(returns: pd.Series, periods_per_year: float) -> float:
    r = returns.dropna()
    if len(r) < 2:
        return 0.0
    sd = r.std(ddof=1)
    if sd == 0:
        return 0.0
    return float(r.mean() / sd * math.sqrt(periods_per_year))


def max_drawdown(returns: pd.Series) -> float:
    r = returns.dropna()
    if len(r) == 0:
        return 0.0
    equity = (1.0 + r).cumprod()
    drawdown = equity / equity.cummax() - 1.0
    return float(drawdown.min())


def annualized_turnover(turnover: pd.Series, periods_per_year: float) -> float:
    t = turnover.dropna()
    if len(t) == 0:
        return 0.0
    return float(t.mean() * periods_per_year)


def sharpe_stats(returns: pd.Series) -> dict:
    r = np.asarray(returns.dropna(), dtype=float)
    n = int(r.size)
    if n < 4:
        return {"n": n, "sr": 0.0, "skew": 0.0, "kurt": 3.0}
    mu = float(r.mean())
    sd = float(r.std(ddof=1))
    if sd == 0:
        return {"n": n, "sr": 0.0, "skew": 0.0, "kurt": 3.0}
    s0 = float(r.std(ddof=0))
    skew = float(((r - mu) ** 3).mean() / s0**3)
    kurt = float(((r - mu) ** 4).mean() / s0**4)
    return {"n": n, "sr": mu / sd, "skew": skew, "kurt": kurt}


def probabilistic_sharpe(sr: float, n: int, skew: float, kurt: float, sr_star: float) -> float:
    """Bailey & Lopez de Prado PSR: probability the true Sharpe exceeds sr_star."""
    if n < 4:
        return 0.0
    variance = 1.0 - skew * sr + ((kurt - 1.0) / 4.0) * sr**2
    denom = math.sqrt(max(1e-12, variance))
    z = (sr - sr_star) * math.sqrt(n - 1) / denom
    return norm_cdf(z)


def ic_summary(ic: pd.Series) -> dict:
    values = ic.dropna()
    n = len(values)
    if n < 2:
        return {"mean": 0.0, "t_stat": 0.0, "p_value": 1.0, "hit_rate": 0.0}
    mean = float(values.mean())
    sd = float(values.std(ddof=1))
    t_stat = 0.0 if sd == 0 else mean / (sd / math.sqrt(n))
    p_value = 2.0 * (1.0 - norm_cdf(abs(t_stat)))
    hit_rate = float((values > 0).mean())
    return {"mean": mean, "t_stat": float(t_stat), "p_value": float(p_value), "hit_rate": hit_rate}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/research/evaluation/test_metrics.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add research/evaluation/metrics.py tests/research/evaluation/test_metrics.py
git commit -m "feat: add factor evaluation return-series metrics"
```

---

### Task 5: Multiple-Testing Control (Deflated Sharpe + BH-FDR)

**Files:**
- Create: `research/evaluation/multiple_testing.py`
- Test: `tests/research/evaluation/test_multiple_testing.py`

**Interfaces:**
- Consumes: per-factor stat dicts `{factor_id: {"sr", "n", "skew", "kurt", "ic_p"}}` (from Task 4 outputs).
- Produces:
  - `inv_norm(p: float) -> float` (inverse standard normal CDF, Acklam approximation).
  - `expected_max_sharpe(trial_srs: list[float]) -> float`.
  - `benjamini_hochberg(p_values: dict[str, float], q: float) -> dict[str, bool]`.
  - `MultipleTestingVerdict` (frozen dataclass): `deflated_sharpe: float`, `passes_dsr: bool`, `passes_fdr: bool`, `survives: bool`.
  - `control(per_factor: dict[str, dict], q: float = 0.10, dsr_threshold: float = 0.95) -> dict[str, MultipleTestingVerdict]`.
- Semantics: `sr_star` = `expected_max_sharpe` of the trial Sharpes; `deflated_sharpe` = `probabilistic_sharpe(sr, n, skew, kurt, sr_star)`; `survives` = `passes_dsr and passes_fdr`.

- [ ] **Step 1: Write the failing test**

```python
# tests/research/evaluation/test_multiple_testing.py
from __future__ import annotations

from research.evaluation.multiple_testing import (
    benjamini_hochberg,
    control,
    expected_max_sharpe,
    inv_norm,
)


def test_inv_norm_is_inverse_of_cdf_midpoints():
    assert round(inv_norm(0.5), 6) == 0.0
    assert round(inv_norm(0.975), 2) == 1.96


def test_expected_max_sharpe_grows_with_trial_count_and_spread():
    few = expected_max_sharpe([0.0, 0.1, 0.2])
    many = expected_max_sharpe([0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7])
    assert many > few >= 0.0


def test_benjamini_hochberg_selects_low_p_values():
    passed = benjamini_hochberg({"a": 0.001, "b": 0.2, "c": 0.9, "d": 0.04}, q=0.10)
    assert passed["a"] is True
    assert passed["c"] is False


def test_control_requires_both_gates():
    per_factor = {
        "strong": {"sr": 0.5, "n": 500, "skew": 0.0, "kurt": 3.0, "ic_p": 0.001},
        "noise": {"sr": 0.001, "n": 500, "skew": 0.0, "kurt": 3.0, "ic_p": 0.95},
    }
    verdicts = control(per_factor)
    assert verdicts["strong"].survives is True
    assert verdicts["noise"].survives is False
    assert verdicts["noise"].passes_fdr is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/research/evaluation/test_multiple_testing.py -v`
Expected: FAIL — module/functions undefined.

- [ ] **Step 3: Write minimal implementation**

```python
# research/evaluation/multiple_testing.py
from __future__ import annotations

import math
from dataclasses import dataclass

from research.evaluation.metrics import probabilistic_sharpe

_EULER = 0.5772156649015329


def inv_norm(p: float) -> float:
    """Inverse standard normal CDF via Peter Acklam's rational approximation."""
    if not 0.0 < p < 1.0:
        raise ValueError("p must be in (0, 1)")
    a = [-3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02,
         1.383577518672690e02, -3.066479806614716e01, 2.506628277459239e00]
    b = [-5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02,
         6.680131188771972e01, -1.328068155288572e01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00,
         -2.549732539343734e00, 4.374664141464968e00, 2.938163982698783e00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00,
         3.754408661907416e00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / (
        (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1))


def expected_max_sharpe(trial_srs: list[float]) -> float:
    m = len(trial_srs)
    if m < 2:
        return 0.0
    mean = sum(trial_srs) / m
    var = sum((s - mean) ** 2 for s in trial_srs) / (m - 1)
    if var <= 0:
        return 0.0
    std = math.sqrt(var)
    a = inv_norm(1 - 1.0 / m)
    b = inv_norm(1 - 1.0 / (m * math.e))
    return std * ((1 - _EULER) * a + _EULER * b)


def benjamini_hochberg(p_values: dict[str, float], q: float) -> dict[str, bool]:
    items = sorted(p_values.items(), key=lambda kv: kv[1])
    m = len(items)
    max_rank = 0
    for rank, (_, p) in enumerate(items, start=1):
        if p <= (rank / m) * q:
            max_rank = rank
    selected = {fid for rank, (fid, _) in enumerate(items, start=1) if rank <= max_rank}
    return {fid: fid in selected for fid in p_values}


@dataclass(frozen=True)
class MultipleTestingVerdict:
    deflated_sharpe: float
    passes_dsr: bool
    passes_fdr: bool
    survives: bool


def control(
    per_factor: dict[str, dict], q: float = 0.10, dsr_threshold: float = 0.95
) -> dict[str, MultipleTestingVerdict]:
    sr_star = expected_max_sharpe([v["sr"] for v in per_factor.values()])
    fdr = benjamini_hochberg({fid: v["ic_p"] for fid, v in per_factor.items()}, q)
    verdicts: dict[str, MultipleTestingVerdict] = {}
    for fid, v in per_factor.items():
        dsr = probabilistic_sharpe(v["sr"], v["n"], v["skew"], v["kurt"], sr_star)
        passes_dsr = dsr >= dsr_threshold
        passes_fdr = fdr[fid]
        verdicts[fid] = MultipleTestingVerdict(
            deflated_sharpe=dsr,
            passes_dsr=passes_dsr,
            passes_fdr=passes_fdr,
            survives=passes_dsr and passes_fdr,
        )
    return verdicts
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/research/evaluation/test_multiple_testing.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add research/evaluation/multiple_testing.py tests/research/evaluation/test_multiple_testing.py
git commit -m "feat: add deflated-Sharpe and BH-FDR multiple-testing control"
```

---

### Task 6: Overlap Attribution

**Files:**
- Create: `research/evaluation/overlap.py`
- Test: `tests/research/evaluation/test_overlap.py`

**Interfaces:**
- Consumes: `factor_selections: dict[date, set[str]]`, `baseline_selections: dict[date, set[str]]`, and `forward: pd.DataFrame` (date × ticker, from Task 1). Shadow record dicts have keys `as_of` (date or ISO str), `ticker`, `action`.
- Produces:
  - `OverlapReport` (frozen dataclass): `counts: dict[str, int]`, `cohort_returns: dict[str, float]` over cohorts `research_only`, `overlap`, `baseline_only`.
  - `baseline_selections_from_records(records: list[dict]) -> dict[date, set[str]]` (buys only, grouped by `as_of`).
  - `attribute(factor_selections, baseline_selections, forward) -> OverlapReport`.

- [ ] **Step 1: Write the failing test**

```python
# tests/research/evaluation/test_overlap.py
from __future__ import annotations

from datetime import date

import pandas as pd

from research.evaluation.overlap import (
    attribute,
    baseline_selections_from_records,
)


def test_baseline_selections_group_buys_by_date():
    records = [
        {"as_of": "2026-01-05", "ticker": "AAPL", "action": "buy"},
        {"as_of": date(2026, 1, 5), "ticker": "MSFT", "action": "buy"},
        {"as_of": "2026-01-05", "ticker": "NVDA", "action": "sell"},
    ]
    selections = baseline_selections_from_records(records)
    assert selections[date(2026, 1, 5)] == {"AAPL", "MSFT"}


def test_cohorts_partition_and_average_returns():
    day = pd.Timestamp("2026-01-05")
    forward = pd.DataFrame({"A": [0.10], "B": [0.20], "C": [0.30]}, index=[day])
    factor_selections = {day.date(): {"A", "B"}}
    baseline_selections = {day.date(): {"B", "C"}}
    report = attribute(factor_selections, baseline_selections, forward)
    assert report.counts == {"research_only": 1, "overlap": 1, "baseline_only": 1}
    assert round(report.cohort_returns["research_only"], 6) == 0.10  # A
    assert round(report.cohort_returns["overlap"], 6) == 0.20        # B
    assert round(report.cohort_returns["baseline_only"], 6) == 0.30  # C
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/research/evaluation/test_overlap.py -v`
Expected: FAIL — module/functions undefined.

- [ ] **Step 3: Write minimal implementation**

```python
# research/evaluation/overlap.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

_COHORTS = ("research_only", "overlap", "baseline_only")


@dataclass(frozen=True)
class OverlapReport:
    counts: dict[str, int]
    cohort_returns: dict[str, float]


def _as_date(value: object) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def baseline_selections_from_records(records: list[dict]) -> dict[date, set[str]]:
    selections: dict[date, set[str]] = {}
    for record in records:
        if str(record.get("action")) != "buy":
            continue
        day = _as_date(record["as_of"])
        selections.setdefault(day, set()).add(str(record["ticker"]))
    return selections


def attribute(
    factor_selections: dict[date, set[str]],
    baseline_selections: dict[date, set[str]],
    forward: pd.DataFrame,
) -> OverlapReport:
    buckets: dict[str, list[float]] = {cohort: [] for cohort in _COHORTS}
    for day, factor_names in factor_selections.items():
        baseline_names = baseline_selections.get(day, set())
        timestamp = pd.Timestamp(day)
        if timestamp not in forward.index:
            continue
        fwd_row = forward.loc[timestamp]
        for ticker in factor_names | baseline_names:
            if ticker not in fwd_row or pd.isna(fwd_row[ticker]):
                continue
            in_factor = ticker in factor_names
            in_baseline = ticker in baseline_names
            if in_factor and in_baseline:
                cohort = "overlap"
            elif in_factor:
                cohort = "research_only"
            else:
                cohort = "baseline_only"
            buckets[cohort].append(float(fwd_row[ticker]))
    counts = {cohort: len(values) for cohort, values in buckets.items()}
    cohort_returns = {
        cohort: (float(np.mean(values)) if values else 0.0) for cohort, values in buckets.items()
    }
    return OverlapReport(counts=counts, cohort_returns=cohort_returns)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/research/evaluation/test_overlap.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add research/evaluation/overlap.py tests/research/evaluation/test_overlap.py
git commit -m "feat: add overlap cohort attribution for factor evaluation"
```

---

### Task 7: Evaluator Orchestration

**Files:**
- Create: `research/evaluation/evaluator.py`
- Test: `tests/research/evaluation/test_evaluator.py`

**Interfaces:**
- Consumes: Tasks 1–6, plus `research.factors.catalog.build_default_registry`/`DEFAULT_FACTOR_IDS`, `research.factors.engine.FactorEngine`, `research.factors.panel.build_factor_panel`.
- Produces:
  - `EvaluationConfig` (frozen dataclass): `horizon=21`, `n_outer=4`, `n_inner=3`, `embargo=21`, `quantiles=(0.2, 0.3)`, `fdr_q=0.10`, `min_names=5`, `seed=7`.
  - `evaluate_factors(bars_by_ticker: dict, factor_ids: tuple[str, ...] | None = None, baseline_records: list[dict] | None = None, config: EvaluationConfig | None = None) -> dict` — the full per-factor evidence structure (predictive metrics, multiple-testing verdicts, overlap), plus the engine `snapshot_identity` and `provenance` mapping.
- Semantics: factor score frames are obtained from `registry.get(factor_id).compute(panel)` (cross-sectional ranking makes raw vs normalized scores equivalent for top-quantile membership); the engine is called once to attach provenance/identity. The quantile cutoff is chosen per factor by best mean inner-validation IC across `config.quantiles`; the chosen cutoff scores each outer-test span once; outer-test excess-return and IC series are concatenated across folds before metrics.

- [ ] **Step 1: Write the failing test**

```python
# tests/research/evaluation/test_evaluator.py
from __future__ import annotations

from datetime import date, timedelta

from research.evaluation.evaluator import EvaluationConfig, evaluate_factors


def _trending_bars(n_days=400):
    start = date(2024, 1, 1)
    tickers = {"A": 1.0, "B": 0.6, "C": 0.3, "D": -0.2, "E": -0.5, "F": -0.9}
    bars = {}
    for ticker, drift in tickers.items():
        price = 100.0
        rows = []
        for i in range(n_days):
            price = max(1.0, price + drift)
            rows.append({"date": start + timedelta(days=i), "open": price, "high": price + 1,
                         "low": price - 1, "close": price, "volume": 1_000 + i})
        bars[ticker] = rows
    return bars


def test_evaluate_returns_per_factor_evidence_and_provenance():
    config = EvaluationConfig(horizon=5, n_outer=3, n_inner=2, embargo=5, min_names=3)
    result = evaluate_factors(_trending_bars(), config=config)
    assert set(result["factors"]) == {"price_momentum_126d", "high_52w", "low_volatility_63d", "liquidity_20d"}
    momentum = result["factors"]["price_momentum_126d"]
    assert {"sharpe", "deflated_sharpe", "survives_multiple_testing", "ic_mean", "chosen_quantile"} <= set(momentum)
    assert isinstance(result["snapshot_identity"], str) and result["snapshot_identity"]
    assert "data_cutoff" in result["provenance"]


def test_determinism_same_bars_same_result():
    config = EvaluationConfig(horizon=5, n_outer=3, n_inner=2, embargo=5, min_names=3)
    bars = _trending_bars()
    assert evaluate_factors(bars, config=config) == evaluate_factors(bars, config=config)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/research/evaluation/test_evaluator.py -v`
Expected: FAIL — module/functions undefined.

- [ ] **Step 3: Write minimal implementation**

```python
# research/evaluation/evaluator.py
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from research.evaluation.folds import nested_walk_forward
from research.evaluation.forward_returns import forward_excess_returns
from research.evaluation.metrics import (
    annualized_turnover,
    ic_summary,
    max_drawdown,
    sharpe,
    sharpe_stats,
)
from research.evaluation.multiple_testing import control
from research.evaluation.overlap import attribute, baseline_selections_from_records
from research.evaluation.portfolio import quantile_long_only
from research.factors.catalog import DEFAULT_FACTOR_IDS, build_default_registry
from research.factors.engine import FactorEngine
from research.factors.panel import build_factor_panel


@dataclass(frozen=True)
class EvaluationConfig:
    horizon: int = 21
    n_outer: int = 4
    n_inner: int = 3
    embargo: int = 21
    quantiles: tuple[float, ...] = (0.2, 0.3)
    fdr_q: float = 0.10
    min_names: int = 5
    seed: int = 7


def _slice(frame: pd.DataFrame, bounds: tuple[int, int]) -> pd.DataFrame:
    start, end = bounds
    return frame.iloc[start:end]


def _select_quantile(scores, forward, inner, config) -> float:
    best_q, best_ic = config.quantiles[0], float("-inf")
    for quantile in config.quantiles:
        ics: list[float] = []
        for fold in inner:
            v_scores = _slice(scores, fold.validate)
            v_forward = _slice(forward, fold.validate)
            series = quantile_long_only(v_scores, v_forward, quantile, config.horizon, config.min_names)
            if len(series.ic):
                ics.append(float(series.ic.mean()))
        mean_ic = sum(ics) / len(ics) if ics else float("-inf")
        if mean_ic > best_ic:
            best_ic, best_q = mean_ic, quantile
    return best_q


def evaluate_factors(
    bars_by_ticker: dict,
    factor_ids: tuple[str, ...] | None = None,
    baseline_records: list[dict] | None = None,
    config: EvaluationConfig | None = None,
) -> dict:
    config = config or EvaluationConfig()
    factor_ids = factor_ids or DEFAULT_FACTOR_IDS
    registry = build_default_registry()
    panel = build_factor_panel(bars_by_ticker)
    engine_snapshot = FactorEngine(registry).compute(panel, factor_ids)
    forward = forward_excess_returns(panel, config.horizon)
    n_dates = len(panel.field("close").index)
    folds = nested_walk_forward(n_dates, config.n_outer, config.n_inner, config.horizon, config.embargo)
    periods_per_year = 252.0 / config.horizon

    baseline = baseline_selections_from_records(baseline_records or [])
    per_factor_stats: dict[str, dict] = {}
    per_factor_series: dict[str, dict] = {}

    for factor_id in factor_ids:
        scores = registry.get(factor_id).compute(panel).astype(float)
        oos_returns: list[pd.Series] = []
        oos_ic: list[pd.Series] = []
        oos_turnover: list[pd.Series] = []
        factor_selection: dict = {}
        for outer in folds:
            quantile = _select_quantile(_slice(scores, outer.train), _slice(forward, outer.train), outer.inner, config)
            test_scores = _slice(scores, outer.test)
            test_forward = _slice(forward, outer.test)
            series = quantile_long_only(test_scores, test_forward, quantile, config.horizon, config.min_names)
            oos_returns.append(series.returns)
            oos_ic.append(series.ic)
            oos_turnover.append(series.turnover)
            for i in range(0, len(test_scores.index), config.horizon):
                day = test_scores.index[i]
                row = test_scores.loc[day].dropna()
                if len(row) < config.min_names:
                    continue
                k = max(1, int(len(row) * quantile))
                factor_selection[day.date()] = set(row.sort_values(ascending=False).index[:k])
        returns = pd.concat(oos_returns) if oos_returns else pd.Series(dtype=float)
        ic = pd.concat(oos_ic) if oos_ic else pd.Series(dtype=float)
        turnover = pd.concat(oos_turnover) if oos_turnover else pd.Series(dtype=float)
        stats = sharpe_stats(returns)
        ic_stat = ic_summary(ic)
        per_factor_stats[factor_id] = {
            "sr": stats["sr"], "n": stats["n"], "skew": stats["skew"],
            "kurt": stats["kurt"], "ic_p": ic_stat["p_value"],
        }
        per_factor_series[factor_id] = {
            "returns": returns, "ic": ic, "turnover": turnover,
            "ic_stat": ic_stat, "selection": factor_selection, "chosen_quantile": quantile,
        }

    verdicts = control(per_factor_stats, q=config.fdr_q)

    factors: dict[str, dict] = {}
    for factor_id in factor_ids:
        s = per_factor_series[factor_id]
        verdict = verdicts[factor_id]
        overlap = attribute(s["selection"], baseline, forward)
        factors[factor_id] = {
            "chosen_quantile": s["chosen_quantile"],
            "sharpe": sharpe(s["returns"], periods_per_year),
            "deflated_sharpe": verdict.deflated_sharpe,
            "passes_dsr": verdict.passes_dsr,
            "passes_fdr": verdict.passes_fdr,
            "survives_multiple_testing": verdict.survives,
            "max_drawdown": max_drawdown(s["returns"]),
            "annual_turnover": annualized_turnover(s["turnover"], periods_per_year),
            "ic_mean": s["ic_stat"]["mean"],
            "ic_t_stat": s["ic_stat"]["t_stat"],
            "n_observations": int(len(s["returns"])),
            "overlap_counts": overlap.counts,
            "overlap_returns": overlap.cohort_returns,
        }

    return {
        "factors": factors,
        "snapshot_identity": engine_snapshot.snapshot_identity,
        "provenance": dict(engine_snapshot.provenance.to_mapping()),
        "config": {
            "horizon": config.horizon, "n_outer": config.n_outer, "n_inner": config.n_inner,
            "embargo": config.embargo, "quantiles": list(config.quantiles),
            "fdr_q": config.fdr_q, "min_names": config.min_names, "seed": config.seed,
        },
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/research/evaluation/test_evaluator.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add research/evaluation/evaluator.py tests/research/evaluation/test_evaluator.py
git commit -m "feat: orchestrate nested walk-forward factor evaluation"
```

---

### Task 8: Run Card and CLI

**Files:**
- Create: `research/evaluation/runcard.py`
- Modify: `research/evaluation/__init__.py`
- Create: `scripts/run_factor_evaluation.py`
- Test: `tests/research/evaluation/test_runcard.py`
- Test: `tests/scripts/test_run_factor_evaluation.py`

**Interfaces:**
- Consumes: `evaluate_factors` result, git revision, input artifact checksum.
- Produces:
  - `build_run_card(evaluation: dict, git_revision: str, input_checksum: str) -> dict`.
  - `write_run_card(card: dict, output_dir: str) -> Path` — writes `factor_evaluation_<cutoff>.json` with sorted keys; never writes under `output/`.
  - CLI `scripts/run_factor_evaluation.py` with `--bars-from-json` (required), `--shadow-from-json`, `--horizon`, `--outer-folds`, `--inner-folds`, `--quantiles`, `--fdr-q`, `--min-names`, `--seed`, `--output-dir` (required).

- [ ] **Step 1: Write the failing tests**

```python
# tests/research/evaluation/test_runcard.py
from __future__ import annotations

import json
from pathlib import Path

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
```

```python
# tests/scripts/test_run_factor_evaluation.py
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from scripts.run_factor_evaluation import main


def _write_bars(path: Path, n_days=400):
    start = date(2024, 1, 1)
    tickers = {"A": 1.0, "B": 0.6, "C": 0.3, "D": -0.2, "E": -0.5, "F": -0.9}
    bars = {}
    for ticker, drift in tickers.items():
        price = 100.0
        rows = []
        for i in range(n_days):
            price = max(1.0, price + drift)
            rows.append({"date": (start + timedelta(days=i)).isoformat(), "open": price,
                         "high": price + 1, "low": price - 1, "close": price, "volume": 1_000 + i})
        bars[ticker] = rows
    path.write_text(json.dumps({"bars": bars}))


def test_cli_writes_run_card_with_all_factors(tmp_path):
    bars_path = tmp_path / "bars.json"
    _write_bars(bars_path)
    out_dir = tmp_path / "out"
    code = main([
        "--bars-from-json", str(bars_path), "--output-dir", str(out_dir),
        "--horizon", "5", "--outer-folds", "3", "--inner-folds", "2", "--min-names", "3",
    ])
    assert code == 0
    cards = list(out_dir.glob("factor_evaluation_*.json"))
    assert len(cards) == 1
    card = json.loads(cards[0].read_text())
    assert set(card["evaluation"]["factors"]) == {
        "price_momentum_126d", "high_52w", "low_volatility_63d", "liquidity_20d"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/research/evaluation/test_runcard.py tests/scripts/test_run_factor_evaluation.py -v`
Expected: FAIL — `runcard` and `scripts.run_factor_evaluation` undefined.

- [ ] **Step 3: Implement the run card**

```python
# research/evaluation/runcard.py
from __future__ import annotations

import json
from pathlib import Path


def build_run_card(evaluation: dict, git_revision: str, input_checksum: str) -> dict:
    return {
        "git_revision": git_revision,
        "input_artifact_checksum": input_checksum,
        "snapshot_identity": evaluation["snapshot_identity"],
        "provenance": evaluation["provenance"],
        "config": evaluation["config"],
        "evaluation": evaluation,
    }


def write_run_card(card: dict, output_dir: str) -> Path:
    directory = Path(output_dir)
    if "output" in directory.parts:
        raise ValueError("run cards must not be written under output/")
    directory.mkdir(parents=True, exist_ok=True)
    cutoff = card["provenance"]["data_cutoff"]
    path = directory / f"factor_evaluation_{cutoff}.json"
    path.write_text(json.dumps(card, sort_keys=True, indent=2))
    return path
```

Add to `research/evaluation/__init__.py`:

```python
from research.evaluation.evaluator import EvaluationConfig, evaluate_factors
from research.evaluation.runcard import build_run_card, write_run_card

__all__ = ["EvaluationConfig", "evaluate_factors", "build_run_card", "write_run_card"]
```

- [ ] **Step 4: Implement the CLI**

```python
# scripts/run_factor_evaluation.py
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import date
from pathlib import Path

from research.evaluation.evaluator import EvaluationConfig, evaluate_factors
from research.evaluation.runcard import build_run_card, write_run_card


def _load_bars(path: str) -> dict:
    payload = json.loads(Path(path).read_text())
    bars = payload.get("bars")
    if not bars:
        raise ValueError(f"no bars found in {path}")
    return {
        ticker: [
            {**bar, "date": date.fromisoformat(bar["date"]) if isinstance(bar["date"], str) else bar["date"]}
            for bar in rows
        ]
        for ticker, rows in bars.items()
    }


def _git_revision() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    except Exception:
        return "unknown"


def _checksum(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline factor evaluation run card generator")
    parser.add_argument("--bars-from-json", required=True)
    parser.add_argument("--shadow-from-json", default=None)
    parser.add_argument("--horizon", type=int, default=21)
    parser.add_argument("--outer-folds", type=int, default=4)
    parser.add_argument("--inner-folds", type=int, default=3)
    parser.add_argument("--embargo", type=int, default=None)
    parser.add_argument("--quantiles", type=float, nargs="+", default=[0.2, 0.3])
    parser.add_argument("--fdr-q", type=float, default=0.10)
    parser.add_argument("--min-names", type=int, default=5)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)

    bars = _load_bars(args.bars_from_json)
    baseline_records = None
    if args.shadow_from_json:
        baseline_records = json.loads(Path(args.shadow_from_json).read_text())

    config = EvaluationConfig(
        horizon=args.horizon, n_outer=args.outer_folds, n_inner=args.inner_folds,
        embargo=args.embargo if args.embargo is not None else args.horizon,
        quantiles=tuple(args.quantiles), fdr_q=args.fdr_q, min_names=args.min_names, seed=args.seed,
    )
    evaluation = evaluate_factors(bars, baseline_records=baseline_records, config=config)
    card = build_run_card(evaluation, _git_revision(), _checksum(args.bars_from_json))
    path = write_run_card(card, args.output_dir)
    print(f"wrote factor evaluation run card: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/research/evaluation/test_runcard.py tests/scripts/test_run_factor_evaluation.py -v`
Expected: PASS (2 tests).

- [ ] **Step 6: Commit**

```bash
git add research/evaluation/runcard.py research/evaluation/__init__.py scripts/run_factor_evaluation.py tests/research/evaluation/test_runcard.py tests/scripts/test_run_factor_evaluation.py
git commit -m "feat: add factor evaluation run card and CLI"
```

---

### Task 9: Boundary Enforcement, Smoke Test, and Phase Acceptance

**Files:**
- Modify: `tests/research/test_architecture.py`
- Test: (reuses existing suites)

**Interfaces:**
- Verifies the isolation boundary now covers `research/evaluation/`; produces no new runtime API.

- [ ] **Step 1: Extend the boundary scan to the evaluation subpackage**

Inspect `tests/research/test_architecture.py`. It scans `Path("research").rglob("*.py")`, so `research/evaluation/` is already covered by the existing walker. Add an explicit assertion so a regression is unambiguous:

```python
def test_evaluation_subpackage_is_scanned_by_boundary():
    from pathlib import Path
    scanned = {str(p) for p in Path("research").rglob("*.py")}
    assert any("research/evaluation/" in p for p in scanned)
```

- [ ] **Step 2: Run the boundary and full research suites**

Run: `pytest tests/research/ -v`
Expected: PASS — all Phase 1-2 tests plus the new `tests/research/evaluation/` tests and the boundary assertion.

- [ ] **Step 3: Run the deterministic smoke test on frozen bars**

The canonical frozen artifact lives in the primary checkout at
`/Users/huiliang/GitHub/algo-poc/output/backtest_multi_20260710_005841.json`
(the corrected baseline named in `docs/strategies/portfolio-2026-05.md`). Run to
a scratch directory — never under `output/`:

```bash
python scripts/run_factor_evaluation.py \
  --bars-from-json /Users/huiliang/GitHub/algo-poc/output/backtest_multi_20260710_005841.json \
  --output-dir /tmp/algo-poc-factor-eval
```

Expected: exit 0; one `factor_evaluation_<cutoff>.json` written under
`/tmp/algo-poc-factor-eval` containing all four factor keys with `sharpe`,
`deflated_sharpe`, `survives_multiple_testing`, `ic_mean`, and `overlap_counts`.
Running it twice produces byte-identical files (determinism).

- [ ] **Step 4: Run the full repository test suite**

Run: `pytest`
Expected: all tests pass with zero failures. (If the repo's known Python 3.14 event-loop harness quirk applies, initialize the event loop as documented in the 2026-07-22 handoff.)

- [ ] **Step 5: Verify packaging includes the new subpackage**

Run: `pip wheel . --no-deps -w /tmp/algo-poc-eval-wheel && unzip -l /tmp/algo-poc-eval-wheel/algo_poc-*.whl | grep research/evaluation`
Expected: exit 0; the wheel lists `research/evaluation/` modules.

- [ ] **Step 6: Confirm no trading-path or forbidden change**

Run: `git diff --stat main..HEAD`
Expected: changes only under `research/evaluation/`, `tests/research/`, `tests/scripts/`, `scripts/run_factor_evaluation.py`, and `docs/superpowers/`. Confirm manually: no edits under `services/`, `backtest/runner.py`, `scripts/run_paper.py`, or `config/`; no new IB/Redis import; no default-on setting.

- [ ] **Step 7: Commit**

```bash
git add tests/research/test_architecture.py
git commit -m "test: extend research boundary to evaluation subpackage"
```

## Phase Acceptance Criteria

This plan is complete only when all of the following are demonstrated:

- Each of the four catalog factors is evaluated with nested, purged, embargoed walk-forward over frozen bars.
- The quantile cutoff is selected only inside inner folds; each outer-test span is scored exactly once.
- Factor edge is measured as top-quantile long-only excess return over the equal-weight universe, with IC reported alongside.
- Deflated Sharpe and BH-FDR both gate `survives_multiple_testing`.
- Overlap cohorts and per-cohort returns are reported when a baseline set is supplied.
- Each run writes one immutable, provenance-complete run card; identical inputs reproduce it byte-for-byte.
- `research/evaluation/` imports no execution, risk, IB, or Redis surface, enforced by the boundary test.
- No trading-path file changes; the full repository test suite and the package build pass.
