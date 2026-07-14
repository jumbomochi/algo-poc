# Native Factor Shadow Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the native factor contracts, point-in-time panel, initial price-factor catalog, and failure-isolated shadow scoring across all six sleeves without changing any trading decision.

**Architecture:** A new dependency-light `research` package computes causal factor panels and exposes immutable factor snapshots. Optional observers attach those snapshots to raw buy candidates in the backtest and paper runners, record the subsequent risk decision, and never alter signals, sizing, risk, Redis messages, or IB execution. Backtests retain shadow records in their result artifacts; paper runs persist them through an independent SQLAlchemy session.

**Tech Stack:** Python 3.12, dataclasses, typing protocols, pandas, NumPy, SQLAlchemy 2, Alembic, pytest

## Global Constraints

- This plan implements delivery phases 1 and 2 only: native factor foundations and shadow scoring.
- Research remains observational: no research recommendation publishing, no new sleeve allocation, and no order-path changes.
- `research/` must not import `services.execution`, `services.risk_management`, `ib_insync`, or any Interactive Brokers client.
- Observer failures must never change an established sleeve's signals, risk decisions, fills, or published recommendations.
- Factor values at date `t` may use only observations effective on or before `t`.
- Factors are explicit reviewed Python classes; arbitrary generated Python and dynamic module loading are excluded.
- Initial factors are price-only so the first deployment can be proven against the existing frozen bar artifacts. Point-in-time fundamental projection is included in the panel contract for later factor plans.
- No new third-party runtime dependency is added in this plan.
- The canonical design is `docs/superpowers/specs/2026-07-14-native-factor-research-design.md`.

## Scope Decomposition

The approved design is a multi-phase program. This plan produces the first independently deployable deliverable. Subsequent plans will cover:

1. Nested walk-forward validation, multiple-testing control, overlap attribution, and run cards.
2. Declarative strategy search, Research Validation Score, and Candidate Conviction Score.
3. Automated paper research sleeve and three-month promotion gate.
4. Bounded 2% live research sleeve, automatic suspension, and decay monitoring.

No later phase may begin by bypassing the shadow data and causality tests created here.

## File Map

**New package files**

- `research/__init__.py` — public research package marker.
- `research/factors/__init__.py` — exports the factor contracts and default registry.
- `research/factors/contracts.py` — immutable `FactorSpec`, `FactorPanel`, and `Factor` protocol.
- `research/factors/operations.py` — causal transforms shared by reviewed factors.
- `research/factors/panel.py` — point-in-time price and fundamental panel construction.
- `research/factors/registry.py` — explicit factor registry with duplicate/version validation.
- `research/factors/catalog.py` — first four reviewed price factors and default registry builder.
- `research/factors/engine.py` — factor computation, normalization, and snapshot lookup.
- `research/shadow.py` — candidate observer protocol, records, in-memory recorder, and SQL recorder.
- `shared/models/research.py` — operational paper-shadow candidate model.
- `migrations/versions/9b3d1c7e4a20_add_research_candidates.py` — additive schema migration.

**Modified files**

- `pyproject.toml` — package `research` in the wheel.
- `shared/models/__init__.py` — import/export the research candidate model.
- `shared/config.py` — add disabled-by-default shadow research configuration.
- `config/default.yaml` — document disabled-by-default shadow settings.
- `backtest/runner.py` — optional failure-isolated candidate observer and result records.
- `scripts/run_backtest.py` — opt-in `--research-shadow` wiring and saved artifacts.
- `scripts/run_paper.py` — opt-in shadow recorder using an independent DB session.

**New tests**

- `tests/research/test_contracts.py`
- `tests/research/test_operations.py`
- `tests/research/test_panel.py`
- `tests/research/test_registry.py`
- `tests/research/test_catalog.py`
- `tests/research/test_engine.py`
- `tests/research/test_shadow.py`
- `tests/research/test_architecture.py`
- `tests/backtest/test_research_shadow.py`
- `tests/scripts/test_run_paper_research_shadow.py`

---

### Task 1: Factor Contracts and Explicit Registry

**Files:**
- Create: `research/__init__.py`
- Create: `research/factors/__init__.py`
- Create: `research/factors/contracts.py`
- Create: `research/factors/registry.py`
- Modify: `pyproject.toml`
- Test: `tests/research/test_contracts.py`
- Test: `tests/research/test_registry.py`

**Interfaces:**
- Produces: `FactorSpec`, `FactorPanel`, `Factor`, `FactorRegistry`.
- `Factor.compute(panel: FactorPanel) -> pd.DataFrame` returns dates as index and tickers as columns.
- `FactorRegistry.register(factor: Factor) -> None`, `get(factor_id: str) -> Factor`, and `list_specs() -> tuple[FactorSpec, ...]`.

- [ ] **Step 1: Write the failing contract tests**

```python
# tests/research/test_contracts.py
from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from research.factors.contracts import FactorPanel, FactorSpec


def test_factor_spec_requires_semantic_identity_and_positive_lookback():
    spec = FactorSpec(
        factor_id="price_momentum_126d",
        version="1.0.0",
        family="momentum",
        description="126-day total return",
        required_fields=("close",),
        supported_sleeves=("momentum",),
        lookback_days=126,
        direction=1,
        source="Jegadeesh and Titman",
        license="formula",
    )
    assert spec.key == "price_momentum_126d@1.0.0"

    with pytest.raises(ValueError, match="lookback_days"):
        FactorSpec(**{**spec.__dict__, "lookback_days": 0})


def test_factor_panel_rejects_misaligned_fields():
    close = pd.DataFrame({"AAPL": [100.0]}, index=pd.to_datetime(["2026-01-02"]))
    volume = pd.DataFrame({"MSFT": [10]}, index=close.index)
    with pytest.raises(ValueError, match="same index and columns"):
        FactorPanel(fields={"close": close, "volume": volume}, as_of=date(2026, 1, 2))
```

```python
# tests/research/test_registry.py
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
import pytest

from research.factors.contracts import FactorPanel, FactorSpec
from research.factors.registry import FactorRegistry


@dataclass(frozen=True)
class DummyFactor:
    spec: FactorSpec

    def compute(self, panel: FactorPanel) -> pd.DataFrame:
        return panel.field("close")


def make_factor(factor_id="dummy", version="1.0.0"):
    return DummyFactor(FactorSpec(
        factor_id=factor_id,
        version=version,
        family="test",
        description="test factor",
        required_fields=("close",),
        supported_sleeves=("momentum",),
        lookback_days=1,
        direction=1,
        source="test fixture",
        license="test",
    ))


def test_registry_is_explicit_and_rejects_duplicate_factor_ids():
    registry = FactorRegistry()
    registry.register(make_factor())
    assert registry.get("dummy").spec.version == "1.0.0"
    with pytest.raises(ValueError, match="already registered"):
        registry.register(make_factor(version="2.0.0"))


def test_registry_get_unknown_factor_fails_loudly():
    with pytest.raises(KeyError, match="unknown factor"):
        FactorRegistry().get("missing")
```

- [ ] **Step 2: Run the tests to verify the package does not exist**

Run: `pytest tests/research/test_contracts.py tests/research/test_registry.py -v`

Expected: collection fails with `ModuleNotFoundError: No module named 'research'`.

- [ ] **Step 3: Implement the immutable contracts**

```python
# research/factors/contracts.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Mapping, Protocol, runtime_checkable

import pandas as pd


@dataclass(frozen=True)
class FactorSpec:
    factor_id: str
    version: str
    family: str
    description: str
    required_fields: tuple[str, ...]
    supported_sleeves: tuple[str, ...]
    lookback_days: int
    direction: int
    source: str
    license: str

    def __post_init__(self) -> None:
        if not self.factor_id or "@" in self.factor_id:
            raise ValueError("factor_id must be non-empty and cannot contain '@'")
        if not self.version:
            raise ValueError("version must be non-empty")
        if self.lookback_days < 1:
            raise ValueError("lookback_days must be at least 1")
        if self.direction not in (-1, 1):
            raise ValueError("direction must be -1 or 1")
        if not self.required_fields:
            raise ValueError("required_fields must be non-empty")

    @property
    def key(self) -> str:
        return f"{self.factor_id}@{self.version}"


@dataclass(frozen=True)
class FactorPanel:
    fields: Mapping[str, pd.DataFrame]
    as_of: date

    def __post_init__(self) -> None:
        frames = list(self.fields.values())
        if not frames:
            raise ValueError("fields must be non-empty")
        first = frames[0]
        for frame in frames[1:]:
            if not frame.index.equals(first.index) or not frame.columns.equals(first.columns):
                raise ValueError("all factor fields must have the same index and columns")
        if len(first.index) and first.index.max().date() > self.as_of:
            raise ValueError("panel contains observations after as_of")

    def field(self, name: str) -> pd.DataFrame:
        try:
            return self.fields[name]
        except KeyError as exc:
            raise KeyError(f"factor panel is missing required field '{name}'") from exc


@runtime_checkable
class Factor(Protocol):
    spec: FactorSpec

    def compute(self, panel: FactorPanel) -> pd.DataFrame: ...
```

- [ ] **Step 4: Implement the explicit registry and package exports**

```python
# research/factors/registry.py
from __future__ import annotations

from research.factors.contracts import Factor, FactorSpec


class FactorRegistry:
    def __init__(self) -> None:
        self._factors: dict[str, Factor] = {}

    def register(self, factor: Factor) -> None:
        factor_id = factor.spec.factor_id
        if factor_id in self._factors:
            raise ValueError(f"factor '{factor_id}' is already registered")
        self._factors[factor_id] = factor

    def get(self, factor_id: str) -> Factor:
        try:
            return self._factors[factor_id]
        except KeyError as exc:
            raise KeyError(f"unknown factor '{factor_id}'") from exc

    def list_specs(self) -> tuple[FactorSpec, ...]:
        return tuple(self._factors[key].spec for key in sorted(self._factors))
```

```python
# research/__init__.py
"""Offline research and shadow-scoring subsystem."""

# research/factors/__init__.py
from research.factors.contracts import Factor, FactorPanel, FactorSpec
from research.factors.registry import FactorRegistry

__all__ = ["Factor", "FactorPanel", "FactorSpec", "FactorRegistry"]
```

Modify the Hatch wheel package list:

```toml
[tool.hatch.build.targets.wheel]
packages = ["shared", "services", "backtest", "research"]
```

- [ ] **Step 5: Run focused tests**

Run: `pytest tests/research/test_contracts.py tests/research/test_registry.py -v`

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml research/__init__.py research/factors/__init__.py research/factors/contracts.py research/factors/registry.py tests/research/test_contracts.py tests/research/test_registry.py
git commit -m "feat: add native factor contracts and registry"
```

---

### Task 2: Causal Operations and Point-in-Time Panel Builder

**Files:**
- Create: `research/factors/operations.py`
- Create: `research/factors/panel.py`
- Test: `tests/research/test_operations.py`
- Test: `tests/research/test_panel.py`

**Interfaces:**
- Consumes: `FactorPanel` from Task 1.
- Produces: `cross_sectional_rank`, `trailing_return`, `rolling_volatility`, `rolling_dollar_volume`, `build_factor_panel`.
- `build_factor_panel(bars_by_ticker, fundamentals_by_ticker=None, as_of=None) -> FactorPanel`.

- [ ] **Step 1: Write causality and alignment tests**

```python
# tests/research/test_operations.py
from __future__ import annotations

import pandas as pd

from research.factors.operations import cross_sectional_rank, trailing_return


def test_cross_sectional_rank_is_per_date_and_bounded():
    frame = pd.DataFrame(
        {"A": [1.0, 3.0], "B": [2.0, 1.0]},
        index=pd.to_datetime(["2026-01-02", "2026-01-05"]),
    )
    ranked = cross_sectional_rank(frame)
    assert ranked.loc["2026-01-02", "B"] == 1.0
    assert ranked.loc["2026-01-05", "A"] == 1.0
    assert ranked.min().min() >= 0.0


def test_trailing_return_does_not_read_future_rows():
    original = pd.DataFrame({"A": [100.0, 110.0, 121.0]}, index=pd.date_range("2026-01-01", periods=3))
    mutated = original.copy()
    mutated.loc[mutated.index[-1], "A"] = 9999.0
    before = trailing_return(original, periods=1)
    after = trailing_return(mutated, periods=1)
    pd.testing.assert_series_equal(before.iloc[:2, 0], after.iloc[:2, 0])
```

```python
# tests/research/test_panel.py
from __future__ import annotations

from datetime import date

import pandas as pd

from research.factors.panel import build_factor_panel


def test_panel_aligns_tickers_and_clips_after_as_of():
    bars = {
        "A": [
            {"date": date(2026, 1, 2), "open": 10, "high": 11, "low": 9, "close": 10, "volume": 100},
            {"date": date(2026, 1, 5), "open": 11, "high": 12, "low": 10, "close": 11, "volume": 110},
        ],
        "B": [{"date": date(2026, 1, 5), "open": 20, "high": 21, "low": 19, "close": 20, "volume": 200}],
    }
    panel = build_factor_panel(bars, as_of=date(2026, 1, 2))
    assert list(panel.field("close").columns) == ["A", "B"]
    assert list(panel.field("close").index.date) == [date(2026, 1, 2)]
    assert pd.isna(panel.field("close").loc["2026-01-02", "B"])


def test_fundamentals_appear_only_on_or_after_effective_date():
    bars = {"A": [
        {"date": date(2026, 1, 2), "open": 10, "high": 10, "low": 10, "close": 10, "volume": 100},
        {"date": date(2026, 1, 5), "open": 10, "high": 10, "low": 10, "close": 10, "volume": 100},
        {"date": date(2026, 1, 6), "open": 10, "high": 10, "low": 10, "close": 10, "volume": 100},
    ]}
    fundamentals = {"A": [{"effective_at": "2026-01-05T12:00:00+00:00", "earnings_yield": 0.04}]}
    panel = build_factor_panel(bars, fundamentals_by_ticker=fundamentals)
    values = panel.field("fund:earnings_yield")["A"]
    assert pd.isna(values.iloc[0])
    assert pd.isna(values.iloc[1])
    assert values.iloc[2] == 0.04
```

- [ ] **Step 2: Run tests and verify missing modules**

Run: `pytest tests/research/test_operations.py tests/research/test_panel.py -v`

Expected: collection fails because `operations` and `panel` do not exist.

- [ ] **Step 3: Implement causal operations**

```python
# research/factors/operations.py
from __future__ import annotations

import numpy as np
import pandas as pd


def cross_sectional_rank(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.rank(axis=1, method="average", pct=True)


def trailing_return(close: pd.DataFrame, periods: int) -> pd.DataFrame:
    if periods < 1:
        raise ValueError("periods must be at least 1")
    return close / close.shift(periods) - 1.0


def rolling_volatility(close: pd.DataFrame, periods: int) -> pd.DataFrame:
    returns = close.pct_change(fill_method=None)
    return returns.rolling(periods, min_periods=periods).std() * np.sqrt(252.0)


def rolling_dollar_volume(close: pd.DataFrame, volume: pd.DataFrame, periods: int) -> pd.DataFrame:
    return (close * volume).rolling(periods, min_periods=periods).mean()
```

- [ ] **Step 4: Implement the point-in-time panel builder**

```python
# research/factors/panel.py
from __future__ import annotations

from datetime import date
from typing import Any, Mapping

import pandas as pd

from research.factors.contracts import FactorPanel

PRICE_FIELDS = ("open", "high", "low", "close", "volume")


def build_factor_panel(
    bars_by_ticker: Mapping[str, list[dict[str, Any]]],
    fundamentals_by_ticker: Mapping[str, list[dict[str, Any]]] | None = None,
    as_of: date | None = None,
) -> FactorPanel:
    tickers = sorted(bars_by_ticker)
    dates = sorted({pd.Timestamp(bar["date"]).date() for bars in bars_by_ticker.values() for bar in bars})
    cutoff = as_of or (dates[-1] if dates else date.min)
    index = pd.DatetimeIndex([day for day in dates if day <= cutoff])
    fields = {name: pd.DataFrame(index=index, columns=tickers, dtype=float) for name in PRICE_FIELDS}

    for ticker, bars in bars_by_ticker.items():
        for bar in bars:
            timestamp = pd.Timestamp(bar["date"])
            if timestamp.date() > cutoff or timestamp not in index:
                continue
            for field in PRICE_FIELDS:
                fields[field].at[timestamp, ticker] = float(bar[field])

    metric_names = sorted({
        key
        for rows in (fundamentals_by_ticker or {}).values()
        for row in rows
        for key in row
        if key not in {"effective_at", "ingested_at", "source_revision", "report_date"}
        and isinstance(row[key], (int, float))
    })
    for metric in metric_names:
        frame = pd.DataFrame(index=index, columns=tickers, dtype=float)
        for ticker, rows in (fundamentals_by_ticker or {}).items():
            for row in sorted(rows, key=lambda item: pd.Timestamp(item["effective_at"])):
                effective_day = pd.Timestamp(row["effective_at"]).date()
                if effective_day <= cutoff and metric in row:
                    # Conservative daily-bar policy: a filing becomes usable on
                    # the first trading date strictly after its effective date.
                    # This prevents an after-close filing from contaminating
                    # the same day's close-based signal.
                    frame.loc[frame.index.date > effective_day, ticker] = float(row[metric])
        fields[f"fund:{metric}"] = frame

    return FactorPanel(fields=fields, as_of=cutoff)
```

- [ ] **Step 5: Run focused tests**

Run: `pytest tests/research/test_operations.py tests/research/test_panel.py -v`

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add research/factors/operations.py research/factors/panel.py tests/research/test_operations.py tests/research/test_panel.py
git commit -m "feat: add causal factor panel construction"
```

---

### Task 3: Initial Reviewed Price-Factor Catalog and Snapshot Engine

**Files:**
- Create: `research/factors/catalog.py`
- Create: `research/factors/engine.py`
- Modify: `research/factors/__init__.py`
- Test: `tests/research/test_catalog.py`
- Test: `tests/research/test_engine.py`

**Interfaces:**
- Consumes: factor contracts, registry, operations, and panel.
- Produces: `DEFAULT_FACTOR_IDS`, `build_default_registry()`, `FactorEngine.compute(panel, factor_ids) -> FactorSnapshotIndex`.
- `FactorSnapshotIndex.values_for(as_of: date, ticker: str) -> dict[str, float]` returns versioned factor keys.

- [ ] **Step 1: Write catalog formula and future-mutation tests**

```python
# tests/research/test_catalog.py
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from research.factors.catalog import build_default_registry
from research.factors.panel import build_factor_panel


def make_bars(days=260):
    start = date(2025, 1, 1)
    return {"A": [
        {"date": start + timedelta(days=i), "open": 100 + i, "high": 101 + i,
         "low": 99 + i, "close": 100 + i, "volume": 1_000 + i}
        for i in range(days)
    ]}


def test_default_catalog_contains_four_reviewed_price_factors():
    ids = {spec.factor_id for spec in build_default_registry().list_specs()}
    assert ids == {"price_momentum_126d", "high_52w", "low_volatility_63d", "liquidity_20d"}


def test_catalog_outputs_before_t_are_unchanged_when_future_prices_mutate():
    bars = make_bars()
    panel_before = build_factor_panel(bars)
    mutated = make_bars()
    mutated["A"][-1]["close"] = 999_999.0
    panel_after = build_factor_panel(mutated)
    factor = build_default_registry().get("price_momentum_126d")
    before = factor.compute(panel_before)
    after = factor.compute(panel_after)
    pd.testing.assert_series_equal(before.iloc[:-1, 0], after.iloc[:-1, 0])
```

```python
# tests/research/test_engine.py
from __future__ import annotations

from datetime import date, timedelta

from research.factors.catalog import DEFAULT_FACTOR_IDS, build_default_registry
from research.factors.engine import FactorEngine
from research.factors.panel import build_factor_panel


def test_engine_returns_versioned_finite_snapshot_values():
    start = date(2025, 1, 1)
    bars = {ticker: [
        {"date": start + timedelta(days=i), "open": base + i, "high": base + i + 1,
         "low": base + i - 1, "close": base + i, "volume": 1_000 + i}
        for i in range(260)
    ] for ticker, base in {"A": 100.0, "B": 200.0}.items()}
    panel = build_factor_panel(bars)
    snapshots = FactorEngine(build_default_registry()).compute(panel, DEFAULT_FACTOR_IDS)
    values = snapshots.values_for(panel.as_of, "A")
    assert set(values) == {f"{factor_id}@1.0.0" for factor_id in DEFAULT_FACTOR_IDS}
    assert all(isinstance(value, float) for value in values.values())


def test_unknown_date_or_ticker_returns_empty_snapshot():
    index = FactorEngine(build_default_registry()).compute(build_factor_panel({"A": []}, as_of=date.min), [])
    assert index.values_for(date(2026, 1, 1), "MISSING") == {}
```

- [ ] **Step 2: Run tests and verify missing catalog/engine**

Run: `pytest tests/research/test_catalog.py tests/research/test_engine.py -v`

Expected: collection fails because the modules do not exist.

- [ ] **Step 3: Implement the reviewed catalog**

```python
# research/factors/catalog.py
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from research.factors.contracts import FactorPanel, FactorSpec
from research.factors.operations import rolling_dollar_volume, rolling_volatility, trailing_return
from research.factors.registry import FactorRegistry

ALL_SLEEVES = ("momentum", "earnings_drift", "sector_rotation", "quality_value", "thematic_momentum", "tail_risk_hedge")
DEFAULT_FACTOR_IDS = ("price_momentum_126d", "high_52w", "low_volatility_63d", "liquidity_20d")


@dataclass(frozen=True)
class PriceMomentum126d:
    spec: FactorSpec = FactorSpec("price_momentum_126d", "1.0.0", "momentum", "126-day total return", ("close",), ALL_SLEEVES, 126, 1, "Jegadeesh-Titman momentum", "formula")

    def compute(self, panel: FactorPanel) -> pd.DataFrame:
        return trailing_return(panel.field("close"), 126)


@dataclass(frozen=True)
class High52Week:
    spec: FactorSpec = FactorSpec("high_52w", "1.0.0", "momentum", "Distance to trailing 252-day high", ("close",), ALL_SLEEVES, 252, 1, "George-Hwang 52-week high", "formula")

    def compute(self, panel: FactorPanel) -> pd.DataFrame:
        close = panel.field("close")
        return close / close.rolling(252, min_periods=252).max() - 1.0


@dataclass(frozen=True)
class LowVolatility63d:
    spec: FactorSpec = FactorSpec("low_volatility_63d", "1.0.0", "risk", "Negative 63-day annualized volatility", ("close",), ALL_SLEEVES, 63, 1, "low-volatility anomaly", "formula")

    def compute(self, panel: FactorPanel) -> pd.DataFrame:
        return -rolling_volatility(panel.field("close"), 63)


@dataclass(frozen=True)
class Liquidity20d:
    spec: FactorSpec = FactorSpec("liquidity_20d", "1.0.0", "liquidity", "Log 20-day average dollar volume", ("close", "volume"), ALL_SLEEVES, 20, 1, "execution-capacity control", "formula")

    def compute(self, panel: FactorPanel) -> pd.DataFrame:
        value = rolling_dollar_volume(panel.field("close"), panel.field("volume"), 20)
        return np.log(value.where(value > 0))


def build_default_registry() -> FactorRegistry:
    registry = FactorRegistry()
    for factor in (PriceMomentum126d(), High52Week(), LowVolatility63d(), Liquidity20d()):
        registry.register(factor)
    return registry
```

- [ ] **Step 4: Implement engine validation and snapshots**

```python
# research/factors/engine.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable

import numpy as np
import pandas as pd

from research.factors.contracts import FactorPanel
from research.factors.registry import FactorRegistry


@dataclass(frozen=True)
class FactorSnapshotIndex:
    frames: dict[str, pd.DataFrame]

    def values_for(self, as_of: date, ticker: str) -> dict[str, float]:
        timestamp = pd.Timestamp(as_of)
        values: dict[str, float] = {}
        for key, frame in self.frames.items():
            if timestamp not in frame.index or ticker not in frame.columns:
                continue
            value = frame.at[timestamp, ticker]
            if pd.notna(value) and np.isfinite(float(value)):
                values[key] = float(value)
        return values


class FactorEngine:
    def __init__(self, registry: FactorRegistry) -> None:
        self._registry = registry

    def compute(self, panel: FactorPanel, factor_ids: Iterable[str]) -> FactorSnapshotIndex:
        frames: dict[str, pd.DataFrame] = {}
        for factor_id in factor_ids:
            factor = self._registry.get(factor_id)
            for field in factor.spec.required_fields:
                panel.field(field)
            output = factor.compute(panel)
            reference = panel.field(factor.spec.required_fields[0])
            if not output.index.equals(reference.index) or not output.columns.equals(reference.columns):
                raise ValueError(f"factor '{factor_id}' returned a misaligned frame")
            frames[factor.spec.key] = output.astype(float)
        return FactorSnapshotIndex(frames=frames)
```

Export `DEFAULT_FACTOR_IDS`, `build_default_registry`, `FactorEngine`, and `FactorSnapshotIndex` from `research/factors/__init__.py`.

- [ ] **Step 5: Run all factor tests**

Run: `pytest tests/research/test_contracts.py tests/research/test_operations.py tests/research/test_panel.py tests/research/test_registry.py tests/research/test_catalog.py tests/research/test_engine.py -v`

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add research/factors tests/research/test_catalog.py tests/research/test_engine.py
git commit -m "feat: add initial causal factor catalog"
```

---

### Task 4: Failure-Isolated In-Memory Shadow Recorder

**Files:**
- Create: `research/shadow.py`
- Modify: `backtest/runner.py`
- Test: `tests/research/test_shadow.py`
- Test: `tests/backtest/test_research_shadow.py`

**Interfaces:**
- Consumes: `FactorSnapshotIndex`.
- Produces: `ShadowCandidateRecord`, `CandidateObserver`, `InMemoryShadowRecorder`.
- Adds optional keyword-only `candidate_observer: CandidateObserver | None = None` and `portfolio_name: str = ""` to `BacktestRunner.run`.
- Adds `shadow_candidates: list[dict]` to `BacktestResult`.

- [ ] **Step 1: Write recorder and backtest-hook tests**

```python
# tests/research/test_shadow.py
from __future__ import annotations

from datetime import date

import pandas as pd

from research.factors.engine import FactorSnapshotIndex
from research.shadow import InMemoryShadowRecorder


def test_in_memory_recorder_attaches_factor_snapshot_and_risk_outcome():
    snapshots = FactorSnapshotIndex({
        "momentum@1.0.0": pd.DataFrame({"AAPL": [0.2]}, index=pd.to_datetime(["2026-01-02"]))
    })
    recorder = InMemoryShadowRecorder(snapshots)
    recorder.observe(
        portfolio="momentum", ticker="AAPL", as_of=date(2026, 1, 2),
        signal={"action": "buy", "quantity": 1.0, "limit_price": 100.0},
        risk_approved=False, risk_reason="position cap",
    )
    record = recorder.records[0]
    assert record.factor_values == {"momentum@1.0.0": 0.2}
    assert record.risk_approved is False
```

```python
# tests/backtest/test_research_shadow.py
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

from backtest.runner import BacktestRunner
from backtest.simulator import SimulatedExecutor


class RecordingObserver:
    def __init__(self, raises=False):
        self.calls = []
        self.raises = raises

    def observe(self, **kwargs):
        if self.raises:
            raise RuntimeError("observer unavailable")
        self.calls.append(kwargs)


def run_with(observer):
    bars = {"AAPL": [{"date": date(2026, 1, 2), "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000}]}
    risk = MagicMock()
    risk.check_entry.return_value = MagicMock(approved=False, adjusted_quantity=0, reason="cap")
    signal_fn = lambda ticker, history: {"action": "buy", "ticker": ticker, "limit_price": 100.0, "quantity": 1.0, "sector": "Technology"}
    runner = BacktestRunner(SimulatedExecutor(slippage_bps=0, commission_per_share=0), 10_000)
    return runner.run(bars, signal_fn, risk, candidate_observer=observer, portfolio_name="momentum")


def test_backtest_observes_rejected_raw_buy_candidate():
    observer = RecordingObserver()
    result = run_with(observer)
    assert len(observer.calls) == 1
    assert observer.calls[0]["risk_approved"] is False
    assert result.trades == []


def test_observer_failure_does_not_change_backtest_result():
    baseline = run_with(None)
    with_failure = run_with(RecordingObserver(raises=True))
    assert with_failure.trades == baseline.trades
    assert with_failure.portfolio_values == baseline.portfolio_values
```

- [ ] **Step 2: Run tests and verify missing observer interface**

Run: `pytest tests/research/test_shadow.py tests/backtest/test_research_shadow.py -v`

Expected: failures for missing `research.shadow` and unexpected `candidate_observer` argument.

- [ ] **Step 3: Implement shadow records and in-memory recorder**

```python
# research/shadow.py
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any, Protocol

from research.factors.engine import FactorSnapshotIndex


@dataclass(frozen=True)
class ShadowCandidateRecord:
    candidate_key: str
    portfolio: str
    ticker: str
    as_of: date
    action: str
    raw_signal: dict[str, Any]
    factor_values: dict[str, float]
    risk_approved: bool
    risk_reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CandidateObserver(Protocol):
    def observe(self, *, portfolio: str, ticker: str, as_of: date,
                signal: dict[str, Any], risk_approved: bool, risk_reason: str) -> None: ...


def candidate_key(portfolio: str, ticker: str, as_of: date, signal: dict[str, Any]) -> str:
    payload = json.dumps([portfolio, ticker, as_of.isoformat(), signal], sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


class InMemoryShadowRecorder:
    def __init__(self, snapshots: FactorSnapshotIndex) -> None:
        self._snapshots = snapshots
        self.records: list[ShadowCandidateRecord] = []

    def observe(self, *, portfolio: str, ticker: str, as_of: date,
                signal: dict[str, Any], risk_approved: bool, risk_reason: str) -> None:
        self.records.append(ShadowCandidateRecord(
            candidate_key=candidate_key(portfolio, ticker, as_of, signal),
            portfolio=portfolio,
            ticker=ticker,
            as_of=as_of,
            action=str(signal["action"]),
            raw_signal=dict(signal),
            factor_values=self._snapshots.values_for(as_of, ticker),
            risk_approved=risk_approved,
            risk_reason=risk_reason,
        ))
```

- [ ] **Step 4: Add the optional, failure-isolated backtest observer**

In `backtest/runner.py`:

```python
import logging

from research.shadow import CandidateObserver

logger = logging.getLogger(__name__)
```

Extend `BacktestResult`:

```python
shadow_candidates: list[dict] = field(default_factory=list)
```

Extend `BacktestRunner.run` with keyword-only arguments after `trade_start_date`:

```python
*,
candidate_observer: CandidateObserver | None = None,
portfolio_name: str = "",
```

Immediately after `check_entry`, observe every raw buy and isolate failures:

```python
if candidate_observer is not None:
    try:
        candidate_observer.observe(
            portfolio=portfolio_name,
            ticker=ticker,
            as_of=current_date,
            signal=dict(signal),
            risk_approved=bool(decision.approved),
            risk_reason=str(decision.reason),
        )
    except Exception:
        logger.exception("Research shadow observer failed; trading result is unchanged")
```

Return observer records without requiring a concrete observer type:

```python
shadow_records = getattr(candidate_observer, "records", [])
shadow_candidates = [record.to_dict() for record in shadow_records]
```

Pass `shadow_candidates=shadow_candidates` into `BacktestResult`.

- [ ] **Step 5: Run focused and existing runner tests**

Run: `pytest tests/research/test_shadow.py tests/backtest/test_research_shadow.py tests/backtest/test_runner.py tests/backtest/test_runner_dates.py -v`

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add research/shadow.py backtest/runner.py tests/research/test_shadow.py tests/backtest/test_research_shadow.py
git commit -m "feat: record failure-isolated backtest shadow candidates"
```

---

### Task 5: Paper Shadow Persistence Model and Migration

**Files:**
- Create: `shared/models/research.py`
- Create: `migrations/versions/9b3d1c7e4a20_add_research_candidates.py`
- Modify: `shared/models/__init__.py`
- Modify: `research/shadow.py`
- Modify: `tests/shared/test_models.py`
- Test: `tests/research/test_shadow.py`

**Interfaces:**
- Consumes: `ShadowCandidateRecord` and `candidate_key`.
- Produces: `ResearchCandidate`, `SQLShadowRecorder(session, snapshots)`.
- `SQLShadowRecorder.observe(...)` is idempotent by `candidate_key`; it rolls back its independent session and re-raises on failure so the caller can log the isolated error.

- [ ] **Step 1: Add failing model and SQL-recorder tests**

Append to `tests/shared/test_models.py`:

```python
from shared.models.research import ResearchCandidate


def test_research_candidate_has_shadow_audit_fields():
    cols = {column.name for column in ResearchCandidate.__table__.columns}
    assert cols >= {"candidate_key", "portfolio", "ticker", "as_of", "action", "raw_signal", "factor_values", "risk_approved", "risk_reason", "created_at"}
    assert ResearchCandidate.__table__.columns["candidate_key"].unique is True
```

Append to `tests/research/test_shadow.py`:

```python
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from research.shadow import SQLShadowRecorder
from shared.models.base import Base
from shared.models.research import ResearchCandidate


def test_sql_recorder_is_idempotent():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    snapshots = FactorSnapshotIndex({})
    recorder = SQLShadowRecorder(session, snapshots)
    kwargs = dict(portfolio="momentum", ticker="AAPL", as_of=date(2026, 1, 2), signal={"action": "buy"}, risk_approved=True, risk_reason="approved")
    recorder.observe(**kwargs)
    recorder.observe(**kwargs)
    assert len(session.scalars(select(ResearchCandidate)).all()) == 1
```

- [ ] **Step 2: Run tests and verify missing model**

Run: `pytest tests/shared/test_models.py tests/research/test_shadow.py -v`

Expected: collection fails because `shared.models.research` does not exist.

- [ ] **Step 3: Implement the additive model**

```python
# shared/models/research.py
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import Boolean, Date, DateTime, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base


class ResearchCandidate(Base):
    __tablename__ = "research_candidates"

    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    portfolio: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    ticker: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    as_of: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(8), nullable=False)
    raw_signal: Mapped[dict] = mapped_column(JSON, nullable=False)
    factor_values: Mapped[dict] = mapped_column(JSON, nullable=False)
    risk_approved: Mapped[bool] = mapped_column(Boolean, nullable=False)
    risk_reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
```

Import and export `ResearchCandidate` in `shared/models/__init__.py`.

- [ ] **Step 4: Add the Alembic migration**

```python
# migrations/versions/9b3d1c7e4a20_add_research_candidates.py
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "9b3d1c7e4a20"
down_revision: Union[str, Sequence[str], None] = "1f7ead32f0fa"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "research_candidates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("candidate_key", sa.String(length=64), nullable=False),
        sa.Column("portfolio", sa.String(length=50), nullable=False),
        sa.Column("ticker", sa.String(length=10), nullable=False),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("action", sa.String(length=8), nullable=False),
        sa.Column("raw_signal", sa.JSON(), nullable=False),
        sa.Column("factor_values", sa.JSON(), nullable=False),
        sa.Column("risk_approved", sa.Boolean(), nullable=False),
        sa.Column("risk_reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("candidate_key"),
    )
    op.create_index("ix_research_candidates_portfolio", "research_candidates", ["portfolio"])
    op.create_index("ix_research_candidates_ticker", "research_candidates", ["ticker"])
    op.create_index("ix_research_candidates_as_of", "research_candidates", ["as_of"])


def downgrade() -> None:
    op.drop_index("ix_research_candidates_as_of", table_name="research_candidates")
    op.drop_index("ix_research_candidates_ticker", table_name="research_candidates")
    op.drop_index("ix_research_candidates_portfolio", table_name="research_candidates")
    op.drop_table("research_candidates")
```

- [ ] **Step 5: Implement the SQL recorder**

Append to `research/shadow.py`:

```python
from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.models.research import ResearchCandidate


class SQLShadowRecorder:
    def __init__(self, session: Session, snapshots: FactorSnapshotIndex) -> None:
        self._session = session
        self._snapshots = snapshots

    def observe(self, *, portfolio: str, ticker: str, as_of: date,
                signal: dict[str, Any], risk_approved: bool, risk_reason: str) -> None:
        try:
            key = candidate_key(portfolio, ticker, as_of, signal)
            if self._session.scalar(select(ResearchCandidate.id).where(ResearchCandidate.candidate_key == key)) is not None:
                return
            self._session.add(ResearchCandidate(
                candidate_key=key,
                portfolio=portfolio,
                ticker=ticker,
                as_of=as_of,
                action=str(signal["action"]),
                raw_signal=dict(signal),
                factor_values=self._snapshots.values_for(as_of, ticker),
                risk_approved=risk_approved,
                risk_reason=risk_reason,
            ))
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
```

The caller must supply an independent session. The paper integration in Task 6 owns rollback and closure if `observe` raises.

- [ ] **Step 6: Run model and recorder tests**

Run: `pytest tests/shared/test_models.py tests/research/test_shadow.py -v`

Expected: all tests pass.

- [ ] **Step 7: Verify migration SQL without touching a database**

Run: `alembic upgrade 9b3d1c7e4a20 --sql`

Expected: generated SQL contains `CREATE TABLE research_candidates` and no `DROP`, `DELETE`, `TRUNCATE`, or destructive update in the upgrade section. Do not run the migration against paper or live databases as part of this plan.

- [ ] **Step 8: Commit**

```bash
git add shared/models/research.py shared/models/__init__.py migrations/versions/9b3d1c7e4a20_add_research_candidates.py research/shadow.py tests/shared/test_models.py tests/research/test_shadow.py
git commit -m "feat: persist paper research shadow candidates"
```

---

### Task 6: Disabled-by-Default Configuration and Paper Runner Hook

**Files:**
- Modify: `shared/config.py`
- Modify: `config/default.yaml`
- Modify: `scripts/run_paper.py`
- Test: `tests/shared/test_config.py`
- Test: `tests/scripts/test_run_paper_research_shadow.py`

**Interfaces:**
- Consumes: default factor registry/engine, panel builder, and `SQLShadowRecorder`.
- Produces: `ResearchConfig`, optional keyword-only `candidate_observer` on `run_daily`.
- Paper CLI flag `--research-shadow` explicitly enables observation; the default remains off even if future config grows additional research settings.

- [ ] **Step 1: Write failing config and paper-isolation tests**

```python
# tests/scripts/test_run_paper_research_shadow.py
from __future__ import annotations

from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from scripts.paper_state import PaperTradingState
from scripts.run_backtest import PortfolioConfig
from scripts.run_paper import run_daily
from services.risk_management.engine import RiskEngine
from shared.models.base import Base


class Observer:
    def __init__(self, raises=False):
        self.calls = []
        self.raises = raises

    def observe(self, **kwargs):
        if self.raises:
            raise RuntimeError("research db unavailable")
        self.calls.append(kwargs)


def make_state_and_portfolio():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    state = PaperTradingState.create_new({"momentum": 10_000.0}, session)
    signal_fn = lambda ticker, bars: {"action": "buy", "limit_price": 100.0, "quantity": 1.0}
    portfolios = {"momentum": PortfolioConfig("momentum", 10_000.0, signal_fn, RiskEngine(position_entry_limit_pct=100, sector_concentration_pct=100, total_exposure_limit_pct=100))}
    bars = {"AAPL": [{"date": date.today(), "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000}]}
    return state, portfolios, bars


def test_paper_run_observes_buy_candidate():
    state, portfolios, bars = make_state_and_portfolio()
    observer = Observer()
    signals = run_daily(state, portfolios, bars, candidate_observer=observer)
    assert len(signals) == 1
    assert observer.calls[0]["portfolio"] == "momentum"


def test_paper_observer_failure_does_not_change_fill_or_signal():
    state, portfolios, bars = make_state_and_portfolio()
    signals = run_daily(state, portfolios, bars, candidate_observer=Observer(raises=True))
    assert len(signals) == 1
    assert state.get_positions("momentum")["AAPL"]["quantity"] == 1.0
```

Append to `tests/shared/test_config.py`:

```python
def test_research_shadow_is_disabled_by_default(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("mode: paper\n")
    config = load_config(str(path))
    assert config.research.shadow_enabled is False
    assert config.research.factor_ids == ["price_momentum_126d", "high_52w", "low_volatility_63d", "liquidity_20d"]
```

- [ ] **Step 2: Run tests and verify missing config/hook**

Run: `pytest tests/shared/test_config.py tests/scripts/test_run_paper_research_shadow.py -v`

Expected: failures for missing `research` config and unexpected `candidate_observer` argument.

- [ ] **Step 3: Add disabled-by-default configuration**

In `shared/config.py`:

```python
class ResearchConfig(BaseModel):
    shadow_enabled: bool = False
    factor_ids: list[str] = Field(default_factory=lambda: [
        "price_momentum_126d", "high_52w", "low_volatility_63d", "liquidity_20d",
    ])
```

Add to `AppConfig`:

```python
research: ResearchConfig = Field(default_factory=ResearchConfig)
```

Add to `config/default.yaml`:

```yaml
research:
  shadow_enabled: false  # observational only; never changes signals or orders
  factor_ids:
    - price_momentum_126d
    - high_52w
    - low_volatility_63d
    - liquidity_20d
```

- [ ] **Step 4: Add the failure-isolated paper observer hook**

Add keyword-only observer support to `run_daily`:

```python
def run_daily(
    state: PaperTradingState,
    portfolios: dict[str, PortfolioConfig],
    bars_by_ticker: dict[str, list[dict]],
    *,
    candidate_observer: CandidateObserver | None = None,
) -> list[dict]:
```

Add a date-normalization helper near `run_daily` so research snapshots use the finalized bar date rather than the wall-clock run date:

```python
def _bar_date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))
```

After `decision` is produced and before the rejection `continue`, call:

```python
if candidate_observer is not None:
    try:
        candidate_observer.observe(
            portfolio=name,
            ticker=ticker,
            as_of=_bar_date(bars[-1]["date"]),
            signal=dict(signal),
            risk_approved=bool(decision.approved),
            risk_reason=str(decision.reason),
        )
    except Exception:
        logger.exception("Research shadow observer failed; paper trading is unchanged")
```

Use the module's structured logger (`get_logger("run_paper")`) rather than `print` for this exception.

- [ ] **Step 5: Wire the opt-in CLI with an independent database session**

Add:

```python
parser.add_argument("--research-shadow", action="store_true", help="Record observational factor snapshots for raw buy candidates")
```

Before the signal transaction, build the observer only when enabled:

```python
candidate_observer = None
research_session = None
if args.research_shadow:
    from research.factors.catalog import DEFAULT_FACTOR_IDS, build_default_registry
    from research.factors.engine import FactorEngine
    from research.factors.panel import build_factor_panel
    from research.shadow import SQLShadowRecorder

    factor_ids = _config.research.factor_ids if "_config" in locals() else list(DEFAULT_FACTOR_IDS)
    snapshots = FactorEngine(build_default_registry()).compute(build_factor_panel(bars_by_ticker), factor_ids)
    research_session = make_db_session(args.db_url)
    candidate_observer = SQLShadowRecorder(research_session, snapshots)
```

Pass `candidate_observer=candidate_observer` to `run_daily`. Close `research_session` in the existing outer `finally` path after signal execution. Do not add `ResearchCandidate` to `STATE_TABLES`; paper reset does not own research audit history.

- [ ] **Step 6: Run paper/config tests**

Run: `pytest tests/shared/test_config.py tests/scripts/test_run_paper_research_shadow.py tests/scripts/test_run_paper_gate.py tests/scripts/test_run_paper_reset.py -v`

Expected: all tests pass, including reset protections.

- [ ] **Step 7: Commit**

```bash
git add shared/config.py config/default.yaml scripts/run_paper.py tests/shared/test_config.py tests/scripts/test_run_paper_research_shadow.py
git commit -m "feat: add opt-in paper factor shadow scoring"
```

---

### Task 7: Opt-In Multi-Sleeve Backtest Shadow Artifacts

**Files:**
- Modify: `scripts/run_backtest.py`
- Test: `tests/backtest/test_save_results.py`
- Test: `tests/backtest/test_multi_portfolio.py`

**Interfaces:**
- Consumes: default factor engine and `InMemoryShadowRecorder`.
- Produces: `--research-shadow` CLI flag and `shadow_candidates` under each portfolio in saved multi-portfolio JSON.
- All six existing portfolios receive the same immutable snapshot index and separate recorders.

- [ ] **Step 1: Write failing artifact tests**

Append to `tests/backtest/test_save_results.py`:

```python
def test_multi_portfolio_results_include_shadow_candidates(tmp_path):
    result = BacktestResult(
        trades=[], portfolio_values=[10_000.0], dates=[date(2026, 1, 2)],
        metrics={}, shadow_candidates=[{"portfolio": "momentum", "ticker": "AAPL"}],
    )
    configs = {"momentum": PortfolioConfig("momentum", 10_000.0, lambda *_: None, MagicMock())}
    path = save_multi_portfolio_results(
        config={}, results={"momentum": result}, portfolio_configs=configs,
        aggregate={"portfolio_values": [10_000.0], "trades": [], "dates": [], "metrics": {}},
        bars={}, output_dir=str(tmp_path),
    )
    payload = json.loads(Path(path).read_text())
    assert payload["portfolios"]["momentum"]["shadow_candidates"][0]["ticker"] == "AAPL"
```

Append this focused isolation test to `tests/backtest/test_multi_portfolio.py` (and add the shown imports if absent):

```python
from datetime import date
from unittest.mock import MagicMock

from backtest.runner import BacktestRunner
from backtest.simulator import SimulatedExecutor


class _PortfolioObserver:
    def __init__(self):
        self.records = []

    def observe(self, **kwargs):
        record = MagicMock()
        record.to_dict.return_value = dict(kwargs)
        self.records.append(record)


def test_each_portfolio_keeps_its_own_shadow_candidates():
    bars = {"AAPL": [{"date": date(2026, 1, 2), "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000}]}
    signal_fn = lambda ticker, history: {"action": "buy", "limit_price": 100.0, "quantity": 1.0, "sector": "Technology"}
    risk = MagicMock()
    risk.check_entry.return_value = MagicMock(approved=False, adjusted_quantity=0, reason="cap")
    results = {}
    for portfolio in ("momentum", "quality_value"):
        observer = _PortfolioObserver()
        results[portfolio] = BacktestRunner(
            SimulatedExecutor(slippage_bps=0, commission_per_share=0), 10_000
        ).run(
            bars, signal_fn, risk,
            candidate_observer=observer,
            portfolio_name=portfolio,
        )
    assert {row["portfolio"] for row in results["momentum"].shadow_candidates} == {"momentum"}
    assert {row["portfolio"] for row in results["quality_value"].shadow_candidates} == {"quality_value"}
```

- [ ] **Step 2: Run tests and verify artifact omission**

Run: `pytest tests/backtest/test_save_results.py tests/backtest/test_multi_portfolio.py -v`

Expected: failure because saved portfolio payloads omit `shadow_candidates`.

- [ ] **Step 3: Add the opt-in CLI and compute snapshots once**

Add:

```python
parser.add_argument("--research-shadow", action="store_true", help="Record factor snapshots for every raw sleeve buy candidate")
```

After bars are loaded and before portfolio backtests:

```python
shadow_recorders: dict[str, InMemoryShadowRecorder] = {}
if args.research_shadow:
    panel = build_factor_panel(bars_by_ticker)
    snapshots = FactorEngine(build_default_registry()).compute(panel, DEFAULT_FACTOR_IDS)
    shadow_recorders = {name: InMemoryShadowRecorder(snapshots) for name in portfolios}
```

Pass per-portfolio observers without changing defaults:

```python
results[name] = runner.run(
    bars_by_ticker,
    pc.signals_fn,
    pc.risk_engine,
    trade_start_date=trade_start_date,
    candidate_observer=shadow_recorders.get(name),
    portfolio_name=name,
)
```

- [ ] **Step 4: Save shadow records in both result formats**

Add `"shadow_candidates": result.shadow_candidates` to each portfolio payload in `save_multi_portfolio_results`. Add a `shadow_candidates` optional parameter with default `None` to `save_results` and persist it as `"shadow_candidates": shadow_candidates or []`. Pass the single result's shadow records from `main`.

- [ ] **Step 5: Run artifact and multi-portfolio tests**

Run: `pytest tests/backtest/test_save_results.py tests/backtest/test_multi_portfolio.py tests/backtest/test_runner.py tests/backtest/test_research_shadow.py -v`

Expected: all tests pass.

- [ ] **Step 6: Run a deterministic cached-bar shadow smoke test**

```bash
python scripts/run_backtest.py --bars-from-json output/backtest_multi_20260710_005841.json --research-shadow --output-dir /tmp/algo-poc-research-shadow
```

Expected: exit 0; the generated JSON contains `shadow_candidates` for all six portfolio keys. The referenced artifact is the canonical corrected baseline named in `docs/strategies/portfolio-2026-05.md`. Do not overwrite anything under `output/`.

- [ ] **Step 7: Commit**

```bash
git add scripts/run_backtest.py tests/backtest/test_save_results.py tests/backtest/test_multi_portfolio.py
git commit -m "feat: export multi-sleeve factor shadow artifacts"
```

---

### Task 8: Architectural Boundary and Phase Acceptance

**Files:**
- Create: `tests/research/test_architecture.py`
- Modify: `docs/superpowers/specs/2026-07-14-native-factor-research-design.md`

**Interfaces:**
- Verifies the complete phase boundary; produces no new runtime API.

- [ ] **Step 1: Write the architectural import-boundary test**

```python
# tests/research/test_architecture.py
from __future__ import annotations

import ast
from pathlib import Path


FORBIDDEN_ROOTS = {"ib_insync", "services.execution", "services.risk_management"}


def test_research_package_cannot_import_trading_surfaces():
    violations = []
    for path in Path("research").rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                if any(name == root or name.startswith(root + ".") for root in FORBIDDEN_ROOTS):
                    violations.append(f"{path}:{node.lineno}:{name}")
    assert violations == []
```

- [ ] **Step 2: Run the complete research and affected regression suites**

Run:

```bash
pytest tests/research/ tests/backtest/ tests/scripts/test_run_paper_gate.py tests/scripts/test_run_paper_reset.py tests/scripts/test_run_paper_research_shadow.py tests/shared/test_config.py tests/shared/test_models.py -v
```

Expected: all tests pass with zero failures.

- [ ] **Step 3: Run the full test suite**

Run: `pytest`

Expected: all tests pass with zero failures.

- [ ] **Step 4: Verify packaging**

Run: `pip wheel . --no-deps -w /tmp/algo-poc-wheel`

Expected: exit 0 and the wheel contains both `research/` and the existing `shared/`, `services/`, and `backtest/` packages. Inspect it with `unzip -l /tmp/algo-poc-wheel/algo_poc-*.whl`.

- [ ] **Step 5: Update the design delivery status**

Add a short implementation-status block to the design document:

```markdown
## Implementation status

- Phases 1–2: native factor foundation and six-sleeve shadow scoring implemented.
- Research remains disabled by default and observational only.
- Paper/live research candidate generation is not enabled.
```

- [ ] **Step 6: Review the final diff for forbidden scope expansion**

Run: `git diff --check addf50c..HEAD`

Expected: no whitespace errors. Confirm manually that there are no modifications under `services/execution/`, no new Redis publishing path, no IB client imports under `research/`, and no default-on research setting.

- [ ] **Step 7: Commit phase documentation**

```bash
git add tests/research/test_architecture.py docs/superpowers/specs/2026-07-14-native-factor-research-design.md
git commit -m "test: enforce research trading boundary"
```

## Phase Acceptance Criteria

This plan is complete only when all of the following are demonstrated:

- Four versioned causal price factors compute on frozen historical bars.
- Future-data mutation cannot alter prior factor outputs.
- All six sleeves produce opt-in shadow candidate records in backtests.
- Raw candidates rejected by risk are retained in shadow data.
- Paper shadow persistence is opt-in, idempotent, and uses an independent session.
- Observer failure leaves backtest and paper trading results unchanged.
- Existing reset safeguards remain green and do not delete research audit history.
- No research module imports execution, risk implementation, or IB clients.
- Research is disabled by default.
- The full repository test suite and package build pass.
