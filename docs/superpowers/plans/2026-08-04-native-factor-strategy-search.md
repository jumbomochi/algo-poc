# Native Factor Strategy Search (Phase 4) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the `research/strategy/` subpackage that combines ≤3 validated factors into versioned, sleeve-targeted strategies and scores each with an offline Research Validation Score, authorizing nothing and touching no capital.

**Architecture:** A pure offline research subpackage layered on the Phase 1–3 factor/evaluation code. Factors are z-scored and weight-blended (`combine.py`); a deterministic inner-fold-confined optimizer fits weights + entry threshold (`optimizer.py`); nested walk-forward search scores each factor-set specification once on the outer test and applies Deflated-Sharpe + BH-FDR across specs (`search.py`); sleeve-specific objectives/guardrails gate the two price-evaluable sleeves and register the other four as explicitly blocked (`objectives.py`); the Research Validation Score composes five measurable components plus an `unavailable` concordance slot into an `offline_subtotal` out of 80 (`rvs.py`); an immutable run card and thin CLI expose it (`runcard.py`, `scripts/run_strategy_search.py`).

**Tech Stack:** Python 3.12, pandas, numpy, pytest (`asyncio_mode="auto"`). No new third-party runtime dependency; no scipy (normal CDF via `math.erf`, already in `research/evaluation/metrics.py`).

## Global Constraints

- Every module begins with `from __future__ import annotations`.
- `research/strategy/` must NOT import: `services.execution`, `services.risk_management`, `ib_insync`, `ibapi`, `redis`, `shared.redis_client`, `shared.schemas.messages`, `backtest.runner`, `scripts.run_paper`, `importlib`, `runpy`, `builtins.__import__`, or use a direct `__import__` call. It MAY import `research.factors.*`, `research.evaluation.*`, `pandas`, `numpy`, and stdlib.
- Deterministic by identity: same frozen bars + same code revision + same seed → byte-identical run card. No wall-clock or Python-hash-seed dependence enters any reported number. Any wall-clock stamp is passed in, never read internally.
- No synthetic data on blocked paths: the four data-blocked sleeves are registered as blocked and never run against invented inputs.
- Run cards are written under `--output-dir` and MUST refuse any path containing an `output` path component (mirror `research/evaluation/runcard.py`).
- Research Validation Score: paper/model concordance is reported `unavailable` and excluded from the subtotal; NO renormalization to 100; `live_eligible` is always `false` with reason `awaiting_paper_concordance`.
- TDD: write the failing test first, watch it fail, implement minimally, watch it pass, commit. Commit after every task.
- Run the full suite with an explicit event loop when doing a final check (known Python 3.14 harness quirk): `python -c "import asyncio; asyncio.set_event_loop(asyncio.new_event_loop())"` is not needed for `pytest` on 3.12; use plain `pytest` per task.

## Reused interfaces (already in the repo — do not reimplement)

From `research/factors`:
- `research.factors.panel.build_factor_panel(bars_by_ticker) -> FactorPanel`
- `research.factors.catalog.build_default_registry() -> FactorRegistry`, `DEFAULT_FACTOR_IDS: tuple[str,...]`
- `FactorRegistry.get(factor_id) -> Factor`; `Factor.compute(panel) -> pd.DataFrame` (date×ticker); `Factor.spec` has `.factor_id`, `.version`, `.family`.
- The four catalog factors, all `direction=1`, `normalization_policy="none"`: `price_momentum_126d` (family `momentum`), `high_52w` (`momentum`), `low_volatility_63d` (`risk`), `liquidity_20d` (`liquidity`).
- `research.factors.operations.cross_sectional_zscore(frame) -> pd.DataFrame`
- `research.factors.engine.FactorEngine(registry).compute(panel, factor_ids) -> FactorSnapshotIndex` with `.snapshot_identity`, `.provenance.to_mapping()`.

From `research/evaluation`:
- `research.evaluation.folds.nested_walk_forward(n_dates, n_outer, n_inner, horizon, embargo) -> list[OuterFold]`; `OuterFold(train:(int,int), test:(int,int), inner:tuple[InnerFold,...])`; `InnerFold(train:(int,int), validate:(int,int))`. Slice frames with `.iloc[start:end]`.
- `research.evaluation.forward_returns.forward_excess_returns(panel, horizon) -> pd.DataFrame` (date×ticker).
- `research.evaluation.portfolio.quantile_long_only(scores, forward, quantile, rebalance, min_names=5) -> PortfolioSeries`; `PortfolioSeries(returns, turnover, ic)` (all `pd.Series`).
- `research.evaluation.portfolio.top_quantile_names(score_row, forward_row, quantile, min_names) -> list[str]`.
- `research.evaluation.metrics.sharpe(returns, periods_per_year) -> float`, `max_drawdown(returns) -> float`, `annualized_turnover(turnover, periods_per_year) -> float`, `sharpe_stats(returns) -> {"n","sr","skew","kurt"}`, `ic_summary(ic) -> {"mean","t_stat","p_value","hit_rate"}`.
- `research.evaluation.multiple_testing.control(per_spec, q=0.10, dsr_threshold=0.95) -> dict[str, MultipleTestingVerdict]`; each input dict value has keys `sr,n,skew,kurt,ic_p`; `MultipleTestingVerdict(deflated_sharpe, passes_dsr, passes_fdr, survives)`. `n_trials` is `len(per_spec)` implicitly — passing all specs makes `n_trials == #specs`.
- `research.evaluation.overlap.baseline_selections_from_records(records) -> dict[date,set[str]]`; `attribute(selection, baseline, forward) -> OverlapReport(counts: dict[str,int], cohort_returns: dict[str,float])`. Cohort keys: `"research_only"`, `"overlap"`, `"baseline_only"`.

## File Structure

- Create `research/strategy/__init__.py` — package marker (empty).
- Create `research/strategy/combine.py` — `combine_factor_scores(frames, weights) -> pd.DataFrame` (Task 1).
- Create `research/strategy/spec.py` — `StrategySearchSpace`, `FittedStrategy`, family-compat map, enumeration, hashing (Task 2).
- Create `research/strategy/objectives.py` — `SleeveObjective`/`Guardrail` protocols, `ObjectiveContext`, `GuardrailResult`, `ObjectiveDataUnavailable`, momentum + thematic implementations, 4 blocked registrations, `objective_for`/`guardrails_for` (Task 3).
- Create `research/strategy/optimizer.py` — `optimize_weights(...) -> WeightFit` (Task 4).
- Create `research/strategy/search.py` — `SearchConfig`, `search_sleeve`, `search_strategies` (Task 5).
- Create `research/strategy/rvs.py` — `ResearchValidationScore`, `compute_rvs(...)` (Task 6).
- Create `research/strategy/runcard.py` — `build_strategy_run_card`, `write_strategy_run_card` (Task 7).
- Create `scripts/run_strategy_search.py` — CLI (Task 7).
- Create tests under `tests/research/strategy/`.
- Modify `tests/research/test_architecture.py` — add strategy-subpackage boundary assertion (Task 7).

---

### Task 1: Factor combination primitive

**Files:**
- Create: `research/strategy/__init__.py`
- Create: `research/strategy/combine.py`
- Test: `tests/research/strategy/__init__.py`, `tests/research/strategy/test_combine.py`

**Interfaces:**
- Consumes: `research.factors.operations.cross_sectional_zscore`.
- Produces: `combine_factor_scores(frames: dict[str, pd.DataFrame], weights: dict[str, float]) -> pd.DataFrame` — z-scores each factor frame cross-sectionally per date, multiplies by its weight, and sums into one date×ticker combined-score frame. All input frames share an index/columns. Weight keys must equal frame keys.

- [ ] **Step 1: Create package markers**

Create `research/strategy/__init__.py` (empty file) and `tests/research/strategy/__init__.py` (empty file).

- [ ] **Step 2: Write the failing test**

Create `tests/research/strategy/test_combine.py`:

```python
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from research.strategy.combine import combine_factor_scores


def _frame(values: dict[str, list[float]], dates: list[str]) -> pd.DataFrame:
    idx = pd.to_datetime(dates)
    return pd.DataFrame(values, index=idx)


def test_equal_weight_blend_of_two_zscored_factors():
    dates = ["2020-01-01", "2020-01-02"]
    a = _frame({"AAA": [1.0, 1.0], "BBB": [2.0, 2.0], "CCC": [3.0, 3.0]}, dates)
    b = _frame({"AAA": [3.0, 3.0], "BBB": [2.0, 2.0], "CCC": [1.0, 1.0]}, dates)
    combined = combine_factor_scores({"a": a, "b": b}, {"a": 0.5, "b": 0.5})
    # a and b are exact opposites after z-scoring; equal-weight blend is flat.
    row = combined.iloc[0]
    assert row["AAA"] == pytest.approx(0.0, abs=1e-9)
    assert row["BBB"] == pytest.approx(0.0, abs=1e-9)
    assert row["CCC"] == pytest.approx(0.0, abs=1e-9)


def test_weights_change_the_ranking():
    dates = ["2020-01-01"]
    a = _frame({"AAA": [1.0], "BBB": [2.0], "CCC": [3.0]}, dates)
    b = _frame({"AAA": [3.0], "BBB": [2.0], "CCC": [1.0]}, dates)
    tilt_a = combine_factor_scores({"a": a, "b": b}, {"a": 0.9, "b": 0.1}).iloc[0]
    tilt_b = combine_factor_scores({"a": a, "b": b}, {"a": 0.1, "b": 0.9}).iloc[0]
    # Tilting toward a ranks CCC top; tilting toward b ranks AAA top.
    assert tilt_a.idxmax() == "CCC"
    assert tilt_b.idxmax() == "AAA"


def test_invariant_to_positive_affine_rescale_of_an_input():
    dates = ["2020-01-01"]
    a = _frame({"AAA": [1.0], "BBB": [2.0], "CCC": [3.0]}, dates)
    b = _frame({"AAA": [3.0], "BBB": [2.0], "CCC": [1.0]}, dates)
    base = combine_factor_scores({"a": a, "b": b}, {"a": 0.5, "b": 0.5})
    rescaled = combine_factor_scores({"a": a * 10.0 + 5.0, "b": b}, {"a": 0.5, "b": 0.5})
    # Cross-sectional z-score removes scale/offset, so ranking is unchanged.
    pd.testing.assert_series_equal(
        base.iloc[0].rank(), rescaled.iloc[0].rank(), check_names=False
    )


def test_weight_keys_must_match_frame_keys():
    dates = ["2020-01-01"]
    a = _frame({"AAA": [1.0]}, dates)
    with pytest.raises(ValueError):
        combine_factor_scores({"a": a}, {"b": 1.0})
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/research/strategy/test_combine.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'research.strategy.combine'`.

- [ ] **Step 4: Write minimal implementation**

Create `research/strategy/combine.py`:

```python
from __future__ import annotations

import pandas as pd

from research.factors.operations import cross_sectional_zscore


def combine_factor_scores(
    frames: dict[str, pd.DataFrame], weights: dict[str, float]
) -> pd.DataFrame:
    """Z-score each factor frame cross-sectionally, weight, and sum.

    Weighted blends are only meaningful across comparable scales, so every
    factor is z-scored per date before weighting. Weight keys must exactly
    match frame keys.
    """
    if set(frames) != set(weights):
        raise ValueError("weights keys must match frame keys exactly")
    combined: pd.DataFrame | None = None
    for key, frame in frames.items():
        contribution = cross_sectional_zscore(frame) * float(weights[key])
        combined = contribution if combined is None else combined + contribution
    if combined is None:
        raise ValueError("frames must be non-empty")
    return combined
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/research/strategy/test_combine.py -v`
Expected: PASS (4 passed).

- [ ] **Step 6: Commit**

```bash
git add research/strategy/__init__.py research/strategy/combine.py tests/research/strategy/
git commit -m "feat: add z-score factor combination primitive"
```

---

### Task 2: Strategy specification, enumeration, and hashing

**Files:**
- Create: `research/strategy/spec.py`
- Test: `tests/research/strategy/test_spec.py`

**Interfaces:**
- Consumes: nothing from earlier tasks (pure).
- Produces:
  - `SLEEVE_COMPATIBLE_FAMILIES: dict[str, frozenset[str]]`
  - `EVALUABLE_SLEEVES: tuple[str, ...]` == `("momentum", "thematic_momentum")`
  - `BLOCKED_SLEEVES: dict[str, str]` (sleeve → required-data phrase)
  - `enumerate_factor_sets(pool: list[tuple[str, str]], compatible_families: frozenset[str], max_factors: int = 3) -> list[tuple[str, ...]]` where `pool` is a list of `(factor_id, family)`; returns sorted tuples of ≤`max_factors` compatible factor ids, each tuple sorted, the outer list sorted.
  - `FittedStrategy` frozen dataclass: `strategy_id: str`, `version: str`, `target_sleeve: str`, `factor_set: tuple[str, ...]`, `factor_versions: tuple[str, ...]`, `objective_id: str`, `weights_by_fold: tuple[tuple[float, ...], ...]`, `threshold: float`, `seed: int`, `fold_signature: str`, `input_checksum: str`. Property `strategy_hash: str` — deterministic sha256 over all identity fields.

- [ ] **Step 1: Write the failing test**

Create `tests/research/strategy/test_spec.py`:

```python
from __future__ import annotations

import pytest

from research.strategy.spec import (
    BLOCKED_SLEEVES,
    EVALUABLE_SLEEVES,
    SLEEVE_COMPATIBLE_FAMILIES,
    FittedStrategy,
    enumerate_factor_sets,
)


def test_evaluable_and_blocked_sleeves_partition_the_six():
    six = {
        "momentum",
        "thematic_momentum",
        "sector_rotation",
        "quality_value",
        "earnings_drift",
        "tail_risk_hedge",
    }
    assert set(EVALUABLE_SLEEVES) | set(BLOCKED_SLEEVES) == six
    assert set(EVALUABLE_SLEEVES).isdisjoint(BLOCKED_SLEEVES)
    assert set(SLEEVE_COMPATIBLE_FAMILIES) == six


def test_enumerate_yields_all_subsets_up_to_three_of_compatible_factors():
    pool = [
        ("price_momentum_126d", "momentum"),
        ("high_52w", "momentum"),
        ("low_volatility_63d", "risk"),
        ("liquidity_20d", "liquidity"),
    ]
    families = frozenset({"momentum", "risk", "liquidity"})
    sets = enumerate_factor_sets(pool, families, max_factors=3)
    # C(4,1)+C(4,2)+C(4,3) = 4+6+4 = 14
    assert len(sets) == 14
    assert all(len(s) <= 3 for s in sets)
    assert all(tuple(sorted(s)) == s for s in sets)
    assert sets == sorted(sets)
    assert ("high_52w", "price_momentum_126d") in sets


def test_enumerate_excludes_incompatible_families():
    pool = [("f_mom", "momentum"), ("f_sector", "sector")]
    sets = enumerate_factor_sets(pool, frozenset({"momentum"}), max_factors=3)
    assert sets == [("f_mom",)]


def test_strategy_hash_is_deterministic_and_sensitive_to_weights():
    base = FittedStrategy(
        strategy_id="momentum::price_momentum_126d",
        version="v1",
        target_sleeve="momentum",
        factor_set=("price_momentum_126d",),
        factor_versions=("1.0.0",),
        objective_id="momentum",
        weights_by_fold=((1.0,),),
        threshold=0.2,
        seed=7,
        fold_signature="o4-i3-h21-e21",
        input_checksum="sha256:abc",
    )
    same = FittedStrategy(**{**base.__dict__})
    assert base.strategy_hash == same.strategy_hash
    tweaked = FittedStrategy(**{**base.__dict__, "weights_by_fold": ((0.9,),)})
    assert tweaked.strategy_hash != base.strategy_hash
    tweaked_thr = FittedStrategy(**{**base.__dict__, "threshold": 0.3})
    assert tweaked_thr.strategy_hash != base.strategy_hash
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/research/strategy/test_spec.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'research.strategy.spec'`.

- [ ] **Step 3: Write minimal implementation**

Create `research/strategy/spec.py`:

```python
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from itertools import combinations

SLEEVE_COMPATIBLE_FAMILIES: dict[str, frozenset[str]] = {
    "momentum": frozenset({"momentum", "risk", "liquidity"}),
    "thematic_momentum": frozenset({"momentum", "risk", "liquidity"}),
    "sector_rotation": frozenset({"sector", "momentum", "risk"}),
    "quality_value": frozenset({"value", "quality", "sector"}),
    "earnings_drift": frozenset({"event", "momentum"}),
    "tail_risk_hedge": frozenset({"risk", "regime"}),
}

EVALUABLE_SLEEVES: tuple[str, ...] = ("momentum", "thematic_momentum")

BLOCKED_SLEEVES: dict[str, str] = {
    "sector_rotation": "sector map",
    "quality_value": "fundamentals + sector",
    "earnings_drift": "earnings event dates",
    "tail_risk_hedge": "regime/crisis series",
}


def enumerate_factor_sets(
    pool: list[tuple[str, str]],
    compatible_families: frozenset[str],
    max_factors: int = 3,
) -> list[tuple[str, ...]]:
    """All sorted ≤max_factors subsets of pool factors in compatible families."""
    eligible = sorted(fid for fid, family in pool if family in compatible_families)
    sets: list[tuple[str, ...]] = []
    for size in range(1, max_factors + 1):
        for combo in combinations(eligible, size):
            sets.append(tuple(sorted(combo)))
    return sorted(sets)


@dataclass(frozen=True)
class FittedStrategy:
    strategy_id: str
    version: str
    target_sleeve: str
    factor_set: tuple[str, ...]
    factor_versions: tuple[str, ...]
    objective_id: str
    weights_by_fold: tuple[tuple[float, ...], ...]
    threshold: float
    seed: int
    fold_signature: str
    input_checksum: str

    @property
    def strategy_hash(self) -> str:
        payload = json.dumps(
            {
                "strategy_id": self.strategy_id,
                "version": self.version,
                "target_sleeve": self.target_sleeve,
                "factor_set": list(self.factor_set),
                "factor_versions": list(self.factor_versions),
                "objective_id": self.objective_id,
                "weights_by_fold": [list(w) for w in self.weights_by_fold],
                "threshold": self.threshold,
                "seed": self.seed,
                "fold_signature": self.fold_signature,
                "input_checksum": self.input_checksum,
            },
            sort_keys=True,
        ).encode()
        return f"sha256:{hashlib.sha256(payload).hexdigest()}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/research/strategy/test_spec.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add research/strategy/spec.py tests/research/strategy/test_spec.py
git commit -m "feat: add strategy spec enumeration and hashing"
```

---

### Task 3: Sleeve objectives and guardrails

**Files:**
- Create: `research/strategy/objectives.py`
- Test: `tests/research/strategy/test_objectives.py`

**Interfaces:**
- Consumes: `research.evaluation.metrics.{sharpe, max_drawdown, annualized_turnover}`; `research.evaluation.portfolio.PortfolioSeries`.
- Produces:
  - `class ObjectiveDataUnavailable(Exception)` — carries `.sleeve` and `.needs`.
  - `@dataclass(frozen=True) class ObjectiveContext`: `periods_per_year: float`, `per_fold_sharpes: tuple[float, ...] = ()`, `holdings_by_date: dict = None`.
  - `@dataclass(frozen=True) class GuardrailResult`: `name: str`, `passed: bool`, `reason: str`.
  - `class SleeveObjective(Protocol)`: `objective_id: str`; `score(self, series: PortfolioSeries, context: ObjectiveContext) -> float`.
  - `class Guardrail(Protocol)`: `name: str`; `check(self, series: PortfolioSeries, context: ObjectiveContext) -> GuardrailResult`.
  - Concrete objectives: `MomentumObjective`, `ThematicMomentumObjective`; concrete guardrails: `MaxDrawdownGuardrail(ceiling)`, `TurnoverGuardrail(ceiling)`, `ConcentrationGuardrail(max_weight)`, `CrashLossGuardrail(floor)`; `BlockedObjective(sleeve, needs)`.
  - `objective_for(sleeve: str) -> SleeveObjective`, `guardrails_for(sleeve: str) -> tuple[Guardrail, ...]`, `is_blocked(sleeve: str) -> bool`.
  - Module constants: `MOMENTUM_CONSISTENCY_LAMBDA = 0.5`, `MAX_DRAWDOWN_CEILING = 0.25`, `TURNOVER_CEILING = 3.0`, `CONCENTRATION_MAX_WEIGHT = 0.5`, `CRASH_LOSS_FLOOR = -0.20`.

- [ ] **Step 1: Write the failing test**

Create `tests/research/strategy/test_objectives.py`:

```python
from __future__ import annotations

import pandas as pd
import pytest

from research.evaluation.portfolio import PortfolioSeries
from research.strategy.objectives import (
    BlockedObjective,
    ConcentrationGuardrail,
    CrashLossGuardrail,
    MaxDrawdownGuardrail,
    MomentumObjective,
    ObjectiveContext,
    ObjectiveDataUnavailable,
    ThematicMomentumObjective,
    TurnoverGuardrail,
    guardrails_for,
    is_blocked,
    objective_for,
)


def _series(returns, turnover=None, ic=None) -> PortfolioSeries:
    r = pd.Series(returns, dtype=float)
    t = pd.Series(turnover if turnover is not None else [0.0] * len(returns), dtype=float)
    i = pd.Series(ic if ic is not None else [0.0] * len(returns), dtype=float)
    return PortfolioSeries(returns=r, turnover=t, ic=i)


def test_momentum_objective_penalizes_fold_dispersion():
    series = _series([0.01, 0.01, 0.01, 0.01])
    steady = MomentumObjective().score(
        series, ObjectiveContext(periods_per_year=12.0, per_fold_sharpes=(1.0, 1.0))
    )
    choppy = MomentumObjective().score(
        series, ObjectiveContext(periods_per_year=12.0, per_fold_sharpes=(0.2, 1.8))
    )
    assert steady > choppy


def test_thematic_objective_is_annualized_sharpe():
    from research.evaluation.metrics import sharpe

    series = _series([0.01, -0.005, 0.02, 0.0, 0.015])
    ctx = ObjectiveContext(periods_per_year=12.0)
    assert ThematicMomentumObjective().score(series, ctx) == pytest.approx(
        sharpe(series.returns, 12.0)
    )


def test_max_drawdown_guardrail_vetoes_on_breach():
    breaching = _series([-0.3, -0.05, 0.02])
    result = MaxDrawdownGuardrail(ceiling=0.25).check(
        breaching, ObjectiveContext(periods_per_year=12.0)
    )
    assert result.passed is False
    passing = _series([0.01, -0.02, 0.03])
    assert MaxDrawdownGuardrail(ceiling=0.25).check(
        passing, ObjectiveContext(periods_per_year=12.0)
    ).passed is True


def test_turnover_guardrail_vetoes_on_breach():
    series = _series([0.01, 0.01], turnover=[1.0, 1.0])
    # annualized turnover = mean(1.0) * (12) = 12 > 3.0 ceiling
    result = TurnoverGuardrail(ceiling=3.0).check(
        series, ObjectiveContext(periods_per_year=12.0)
    )
    assert result.passed is False


def test_concentration_guardrail_uses_min_holdings():
    series = _series([0.01, 0.01])
    ctx = ObjectiveContext(
        periods_per_year=12.0,
        holdings_by_date={"2020-01-01": ["AAA"], "2020-01-02": ["AAA", "BBB", "CCC"]},
    )
    # smallest book has 1 name -> equal weight 1.0 > 0.5 ceiling -> veto
    assert ConcentrationGuardrail(max_weight=0.5).check(series, ctx).passed is False
    ctx_ok = ObjectiveContext(
        periods_per_year=12.0,
        holdings_by_date={"2020-01-01": ["AAA", "BBB", "CCC"]},
    )
    assert ConcentrationGuardrail(max_weight=0.5).check(series, ctx_ok).passed is True


def test_crash_loss_guardrail_vetoes_on_worst_period():
    series = _series([0.01, -0.25, 0.02])
    assert CrashLossGuardrail(floor=-0.20).check(
        series, ObjectiveContext(periods_per_year=12.0)
    ).passed is False


def test_blocked_objective_raises_and_reports_need():
    obj = BlockedObjective(sleeve="sector_rotation", needs="sector map")
    with pytest.raises(ObjectiveDataUnavailable) as exc:
        obj.score(_series([0.0]), ObjectiveContext(periods_per_year=12.0))
    assert exc.value.needs == "sector map"


def test_registry_wires_evaluable_and_blocked_sleeves():
    assert objective_for("momentum").objective_id == "momentum"
    assert objective_for("thematic_momentum").objective_id == "thematic_momentum"
    assert is_blocked("sector_rotation") is True
    assert is_blocked("momentum") is False
    assert len(guardrails_for("momentum")) >= 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/research/strategy/test_objectives.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'research.strategy.objectives'`.

- [ ] **Step 3: Write minimal implementation**

Create `research/strategy/objectives.py`:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import pandas as pd

from research.evaluation.metrics import annualized_turnover, max_drawdown, sharpe
from research.evaluation.portfolio import PortfolioSeries
from research.strategy.spec import BLOCKED_SLEEVES

MOMENTUM_CONSISTENCY_LAMBDA = 0.5
MAX_DRAWDOWN_CEILING = 0.25
TURNOVER_CEILING = 3.0
CONCENTRATION_MAX_WEIGHT = 0.5
CRASH_LOSS_FLOOR = -0.20


class ObjectiveDataUnavailable(Exception):
    def __init__(self, sleeve: str, needs: str) -> None:
        super().__init__(f"{sleeve} objective is blocked: needs {needs}")
        self.sleeve = sleeve
        self.needs = needs


@dataclass(frozen=True)
class ObjectiveContext:
    periods_per_year: float
    per_fold_sharpes: tuple[float, ...] = ()
    holdings_by_date: dict | None = None


@dataclass(frozen=True)
class GuardrailResult:
    name: str
    passed: bool
    reason: str


@runtime_checkable
class SleeveObjective(Protocol):
    objective_id: str

    def score(self, series: PortfolioSeries, context: ObjectiveContext) -> float: ...


@runtime_checkable
class Guardrail(Protocol):
    name: str

    def check(self, series: PortfolioSeries, context: ObjectiveContext) -> GuardrailResult: ...


def _stdev(values: tuple[float, ...]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    return (sum((v - mean) ** 2 for v in values) / (n - 1)) ** 0.5


@dataclass(frozen=True)
class MomentumObjective:
    objective_id: str = "momentum"

    def score(self, series: PortfolioSeries, context: ObjectiveContext) -> float:
        base = sharpe(series.returns, context.periods_per_year)
        penalty = MOMENTUM_CONSISTENCY_LAMBDA * _stdev(context.per_fold_sharpes)
        return base - penalty


@dataclass(frozen=True)
class ThematicMomentumObjective:
    objective_id: str = "thematic_momentum"

    def score(self, series: PortfolioSeries, context: ObjectiveContext) -> float:
        return sharpe(series.returns, context.periods_per_year)


@dataclass(frozen=True)
class MaxDrawdownGuardrail:
    ceiling: float = MAX_DRAWDOWN_CEILING
    name: str = "max_drawdown"

    def check(self, series: PortfolioSeries, context: ObjectiveContext) -> GuardrailResult:
        dd = abs(max_drawdown(series.returns))
        passed = dd <= self.ceiling
        return GuardrailResult(self.name, passed, f"max_drawdown={dd:.4f} ceiling={self.ceiling}")


@dataclass(frozen=True)
class TurnoverGuardrail:
    ceiling: float = TURNOVER_CEILING
    name: str = "annual_turnover"

    def check(self, series: PortfolioSeries, context: ObjectiveContext) -> GuardrailResult:
        turnover = annualized_turnover(series.turnover, context.periods_per_year)
        passed = turnover <= self.ceiling
        return GuardrailResult(self.name, passed, f"annual_turnover={turnover:.4f} ceiling={self.ceiling}")


@dataclass(frozen=True)
class ConcentrationGuardrail:
    max_weight: float = CONCENTRATION_MAX_WEIGHT
    name: str = "concentration"

    def check(self, series: PortfolioSeries, context: ObjectiveContext) -> GuardrailResult:
        holdings = context.holdings_by_date or {}
        sizes = [len(names) for names in holdings.values() if names]
        smallest = min(sizes) if sizes else 0
        weight = 1.0 / smallest if smallest else 1.0
        passed = smallest > 0 and weight <= self.max_weight
        return GuardrailResult(self.name, passed, f"min_holdings={smallest} weight={weight:.4f} max={self.max_weight}")


@dataclass(frozen=True)
class CrashLossGuardrail:
    floor: float = CRASH_LOSS_FLOOR
    name: str = "crash_loss"

    def check(self, series: PortfolioSeries, context: ObjectiveContext) -> GuardrailResult:
        worst = float(series.returns.min()) if len(series.returns) else 0.0
        passed = worst >= self.floor
        return GuardrailResult(self.name, passed, f"worst_period={worst:.4f} floor={self.floor}")


@dataclass(frozen=True)
class BlockedObjective:
    sleeve: str
    needs: str
    objective_id: str = field(default="")

    def score(self, series: PortfolioSeries, context: ObjectiveContext) -> float:
        raise ObjectiveDataUnavailable(self.sleeve, self.needs)


_OBJECTIVES: dict[str, SleeveObjective] = {
    "momentum": MomentumObjective(),
    "thematic_momentum": ThematicMomentumObjective(),
}

_GUARDRAILS: dict[str, tuple[Guardrail, ...]] = {
    "momentum": (MaxDrawdownGuardrail(), TurnoverGuardrail()),
    "thematic_momentum": (ConcentrationGuardrail(), CrashLossGuardrail()),
}


def is_blocked(sleeve: str) -> bool:
    return sleeve in BLOCKED_SLEEVES


def objective_for(sleeve: str) -> SleeveObjective:
    if is_blocked(sleeve):
        return BlockedObjective(sleeve=sleeve, needs=BLOCKED_SLEEVES[sleeve], objective_id=sleeve)
    return _OBJECTIVES[sleeve]


def guardrails_for(sleeve: str) -> tuple[Guardrail, ...]:
    return _GUARDRAILS.get(sleeve, ())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/research/strategy/test_objectives.py -v`
Expected: PASS (8 passed).

- [ ] **Step 5: Commit**

```bash
git add research/strategy/objectives.py tests/research/strategy/test_objectives.py
git commit -m "feat: add sleeve objectives and guardrails with blocked registrations"
```

---

### Task 4: Deterministic inner-fold weight optimizer

**Files:**
- Create: `research/strategy/optimizer.py`
- Test: `tests/research/strategy/test_optimizer.py`

**Interfaces:**
- Consumes: `research.strategy.combine.combine_factor_scores`; `research.evaluation.portfolio.quantile_long_only`; `research.strategy.objectives.{SleeveObjective, ObjectiveContext}`; `research.evaluation.folds.InnerFold`.
- Produces:
  - `@dataclass(frozen=True) class WeightFit`: `weights: tuple[float, ...]`, `threshold: float`, `score: float`.
  - `optimize_weights(frames: dict[str, pd.DataFrame], forward: pd.DataFrame, inner_folds: tuple, thresholds: tuple[float, ...], objective: SleeveObjective, rebalance: int, min_names: int, periods_per_year: float, seed: int) -> WeightFit`. `frames` keys are the factor ids in a fixed order; returned `weights` align to `sorted(frames)`. `rebalance` is the position-holding stride passed to `quantile_long_only`; the return horizon is already baked into `forward` and is **not** a parameter here. Deterministic: fixed simplex start set (equal-weight + unit vertices), fixed coordinate-refinement schedule; `seed` recorded but not used for randomness. Weights are non-negative and sum to 1.

- [ ] **Step 1: Write the failing test**

Create `tests/research/strategy/test_optimizer.py`:

```python
from __future__ import annotations

import numpy as np
import pandas as pd

from research.evaluation.folds import InnerFold
from research.strategy.objectives import ObjectiveContext, ThematicMomentumObjective
from research.strategy.optimizer import WeightFit, optimize_weights


def _panel(n_dates=60, seed=0):
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2020-01-01", periods=n_dates, freq="B")
    tickers = [f"T{i}" for i in range(20)]
    # signal factor: higher score -> higher forward return; noise factor: unrelated
    signal = pd.DataFrame(rng.normal(size=(n_dates, 20)), index=dates, columns=tickers)
    noise = pd.DataFrame(rng.normal(size=(n_dates, 20)), index=dates, columns=tickers)
    horizon = 5
    fwd = signal.shift(-horizon) * 0.02  # forward return proportional to signal
    return {"signal": signal, "noise": noise}, fwd, horizon, dates


def test_optimizer_prefers_the_predictive_factor():
    frames, fwd, horizon, dates = _panel()
    inner = (InnerFold(train=(0, 30), validate=(30, 55)),)
    fit = optimize_weights(
        frames=frames,
        forward=fwd,
        inner_folds=inner,
        thresholds=(0.2, 0.3),
        objective=ThematicMomentumObjective(),
        rebalance=horizon,
        min_names=3,
        periods_per_year=252.0 / horizon,
        seed=7,
    )
    # weights align to sorted(frames) -> ("noise", "signal")
    assert isinstance(fit, WeightFit)
    assert abs(sum(fit.weights) - 1.0) < 1e-9
    assert all(w >= -1e-12 for w in fit.weights)
    assert fit.weights[1] > fit.weights[0]  # more mass on "signal"


def test_optimizer_is_deterministic():
    frames, fwd, horizon, _ = _panel()
    inner = (InnerFold(train=(0, 30), validate=(30, 55)),)
    kwargs = dict(
        frames=frames, forward=fwd, inner_folds=inner, thresholds=(0.2, 0.3),
        objective=ThematicMomentumObjective(), rebalance=horizon, min_names=3,
        periods_per_year=252.0 / horizon, seed=7,
    )
    a = optimize_weights(**kwargs)
    b = optimize_weights(**kwargs)
    assert a == b


def test_optimizer_only_reads_the_inner_validate_span():
    # Mutating rows outside the inner folds must not change the fit.
    frames, fwd, horizon, dates = _panel()
    inner = (InnerFold(train=(0, 30), validate=(30, 55)),)
    kwargs = dict(
        frames=frames, forward=fwd, inner_folds=inner, thresholds=(0.2, 0.3),
        objective=ThematicMomentumObjective(), rebalance=horizon, min_names=3,
        periods_per_year=252.0 / horizon, seed=7,
    )
    base = optimize_weights(**kwargs)
    mutated = {k: v.copy() for k, v in frames.items()}
    mutated["signal"].iloc[55:] = 999.0  # outside validate span
    fwd2 = fwd.copy()
    fwd2.iloc[55:] = 999.0
    after = optimize_weights(**{**kwargs, "frames": mutated, "forward": fwd2})
    assert base == after
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/research/strategy/test_optimizer.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'research.strategy.optimizer'`.

- [ ] **Step 3: Write minimal implementation**

Create `research/strategy/optimizer.py`:

```python
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from research.strategy.combine import combine_factor_scores
from research.strategy.objectives import ObjectiveContext, SleeveObjective
from research.evaluation.portfolio import quantile_long_only

# Fixed coordinate-refinement step schedule — deterministic, no RNG.
_STEP_SCHEDULE = (0.25, 0.125, 0.0625)
_REFINE_PASSES = 3


@dataclass(frozen=True)
class WeightFit:
    weights: tuple[float, ...]
    threshold: float
    score: float


def _normalize(weights: list[float]) -> tuple[float, ...]:
    clipped = [max(0.0, w) for w in weights]
    total = sum(clipped)
    if total <= 0:
        n = len(clipped)
        return tuple(1.0 / n for _ in clipped)
    return tuple(w / total for w in clipped)


def _start_points(n: int) -> list[tuple[float, ...]]:
    starts = [tuple(1.0 / n for _ in range(n))]  # equal weight
    for i in range(n):  # unit vertices
        starts.append(tuple(1.0 if j == i else 0.0 for j in range(n)))
    return starts


def _evaluate(
    keys: list[str],
    weights: tuple[float, ...],
    frames: dict[str, pd.DataFrame],
    forward: pd.DataFrame,
    inner_folds,
    threshold: float,
    objective: SleeveObjective,
    rebalance: int,
    min_names: int,
    periods_per_year: float,
) -> float:
    combined = combine_factor_scores(frames, dict(zip(keys, weights)))
    scores: list[float] = []
    for fold in inner_folds:
        v_scores = combined.iloc[fold.validate[0]:fold.validate[1]]
        v_forward = forward.iloc[fold.validate[0]:fold.validate[1]]
        series = quantile_long_only(
            v_scores, v_forward, threshold, rebalance=rebalance, min_names=min_names
        )
        if len(series.returns):
            scores.append(objective.score(series, ObjectiveContext(periods_per_year=periods_per_year)))
    return sum(scores) / len(scores) if scores else float("-inf")


def optimize_weights(
    frames: dict[str, pd.DataFrame],
    forward: pd.DataFrame,
    inner_folds,
    thresholds: tuple[float, ...],
    objective: SleeveObjective,
    rebalance: int,
    min_names: int,
    periods_per_year: float,
    seed: int,
) -> WeightFit:
    keys = sorted(frames)
    n = len(keys)
    best = WeightFit(weights=tuple(1.0 / n for _ in range(n)), threshold=thresholds[0], score=float("-inf"))
    for threshold in thresholds:
        for start in _start_points(n):
            weights = _normalize(list(start))
            score = _evaluate(keys, weights, frames, forward, inner_folds, threshold, objective, rebalance, min_names, periods_per_year)
            for step in _STEP_SCHEDULE:
                for _ in range(_REFINE_PASSES):
                    improved = False
                    for i in range(n):
                        for j in range(n):
                            if i == j:
                                continue
                            trial = list(weights)
                            trial[i] += step
                            trial[j] -= step
                            trial = _normalize(trial)
                            trial_t = tuple(trial)
                            trial_score = _evaluate(keys, trial_t, frames, forward, inner_folds, threshold, objective, rebalance, min_names, periods_per_year)
                            if trial_score > score + 1e-12:
                                weights, score, improved = trial_t, trial_score, True
                    if not improved:
                        break
            if score > best.score + 1e-12:
                best = WeightFit(weights=weights, threshold=threshold, score=score)
    return best
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/research/strategy/test_optimizer.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add research/strategy/optimizer.py tests/research/strategy/test_optimizer.py
git commit -m "feat: add deterministic inner-fold weight optimizer"
```

---

### Task 5: Search orchestration and multiple-testing

**Files:**
- Create: `research/strategy/search.py`
- Test: `tests/research/strategy/test_search.py`

**Interfaces:**
- Consumes: `research.factors.panel.build_factor_panel`; `research.factors.catalog.build_default_registry`; `research.evaluation.forward_returns.forward_excess_returns`; `research.evaluation.folds.nested_walk_forward`; `research.evaluation.portfolio.{quantile_long_only, top_quantile_names}`; `research.evaluation.metrics.{sharpe, sharpe_stats, ic_summary, max_drawdown, annualized_turnover}`; `research.evaluation.multiple_testing.control`; `research.evaluation.overlap.{baseline_selections_from_records, attribute}`; `research.strategy.spec.*`; `research.strategy.objectives.*`; `research.strategy.optimizer.optimize_weights`; `research.strategy.combine.combine_factor_scores`.
- Produces:
  - `@dataclass(frozen=True) class SearchConfig`: `horizon=21, n_outer=4, n_inner=3, embargo=21, thresholds=(0.2, 0.3), fdr_q=0.10, min_names=5, seed=7`.
  - `search_sleeve(sleeve, bars_by_ticker, baseline_records, config) -> dict` — for a blocked sleeve returns `{"sleeve", "status": "blocked: needs <data>"}`; for an evaluable sleeve returns `{"sleeve", "status": "evaluated", "strategies": {strategy_id: {...}}, "config": {...}}`.
  - `search_strategies(bars_by_ticker, sleeves, baseline_records, config) -> dict` — `{"sleeves": {sleeve: <search_sleeve result>}, "snapshot_identity", "provenance"}`.
  - Each evaluated strategy dict carries: `factor_set`, `factor_versions`, `objective_id`, `weights_by_fold`, `threshold`, `sharpe`, `deflated_sharpe`, `survives_multiple_testing`, `max_drawdown`, `annual_turnover`, `ic_mean`, `ic_t_stat`, `n_observations`, `guardrails` (list of `{name, passed, reason}`), `overlap_counts`, `overlap_returns`, `holdings_sample_dates`, `strategy_hash`.

- [ ] **Step 1: Write the failing test**

Create `tests/research/strategy/test_search.py`:

```python
from __future__ import annotations

import numpy as np

from research.strategy.search import SearchConfig, search_sleeve, search_strategies


def _bars(n_dates=320, seed=1):
    rng = np.random.default_rng(seed)
    tickers = [f"T{i}" for i in range(25)]
    bars: dict[str, list[dict]] = {}
    import datetime as dt

    start = dt.date(2020, 1, 1)
    days = [start + dt.timedelta(days=i) for i in range(n_dates)]
    for t in tickers:
        price = 100.0
        rows = []
        for d in days:
            price *= 1.0 + rng.normal(0, 0.02)
            rows.append({
                "date": d, "open": price, "high": price * 1.01,
                "low": price * 0.99, "close": price, "volume": 1_000_000,
            })
        bars[t] = rows
    return bars


def test_blocked_sleeve_returns_status_without_error():
    result = search_sleeve("sector_rotation", _bars(60), None, SearchConfig())
    assert result["status"].startswith("blocked: needs")
    assert "strategies" not in result


def test_evaluable_sleeve_produces_scored_strategies():
    result = search_sleeve("momentum", _bars(), None, SearchConfig())
    assert result["status"] == "evaluated"
    # 14 factor-set specs enumerated for momentum's compatible families
    assert len(result["strategies"]) == 14
    any_strategy = next(iter(result["strategies"].values()))
    assert "sharpe" in any_strategy
    assert "deflated_sharpe" in any_strategy
    assert "survives_multiple_testing" in any_strategy
    assert isinstance(any_strategy["guardrails"], list)
    assert "strategy_hash" in any_strategy


def test_multiple_testing_n_trials_equals_spec_count():
    # Deflated Sharpe uses n_trials = number of specs. With 14 specs the
    # deflation must be stricter than a single-trial baseline: verify the
    # field is present and finite for every spec.
    result = search_sleeve("thematic_momentum", _bars(), None, SearchConfig())
    dsr = [s["deflated_sharpe"] for s in result["strategies"].values()]
    assert len(dsr) == 14
    assert all(np.isfinite(v) for v in dsr)


def test_search_strategies_reports_all_named_sleeves():
    out = search_strategies(_bars(120), ("momentum", "earnings_drift"), None, SearchConfig())
    assert set(out["sleeves"]) == {"momentum", "earnings_drift"}
    assert out["sleeves"]["earnings_drift"]["status"].startswith("blocked")
    assert "snapshot_identity" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/research/strategy/test_search.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'research.strategy.search'`.

- [ ] **Step 3: Write minimal implementation**

Create `research/strategy/search.py`:

```python
from __future__ import annotations

from dataclasses import dataclass

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
from research.evaluation.portfolio import quantile_long_only, top_quantile_names
from research.factors.catalog import build_default_registry
from research.factors.engine import FactorEngine
from research.factors.panel import build_factor_panel
from research.strategy.combine import combine_factor_scores
from research.strategy.objectives import (
    ObjectiveContext,
    guardrails_for,
    is_blocked,
    objective_for,
)
from research.strategy.optimizer import optimize_weights
from research.strategy.spec import (
    BLOCKED_SLEEVES,
    SLEEVE_COMPATIBLE_FAMILIES,
    FittedStrategy,
    enumerate_factor_sets,
)


@dataclass(frozen=True)
class SearchConfig:
    horizon: int = 21
    n_outer: int = 4
    n_inner: int = 3
    embargo: int = 21
    thresholds: tuple[float, ...] = (0.2, 0.3)
    fdr_q: float = 0.10
    min_names: int = 5
    seed: int = 7


def _fold_signature(config: SearchConfig) -> str:
    return f"o{config.n_outer}-i{config.n_inner}-h{config.horizon}-e{config.embargo}"


def search_sleeve(sleeve, bars_by_ticker, baseline_records, config) -> dict:
    if is_blocked(sleeve):
        return {"sleeve": sleeve, "status": f"blocked: needs {BLOCKED_SLEEVES[sleeve]}"}

    registry = build_default_registry()
    panel = build_factor_panel(bars_by_ticker)
    snapshot = FactorEngine(registry).compute(panel, tuple(f.factor_id for f in _iter_specs(registry)))
    forward = forward_excess_returns(panel, config.horizon)
    input_checksum = panel.input_artifact_checksum()

    pool = [(spec.factor_id, spec.family) for spec in registry.list_specs()]
    families = SLEEVE_COMPATIBLE_FAMILIES[sleeve]
    factor_sets = enumerate_factor_sets(pool, families, max_factors=3)

    n_dates = len(panel.field("close").index)
    folds = nested_walk_forward(n_dates, config.n_outer, config.n_inner, config.horizon, config.embargo)
    periods_per_year = 252.0 / config.horizon
    objective = objective_for(sleeve)
    guardrails = guardrails_for(sleeve)
    baseline = baseline_selections_from_records(baseline_records or [])

    raw_frames = {fid: registry.get(fid).compute(panel).astype(float) for fid in {f for s in factor_sets for f in s}}
    versions = {spec.factor_id: spec.version for spec in registry.list_specs()}

    per_spec_stats: dict[str, dict] = {}
    per_spec_ctx: dict[str, dict] = {}

    for factor_set in factor_sets:
        strategy_id = f"{sleeve}::{'+'.join(factor_set)}"
        frames = {fid: raw_frames[fid] for fid in factor_set}
        oos_returns: list[pd.Series] = []
        oos_ic: list[pd.Series] = []
        oos_turnover: list[pd.Series] = []
        per_fold_sharpes: list[float] = []
        weights_by_fold: list[tuple[float, ...]] = []
        holdings_by_date: dict[str, list[str]] = {}
        threshold = config.thresholds[0]
        for outer in folds:
            fit = optimize_weights(
                frames={k: v.iloc[outer.train[0]:outer.train[1]] for k, v in frames.items()},
                forward=forward.iloc[outer.train[0]:outer.train[1]],
                inner_folds=outer.inner,
                thresholds=config.thresholds,
                objective=objective,
                rebalance=config.horizon,  # rebalance cadence; horizon lives in `forward`
                min_names=config.min_names,
                periods_per_year=periods_per_year,
                seed=config.seed,
            )
            weights_by_fold.append(fit.weights)
            threshold = fit.threshold
            combined = combine_factor_scores(frames, dict(zip(sorted(frames), fit.weights)))
            test_scores = combined.iloc[outer.test[0]:outer.test[1]]
            test_forward = forward.iloc[outer.test[0]:outer.test[1]]
            series = quantile_long_only(
                test_scores, test_forward, threshold,
                rebalance=config.horizon, min_names=config.min_names,
            )
            oos_returns.append(series.returns)
            oos_ic.append(series.ic)
            oos_turnover.append(series.turnover)
            if len(series.returns):
                per_fold_sharpes.append(sharpe(series.returns, periods_per_year))
            for i in range(0, len(test_scores.index), config.horizon):
                day = test_scores.index[i]
                names = top_quantile_names(test_scores.loc[day], test_forward.loc[day], threshold, config.min_names)
                if names:
                    holdings_by_date[day.date().isoformat()] = names

        returns = pd.concat(oos_returns) if oos_returns else pd.Series(dtype=float)
        ic = pd.concat(oos_ic) if oos_ic else pd.Series(dtype=float)
        turnover = pd.concat(oos_turnover) if oos_turnover else pd.Series(dtype=float)
        stats = sharpe_stats(returns)
        ic_stat = ic_summary(ic)
        per_spec_stats[strategy_id] = {
            "sr": stats["sr"], "n": stats["n"], "skew": stats["skew"],
            "kurt": stats["kurt"], "ic_p": ic_stat["p_value"],
        }
        per_spec_ctx[strategy_id] = {
            "factor_set": factor_set,
            "factor_versions": tuple(versions[f] for f in factor_set),
            "returns": returns, "ic": ic, "turnover": turnover, "ic_stat": ic_stat,
            "per_fold_sharpes": tuple(per_fold_sharpes),
            "weights_by_fold": tuple(weights_by_fold), "threshold": threshold,
            "holdings_by_date": holdings_by_date, "input_checksum": input_checksum,
        }

    verdicts = control(per_spec_stats, q=config.fdr_q)

    strategies: dict[str, dict] = {}
    for strategy_id, ctx in per_spec_ctx.items():
        verdict = verdicts[strategy_id]
        returns = ctx["returns"]
        obj_ctx = ObjectiveContext(
            periods_per_year=periods_per_year,
            per_fold_sharpes=ctx["per_fold_sharpes"],
            holdings_by_date=ctx["holdings_by_date"],
        )
        from research.evaluation.portfolio import PortfolioSeries
        series = PortfolioSeries(returns=returns, turnover=ctx["turnover"], ic=ctx["ic"])
        guardrail_results = [g.check(series, obj_ctx).__dict__ for g in guardrails]
        selection = {pd.Timestamp(d).date(): set(names) for d, names in ctx["holdings_by_date"].items()}
        overlap = attribute(selection, baseline, forward)
        fitted = FittedStrategy(
            strategy_id=strategy_id, version="v1", target_sleeve=sleeve,
            factor_set=ctx["factor_set"], factor_versions=ctx["factor_versions"],
            objective_id=objective.objective_id, weights_by_fold=ctx["weights_by_fold"],
            threshold=ctx["threshold"], seed=config.seed,
            fold_signature=_fold_signature(config), input_checksum=ctx["input_checksum"],
        )
        strategies[strategy_id] = {
            "factor_set": list(ctx["factor_set"]),
            "factor_versions": list(ctx["factor_versions"]),
            "objective_id": objective.objective_id,
            "weights_by_fold": [list(w) for w in ctx["weights_by_fold"]],
            "threshold": ctx["threshold"],
            "sharpe": sharpe(returns, periods_per_year),
            "deflated_sharpe": verdict.deflated_sharpe,
            "survives_multiple_testing": verdict.survives,
            "max_drawdown": max_drawdown(returns),
            "annual_turnover": annualized_turnover(ctx["turnover"], periods_per_year),
            "ic_mean": ctx["ic_stat"]["mean"],
            "ic_t_stat": ctx["ic_stat"]["t_stat"],
            "n_observations": int(len(returns)),
            "guardrails": guardrail_results,
            "overlap_counts": dict(sorted(overlap.counts.items())),
            "overlap_returns": dict(sorted(overlap.cohort_returns.items())),
            "holdings_sample_dates": sorted(ctx["holdings_by_date"].keys()),
            "strategy_hash": fitted.strategy_hash,
        }

    return {
        "sleeve": sleeve,
        "status": "evaluated",
        "strategies": dict(sorted(strategies.items())),
        "config": {
            "horizon": config.horizon, "n_outer": config.n_outer, "n_inner": config.n_inner,
            "embargo": config.embargo, "thresholds": list(config.thresholds),
            "fdr_q": config.fdr_q, "min_names": config.min_names, "seed": config.seed,
        },
    }


def _iter_specs(registry):
    return registry.list_specs()


def search_strategies(bars_by_ticker, sleeves, baseline_records, config) -> dict:
    registry = build_default_registry()
    panel = build_factor_panel(bars_by_ticker)
    snapshot = FactorEngine(registry).compute(panel, tuple(s.factor_id for s in registry.list_specs()))
    results = {sleeve: search_sleeve(sleeve, bars_by_ticker, baseline_records, config) for sleeve in sleeves}
    return {
        "sleeves": results,
        "snapshot_identity": snapshot.snapshot_identity,
        "provenance": dict(snapshot.provenance.to_mapping()),
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/research/strategy/test_search.py -v`
Expected: PASS (4 passed). (This test builds ~320 synthetic dates; it may take a few seconds.)

- [ ] **Step 5: Commit**

```bash
git add research/strategy/search.py tests/research/strategy/test_search.py
git commit -m "feat: add strategy search orchestration with multiple-testing control"
```

---

### Task 6: Research Validation Score

**Files:**
- Create: `research/strategy/rvs.py`
- Test: `tests/research/strategy/test_rvs.py`

**Interfaces:**
- Consumes: the per-strategy dict shape produced by Task 5 (`sharpe`, `survives_multiple_testing`, `annual_turnover`, `guardrails`, `overlap_counts`, `ic_t_stat`).
- Produces:
  - Module constants: `PREDICTIVE_WEIGHT=25`, `STABILITY_WEIGHT=20`, `CONCORDANCE_WEIGHT=20`, `UTILITY_WEIGHT=15`, `DIVERSIFICATION_WEIGHT=10`, `DATA_QUALITY_WEIGHT=10`, `TARGET_SHARPE=1.5`.
  - `@dataclass(frozen=True) class ResearchValidationScore`: `components: dict[str, float | None]`, `offline_subtotal: float`, `available_weight: float`, `vetoes: tuple[str, ...]`, `live_eligible: bool`, `live_eligible_reason: str`.
  - `compute_rvs(strategy: dict, per_fold_sharpes: tuple[float, ...], has_baseline: bool, coverage: float) -> ResearchValidationScore`. `coverage` in [0,1] is the share of universe dates with a valid held book. Component values: `paper_model_concordance` is always `None`; `diversification` is `None` when `has_baseline` is False. `offline_subtotal` sums non-None components; `available_weight` sums their weights; NO renormalization. `live_eligible` is always False with reason `"awaiting_paper_concordance"`. Vetoes appended: `"failed_multiple_testing"` if not `survives_multiple_testing`; `"guardrail:<name>"` for each failed guardrail.

- [ ] **Step 1: Write the failing test**

Create `tests/research/strategy/test_rvs.py`:

```python
from __future__ import annotations

from research.strategy.rvs import (
    CONCORDANCE_WEIGHT,
    ResearchValidationScore,
    compute_rvs,
)


def _strategy(**overrides) -> dict:
    base = {
        "sharpe": 1.5,
        "survives_multiple_testing": True,
        "annual_turnover": 1.0,
        "ic_t_stat": 3.0,
        "guardrails": [{"name": "max_drawdown", "passed": True, "reason": ""}],
        "overlap_counts": {"research_only": 8, "overlap": 2, "baseline_only": 0},
    }
    base.update(overrides)
    return base


def test_concordance_is_always_unavailable_and_excluded():
    rvs = compute_rvs(_strategy(), per_fold_sharpes=(1.4, 1.6), has_baseline=True, coverage=1.0)
    assert rvs.components["paper_model_concordance"] is None
    assert rvs.available_weight == 80.0  # 100 - concordance(20)
    assert rvs.offline_subtotal <= 80.0
    assert rvs.live_eligible is False
    assert rvs.live_eligible_reason == "awaiting_paper_concordance"


def test_no_renormalization_to_100():
    strong = compute_rvs(
        _strategy(sharpe=3.0, ic_t_stat=6.0),
        per_fold_sharpes=(3.0, 3.0), has_baseline=True, coverage=1.0,
    )
    # Even a maxed-out offline strategy cannot exceed the available 80 points.
    assert strong.offline_subtotal <= 80.0 + 1e-9


def test_missing_baseline_drops_diversification_from_available_weight():
    rvs = compute_rvs(_strategy(), per_fold_sharpes=(1.5,), has_baseline=False, coverage=1.0)
    assert rvs.components["diversification"] is None
    assert rvs.available_weight == 70.0  # 80 - diversification(10)


def test_failed_multiple_testing_is_a_veto():
    rvs = compute_rvs(
        _strategy(survives_multiple_testing=False),
        per_fold_sharpes=(1.5,), has_baseline=True, coverage=1.0,
    )
    assert "failed_multiple_testing" in rvs.vetoes


def test_failed_guardrail_is_a_veto():
    rvs = compute_rvs(
        _strategy(guardrails=[{"name": "crash_loss", "passed": False, "reason": "x"}]),
        per_fold_sharpes=(1.5,), has_baseline=True, coverage=1.0,
    )
    assert "guardrail:crash_loss" in rvs.vetoes
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/research/strategy/test_rvs.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'research.strategy.rvs'`.

- [ ] **Step 3: Write minimal implementation**

Create `research/strategy/rvs.py`:

```python
from __future__ import annotations

from dataclasses import dataclass

PREDICTIVE_WEIGHT = 25
STABILITY_WEIGHT = 20
CONCORDANCE_WEIGHT = 20
UTILITY_WEIGHT = 15
DIVERSIFICATION_WEIGHT = 10
DATA_QUALITY_WEIGHT = 10
TARGET_SHARPE = 1.5


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _stdev(values: tuple[float, ...]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    return (sum((v - mean) ** 2 for v in values) / (n - 1)) ** 0.5


@dataclass(frozen=True)
class ResearchValidationScore:
    components: dict[str, float | None]
    offline_subtotal: float
    available_weight: float
    vetoes: tuple[str, ...]
    live_eligible: bool
    live_eligible_reason: str


def compute_rvs(
    strategy: dict,
    per_fold_sharpes: tuple[float, ...],
    has_baseline: bool,
    coverage: float,
) -> ResearchValidationScore:
    sharpe = float(strategy["sharpe"])
    predictive = PREDICTIVE_WEIGHT * _clamp01(sharpe / TARGET_SHARPE)

    dispersion = _stdev(per_fold_sharpes)
    stability = STABILITY_WEIGHT * _clamp01(1.0 - dispersion)

    turnover = float(strategy["annual_turnover"])
    utility = UTILITY_WEIGHT * _clamp01(sharpe / TARGET_SHARPE) * _clamp01(1.0 - turnover / 6.0)

    data_quality = DATA_QUALITY_WEIGHT * _clamp01(coverage)

    diversification: float | None
    if has_baseline:
        counts = strategy["overlap_counts"]
        total = sum(counts.values()) or 1
        research_only_share = counts.get("research_only", 0) / total
        diversification = DIVERSIFICATION_WEIGHT * _clamp01(research_only_share)
    else:
        diversification = None

    components: dict[str, float | None] = {
        "walk_forward_predictive_validity": predictive,
        "stability": stability,
        "paper_model_concordance": None,
        "risk_adjusted_utility": utility,
        "diversification": diversification,
        "data_quality_liquidity_feasibility": data_quality,
    }

    weights = {
        "walk_forward_predictive_validity": PREDICTIVE_WEIGHT,
        "stability": STABILITY_WEIGHT,
        "paper_model_concordance": CONCORDANCE_WEIGHT,
        "risk_adjusted_utility": UTILITY_WEIGHT,
        "diversification": DIVERSIFICATION_WEIGHT,
        "data_quality_liquidity_feasibility": DATA_QUALITY_WEIGHT,
    }
    available_weight = float(sum(weights[k] for k, v in components.items() if v is not None))
    offline_subtotal = float(sum(v for v in components.values() if v is not None))

    vetoes: list[str] = []
    if not strategy["survives_multiple_testing"]:
        vetoes.append("failed_multiple_testing")
    for guardrail in strategy["guardrails"]:
        if not guardrail["passed"]:
            vetoes.append(f"guardrail:{guardrail['name']}")

    return ResearchValidationScore(
        components=components,
        offline_subtotal=offline_subtotal,
        available_weight=available_weight,
        vetoes=tuple(vetoes),
        live_eligible=False,
        live_eligible_reason="awaiting_paper_concordance",
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/research/strategy/test_rvs.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add research/strategy/rvs.py tests/research/strategy/test_rvs.py
git commit -m "feat: add offline Research Validation Score with no-renormalization subtotal"
```

---

### Task 7: Run card, CLI, boundary scanner, and smoke test

**Files:**
- Create: `research/strategy/runcard.py`
- Create: `scripts/run_strategy_search.py`
- Modify: `research/evaluation/runcard.py` (add a `prefix` param to `write_run_card` so the strategy path reuses the shared `output/` guard + write)
- Modify: `research/strategy/search.py` (attach RVS into each strategy dict)
- Modify: `tests/research/test_architecture.py` (add strategy-subpackage boundary assertion)
- Test: `tests/research/strategy/test_runcard.py`, `tests/scripts/test_run_strategy_search.py`

**Interfaces:**
- Consumes: Task 5 `search_strategies`; Task 6 `compute_rvs`.
- Produces:
  - In `search.py`, each evaluated strategy dict gains `rvs` (the `ResearchValidationScore` as a dict via `dataclasses.asdict`). Wire it by computing `coverage = len(holdings_sample_dates) / max(1, n_rebalance_dates)` where `n_rebalance_dates` counts outer-test rebalance anchors, and `has_baseline = bool(baseline_records)`.
  - `research.strategy.runcard.build_strategy_run_card(search_result: dict, git_revision: str, input_checksum: str) -> dict`.
  - `research.strategy.runcard.write_strategy_run_card(card: dict, output_dir: str) -> Path` — filename `strategy_search_<data_cutoff>.json`; refuses any path with an `output` component.
  - `scripts/run_strategy_search.py:main(argv=None) -> int`.

- [ ] **Step 1: Wire RVS into search results (write the failing test)**

Add to `tests/research/strategy/test_search.py`:

```python
def test_each_strategy_carries_an_rvs_block():
    from research.strategy.search import SearchConfig, search_sleeve
    result = search_sleeve("momentum", _bars(), None, SearchConfig())
    strategy = next(iter(result["strategies"].values()))
    assert "rvs" in strategy
    assert strategy["rvs"]["live_eligible"] is False
    assert strategy["rvs"]["components"]["paper_model_concordance"] is None
    assert strategy["rvs"]["available_weight"] in (70.0, 80.0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/research/strategy/test_search.py::test_each_strategy_carries_an_rvs_block -v`
Expected: FAIL with `KeyError: 'rvs'`.

- [ ] **Step 3: Wire RVS into `search.py`**

In `research/strategy/search.py`, add the import at the top:

```python
from dataclasses import asdict

from research.strategy.rvs import compute_rvs
```

Then, inside `search_sleeve`, in the loop that builds `strategies[strategy_id]`, after the dict is assembled add:

```python
        n_rebalance = 0
        for outer in folds:
            n_rebalance += len(range(0, max(0, outer.test[1] - outer.test[0]), config.horizon))
        coverage = len(strategies[strategy_id]["holdings_sample_dates"]) / max(1, n_rebalance)
        strategies[strategy_id]["rvs"] = asdict(
            compute_rvs(
                strategies[strategy_id],
                per_fold_sharpes=ctx["per_fold_sharpes"],
                has_baseline=bool(baseline_records),
                coverage=min(1.0, coverage),
            )
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/research/strategy/test_search.py -v`
Expected: PASS (all, including the new RVS test).

- [ ] **Step 5: Write the run card failing test**

Create `tests/research/strategy/test_runcard.py`:

```python
from __future__ import annotations

import json
from pathlib import Path

import pytest

from research.strategy.runcard import build_strategy_run_card, write_strategy_run_card


def _search_result() -> dict:
    return {
        "sleeves": {
            "momentum": {"sleeve": "momentum", "status": "evaluated", "strategies": {}},
            "sector_rotation": {"sleeve": "sector_rotation", "status": "blocked: needs sector map"},
        },
        "snapshot_identity": "sha256:snap",
        "provenance": {"data_cutoff": "2020-06-01", "code_revision": "sha256:code"},
    }


def test_run_card_embeds_provenance_and_search():
    card = build_strategy_run_card(_search_result(), git_revision="deadbeef", input_checksum="sha256:in")
    assert card["git_revision"] == "deadbeef"
    assert card["input_artifact_checksum"] == "sha256:in"
    assert card["provenance"]["data_cutoff"] == "2020-06-01"
    assert card["search"]["sleeves"]["sector_rotation"]["status"].startswith("blocked")


def test_write_refuses_output_directory(tmp_path):
    card = build_strategy_run_card(_search_result(), git_revision="x", input_checksum="y")
    with pytest.raises(ValueError):
        write_strategy_run_card(card, str(tmp_path / "output" / "cards"))


def test_write_is_byte_identical_for_identical_cards(tmp_path):
    card = build_strategy_run_card(_search_result(), git_revision="x", input_checksum="y")
    p1 = write_strategy_run_card(card, str(tmp_path / "a"))
    p2 = write_strategy_run_card(card, str(tmp_path / "b"))
    assert p1.read_text() == p2.read_text()
    assert p1.name == "strategy_search_2020-06-01.json"
```

- [ ] **Step 6: Run test to verify it fails**

Run: `pytest tests/research/strategy/test_runcard.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'research.strategy.runcard'`.

- [ ] **Step 7: Write the run card implementation (reuse the evaluation writer)**

First, parameterize the existing shared writer so the strategy path reuses its `output/` guard and byte-reproducible write instead of duplicating them. Edit `research/evaluation/runcard.py` — add a `prefix` parameter (defaulted so existing callers and their tests are unaffected):

```python
def write_run_card(card: dict, output_dir: str, prefix: str = "factor_evaluation") -> Path:
    directory = Path(output_dir)
    if "output" in directory.parts:
        raise ValueError("run cards must not be written under output/")
    directory.mkdir(parents=True, exist_ok=True)
    cutoff = card["provenance"]["data_cutoff"]
    path = directory / f"{prefix}_{cutoff}.json"
    path.write_text(json.dumps(card, sort_keys=True, indent=2))
    return path
```

Then create `research/strategy/runcard.py` as a thin layer. `build_strategy_run_card` stays (its card shape legitimately differs — it embeds `search`, and `search_result` has no top-level `config`), but the writer delegates to the shared one. Importing `research.evaluation.runcard` is permitted by the boundary scanner (`FORBIDDEN_DEPENDENCY_PREFIXES` covers `backtest/services/scripts/redis/importlib/...`, not `research.evaluation`).

```python
from __future__ import annotations

from pathlib import Path

from research.evaluation.runcard import write_run_card

_FILENAME_PREFIX = "strategy_search"


def build_strategy_run_card(search_result: dict, git_revision: str, input_checksum: str) -> dict:
    return {
        "git_revision": git_revision,
        "input_artifact_checksum": input_checksum,
        "snapshot_identity": search_result["snapshot_identity"],
        "provenance": search_result["provenance"],
        "search": search_result,
    }


def write_strategy_run_card(card: dict, output_dir: str) -> Path:
    return write_run_card(card, output_dir, prefix=_FILENAME_PREFIX)
```

The Step 5 test (`test_runcard.py`) is unchanged: `write_strategy_run_card` still exists, still yields `strategy_search_<cutoff>.json`, still raises `ValueError` on an `output/` component (now from the shared guard), and stays byte-identical (same `json.dumps(sort_keys=True, indent=2)`).

- [ ] **Step 8: Run test to verify it passes**

Run: `pytest tests/research/strategy/test_runcard.py -v`
Expected: PASS (3 passed).

- [ ] **Step 9: Extend the architecture boundary scanner (write the failing test)**

In `tests/research/test_architecture.py`, add this test after `test_evaluation_subpackage_is_scanned_by_boundary`:

```python
def test_strategy_subpackage_is_scanned_by_boundary():
    from pathlib import Path

    scanned = {str(p) for p in Path("research").rglob("*.py")}
    assert any("research/strategy/" in p for p in scanned)
```

- [ ] **Step 10: Run to verify the boundary tests pass**

Run: `pytest tests/research/test_architecture.py -v`
Expected: PASS — the existing `test_research_package_has_no_prohibited_static_imports_or_loaders` already `rglob`s `research/`, so it now also scans `research/strategy/`; the new test asserts strategy modules are present. If the prohibited-imports test FAILS, a strategy module imported a forbidden surface — fix the import, do not weaken the test.

- [ ] **Step 11: Write the CLI (write the failing smoke test)**

Create `tests/scripts/test_run_strategy_search.py`:

```python
from __future__ import annotations

import json
from pathlib import Path

import pytest

CANONICAL = Path("output/backtest_multi_20260710_005841.json")


@pytest.mark.skipif(not CANONICAL.exists(), reason="canonical frozen artifact not present")
def test_cli_writes_one_run_card_with_evaluable_and_blocked_sleeves(tmp_path):
    from scripts.run_strategy_search import main

    out = tmp_path / "cards"
    code = main([
        "--bars-from-json", str(CANONICAL),
        "--sleeve", "momentum",
        "--sleeve", "sector_rotation",
        "--output-dir", str(out),
        "--outer-folds", "3",
        "--inner-folds", "2",
    ])
    assert code == 0
    cards = list(out.glob("strategy_search_*.json"))
    assert len(cards) == 1
    card = json.loads(cards[0].read_text())
    sleeves = card["search"]["sleeves"]
    assert sleeves["momentum"]["status"] == "evaluated"
    assert sleeves["sector_rotation"]["status"].startswith("blocked")


def test_cli_refuses_output_directory():
    from scripts.run_strategy_search import main

    with pytest.raises(ValueError):
        main([
            "--bars-from-json", "does-not-matter.json",
            "--output-dir", "output/cards",
        ])
```

- [ ] **Step 12: Run test to verify it fails**

Run: `pytest tests/scripts/test_run_strategy_search.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'scripts.run_strategy_search'`.

- [ ] **Step 13: Write the CLI**

Create `scripts/run_strategy_search.py`:

```python
# scripts/run_strategy_search.py
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from research.strategy.runcard import build_strategy_run_card, write_strategy_run_card
from research.strategy.search import SearchConfig, search_strategies
from research.strategy.spec import BLOCKED_SLEEVES, EVALUABLE_SLEEVES


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


def _load_shadow(path: str | None) -> list[dict] | None:
    if not path:
        return None
    return json.loads(Path(path).read_text())


def _git_revision() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    except Exception:
        return "unknown"


def _checksum(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline research strategy search run card generator")
    parser.add_argument("--bars-from-json", required=True)
    parser.add_argument("--sleeve", action="append", default=None)
    parser.add_argument("--shadow-from-json", default=None)
    parser.add_argument("--horizon", type=int, default=21)
    parser.add_argument("--outer-folds", type=int, default=4)
    parser.add_argument("--inner-folds", type=int, default=3)
    parser.add_argument("--embargo", type=int, default=None)
    parser.add_argument("--thresholds", type=float, nargs="+", default=[0.2, 0.3])
    parser.add_argument("--fdr-q", type=float, default=0.10)
    parser.add_argument("--min-names", type=int, default=5)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)

    # Refuse output/ before doing any work.
    if "output" in Path(args.output_dir).parts:
        raise ValueError("run cards must not be written under output/")

    sleeves = tuple(args.sleeve) if args.sleeve else tuple(EVALUABLE_SLEEVES) + tuple(BLOCKED_SLEEVES)
    config = SearchConfig(
        horizon=args.horizon,
        n_outer=args.outer_folds,
        n_inner=args.inner_folds,
        embargo=args.embargo if args.embargo is not None else args.horizon,
        thresholds=tuple(args.thresholds),
        fdr_q=args.fdr_q,
        min_names=args.min_names,
        seed=args.seed,
    )

    bars = _load_bars(args.bars_from_json)
    baseline = _load_shadow(args.shadow_from_json)
    result = search_strategies(bars, sleeves, baseline, config)
    card = build_strategy_run_card(
        result, git_revision=_git_revision(), input_checksum=f"sha256:{_checksum(args.bars_from_json)}"
    )
    path = write_strategy_run_card(card, args.output_dir)
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 14: Run the smoke test**

Run: `pytest tests/scripts/test_run_strategy_search.py -v`
Expected: PASS — `test_cli_refuses_output_directory` passes unconditionally; `test_cli_writes_one_run_card...` passes if the canonical artifact exists, otherwise it is skipped. If skipped, additionally run a manual smoke against any available `output/backtest_multi_*.json`:

Run: `python scripts/run_strategy_search.py --bars-from-json $(ls -t output/backtest_multi_*.json | head -1) --sleeve momentum --sleeve sector_rotation --output-dir "$TMPDIR/strategy_cards" --outer-folds 3 --inner-folds 2`
Expected: prints `wrote .../strategy_search_<cutoff>.json`, exit 0.

- [ ] **Step 15: Full-suite check and commit**

Run: `pytest tests/research/ tests/scripts/ -q`
Expected: all green.

Run: `python -m compileall research/strategy scripts/run_strategy_search.py`
Expected: no errors.

```bash
git add research/strategy/runcard.py research/evaluation/runcard.py research/strategy/search.py scripts/run_strategy_search.py tests/research/test_architecture.py tests/research/strategy/test_runcard.py tests/research/strategy/test_search.py tests/scripts/test_run_strategy_search.py
git commit -m "feat: add strategy-search run card, CLI, and boundary coverage"
```

---

## Self-Review

**Spec coverage:**
- §4 boundary / architecture scanner → Task 7 Steps 9–10. ✓
- §5.1 normalized-frame ranking → Task 1 (`combine.py` z-scores before weighting; the P3 evaluator's individual-factor raw ranking is intentionally left untouched — see deviation note below). ✓
- §5.2 strategy spec + hashing + family compat → Task 2. ✓
- §5.3 objectives/guardrails, 2 real + 4 blocked → Task 3. ✓
- §5.4 deterministic inner-fold optimizer → Task 4. ✓
- §5.5 search orchestration + `n_trials=#specs` → Task 5. ✓
- §5.6 RVS, concordance `unavailable`, subtotal /80, no renormalization, `live_eligible=false` → Task 6. ✓
- §5.7 run card (immutable, refuses `output/`, byte-reproducible) → Task 7 Steps 5–8. ✓
- §5.8 CLI → Task 7 Steps 11–14. ✓
- §8 tests: combine invariance (T1), spec/hash (T2), objectives/guardrails/blocked (T3), optimizer determinism + outer-fold-never-seen (T4), n_trials (T5), RVS composition + no-renorm (T6), blocked-status (T5), boundary (T7), byte-identical card (T7), CLI smoke (T7). ✓

**Deviation from spec (flagged for the user):** Spec §5.1 said the change "removes the raw-vs-normalized assumption note currently in `research/evaluation/evaluator.py`." In implementation the z-scoring lives in the new `research/strategy/combine.py` combination path; the P3 evaluator still evaluates *individual* factors where raw ranking is correct (rank-invariant for policy=`none` factors), so its note remains accurate and is intentionally left in place. Net effect on P4 correctness: none — blends are z-scored where they are formed.

**Placeholder scan:** No TBD/TODO; every code step shows complete code; every test step shows real assertions. ✓

**Type consistency:** `PortfolioSeries(returns, turnover, ic)`, `WeightFit(weights, threshold, score)`, `FittedStrategy.strategy_hash`, `MultipleTestingVerdict.{deflated_sharpe,survives}`, `OverlapReport.{counts,cohort_returns}`, `GuardrailResult.{name,passed,reason}`, and `ResearchValidationScore` field names are used consistently across Tasks 1–7. `objective_for`/`guardrails_for`/`is_blocked` names match between Task 3 and Task 5. ✓
