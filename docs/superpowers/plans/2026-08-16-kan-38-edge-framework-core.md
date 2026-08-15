# KAN-38 Edge Framework Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the three genuine gaps in the already-built edge-validation
framework — an implicit trial count, no pre-registered holdout, and no written
binding to the promotion pipeline's gates.

**Architecture:** Extend `research/evaluation/` rather than creating
`research/edge_framework/`. Deflated Sharpe, PSR, BH-FDR and purged nested
walk-forward already exist and are tested; a parallel package would fork the
definition of the gate's own numbers. Two additions, both opt-in: an optional
explicit `n_trials` threaded from a committed trial registry into
`expected_max_sharpe`/`control`, and a new `holdout.py` implementing a
date-registered, single-use holdout whose purge width is the same
`gap = horizon + embargo` that `folds.py` uses.

**Tech Stack:** Python 3.12, pytest, numpy/pandas (already locked). No new
third-party dependency — `research/` hand-rolls `norm_cdf` and `inv_norm`
rather than importing scipy.stats, and this work follows that convention.

**Spec:** JIRA KAN-38 (https://huiliang.atlassian.net/browse/KAN-38); source
direction doc `docs/designs/project-direction.md` D10 and § "Strategy
Promotion Pipeline (D3.2)".

## Global Constraints

- `research/` must not statically import `backtest`, `services`, `scripts`,
  `shared.redis_client`, `shared.schemas`, `redis`, `ib_insync`, `importlib`,
  `runpy`, or `builtins.__import__` — enforced by an AST scan in
  `tests/research/test_architecture.py:16-27`. Data enters via files.
- No new third-party dependency.
- All modules start with `from __future__ import annotations`.
- Existing behaviour must not change when the new inputs are absent: all 141
  existing `tests/research/` tests pass unmodified.
- `research/edge_framework/` must not be created.

---

### Task 1: Explicit trial count in the deflation math

**Files:**
- Modify: `research/evaluation/multiple_testing.py:39-50` (`expected_max_sharpe`), `:72-88` (`control`)
- Test: `tests/research/evaluation/test_multiple_testing.py`

**Interfaces:**
- Produces: `expected_max_sharpe(trial_srs: list[float], n_trials: int | None = None) -> float`;
  `control(per_factor: dict[str, dict], q: float = 0.10, dsr_threshold: float = 0.95, n_trials: int | None = None) -> dict[str, MultipleTestingVerdict]`

- [ ] **Step 1: Write the failing tests**

```python
def test_explicit_trial_count_deflates_harder_than_a_smaller_one():
    per_factor = {
        "strong": {"sr": 0.5, "n": 500, "skew": 0.0, "kurt": 3.0, "ic_p": 0.001},
        "noise": {"sr": 0.001, "n": 500, "skew": 0.0, "kurt": 3.0, "ic_p": 0.95},
    }
    four = control(per_factor, n_trials=4)
    eight = control(per_factor, n_trials=8)
    assert eight["strong"].deflated_sharpe < four["strong"].deflated_sharpe


def test_declared_trial_count_below_the_run_is_rejected():
    per_factor = {
        "a": {"sr": 0.4, "n": 500, "skew": 0.0, "kurt": 3.0, "ic_p": 0.01},
        "b": {"sr": 0.1, "n": 500, "skew": 0.0, "kurt": 3.0, "ic_p": 0.4},
    }
    with pytest.raises(ValueError, match="declared trial count"):
        control(per_factor, n_trials=1)


def test_expected_max_sharpe_uses_declared_count_not_sample_size():
    srs = [0.0, 0.2]
    assert expected_max_sharpe(srs, n_trials=8) > expected_max_sharpe(srs)
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/research/evaluation/test_multiple_testing.py -v`
Expected: FAIL — `control() got an unexpected keyword argument 'n_trials'`

- [ ] **Step 3: Implement**

`expected_max_sharpe` takes `n_trials: int | None = None`; the spread
estimate still comes from the observed `trial_srs` (that is the only sample
available), but the E[max] multiplier uses `m = n_trials or len(trial_srs)`.
`control` gains the same parameter, raises `ValueError` when
`n_trials < len(per_factor)`, and passes it through.

- [ ] **Step 4: Run the whole multiple-testing module's tests**

Run: `pytest tests/research/evaluation/test_multiple_testing.py -v`
Expected: PASS — including the 4 pre-existing tests, unmodified.

- [ ] **Step 5: Commit**

```bash
git add research/evaluation/multiple_testing.py tests/research/evaluation/test_multiple_testing.py
git commit -m "KAN-38: allow an explicit trial count in the DSR deflation"
```

---

### Task 2: The committed trial registry

**Files:**
- Create: `research/trial_registry.json`
- Create: `research/evaluation/trial_registry.py`
- Test: `tests/research/evaluation/test_trial_registry.py`

**Interfaces:**
- Produces: `declared_trial_count(path: Path | str | None = None) -> int`,
  `load_trial_registry(path: Path | str | None = None) -> TrialRegistry` with
  fields `version: int`, `entries: tuple[TrialEntry, ...]`, property
  `n_trials: int` (the sum of the entries' counts).

- [ ] **Step 1: Write the failing tests**

```python
def test_committed_registry_declares_at_least_the_sleeve_search():
    assert declared_trial_count() >= 8


def test_count_is_the_sum_of_entries(tmp_path):
    path = tmp_path / "registry.json"
    path.write_text(json.dumps({"version": 1, "entries": [
        {"searched_at": "2026-05-26", "what": "a", "n_trials": 8, "source": "x"},
        {"searched_at": "2026-08-02", "what": "b", "n_trials": 4, "source": "y"},
    ]}))
    assert declared_trial_count(path) == 12


def test_empty_registry_is_rejected(tmp_path):
    path = tmp_path / "registry.json"
    path.write_text(json.dumps({"version": 1, "entries": []}))
    with pytest.raises(ValueError, match="no trial-registry entries"):
        declared_trial_count(path)
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/research/evaluation/test_trial_registry.py -v`
Expected: FAIL — module `research.evaluation.trial_registry` does not exist.

- [ ] **Step 3: Implement**

The default path resolves from the module file
(`Path(__file__).resolve().parents[1] / "trial_registry.json"`), never from
the process CWD. The committed file records the two searches that produced
today's candidate set: the eight-sleeve selection that left six survivors,
and the four-factor native catalog.

- [ ] **Step 4: Run**

Run: `pytest tests/research/evaluation/test_trial_registry.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add research/trial_registry.json research/evaluation/trial_registry.py tests/research/evaluation/test_trial_registry.py
git commit -m "KAN-38: declare the historical search count in a committed registry"
```

---

### Task 3: Thread the registry through the evaluator

**Files:**
- Modify: `research/evaluation/evaluator.py:25-34` (`EvaluationConfig`), `:127` (`control` call), `:154-158` (reported config)
- Test: `tests/research/evaluation/test_evaluator.py`

**Interfaces:**
- Consumes: `declared_trial_count()` (Task 2), `control(..., n_trials=)` (Task 1)
- Produces: `EvaluationConfig.n_trials: int | None = None`; the returned
  `result["config"]["n_trials"]` is the resolved integer, never `None`.

- [ ] **Step 1: Write the failing test**

```python
def test_evaluation_deflates_against_the_declared_registry_count():
    config = EvaluationConfig(horizon=5, n_outer=3, n_inner=2, embargo=5, min_names=3)
    result = evaluate_factors(_trending_bars(), config=config)
    assert result["config"]["n_trials"] == declared_trial_count()


def test_explicit_config_overrides_the_registry_and_deflates_harder():
    kwargs = dict(horizon=5, n_outer=3, n_inner=2, embargo=5, min_names=3)
    lenient = evaluate_factors(_trending_bars(), config=EvaluationConfig(n_trials=4, **kwargs))
    strict = evaluate_factors(_trending_bars(), config=EvaluationConfig(n_trials=64, **kwargs))
    for factor_id, strict_row in strict["factors"].items():
        assert strict_row["deflated_sharpe"] <= lenient["factors"][factor_id]["deflated_sharpe"]
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/research/evaluation/test_evaluator.py -v`
Expected: FAIL — `EvaluationConfig` has no field `n_trials`.

- [ ] **Step 3: Implement**

`n_trials: int | None = None` on the config; `evaluate_factors` resolves
`config.n_trials if config.n_trials is not None else declared_trial_count()`
and passes it to `control`, then reports the resolved value in the run card's
config block.

- [ ] **Step 4: Run the full research suite**

Run: `pytest tests/research -q`
Expected: PASS, 141 pre-existing tests unmodified.

- [ ] **Step 5: Commit**

```bash
git add research/evaluation/evaluator.py tests/research/evaluation/test_evaluator.py
git commit -m "KAN-38: deflate factor evaluation against the declared trial count"
```

---

### Task 4: The pre-registered, single-use holdout

**Files:**
- Create: `research/evaluation/holdout.py`
- Create: `research/holdout_registry.json`
- Test: `tests/research/evaluation/test_holdout.py`

**Interfaces:**
- Produces:
  - `HoldoutRegistration(split_id, holdout_start, horizon, embargo, registered_at, note)` — frozen dataclass, `holdout_start` an ISO date string
  - `HoldoutSplit(split_id, train: tuple[int, int], purge: tuple[int, int], holdout: tuple[int, int], gap: int)` — frozen dataclass
  - `HoldoutEvaluated(split_id, label, evaluated_at)` — frozen dataclass
  - `HoldoutProtocol.load(path=None)`, `.register(...) -> HoldoutRegistration`,
    `.registration(split_id) -> HoldoutRegistration`,
    `.resolve(split_id, dates) -> HoldoutSplit`,
    `.is_burned(split_id) -> bool`,
    `.evaluate(split_id, dates, label, evaluated_at=None) -> HoldoutSplit`
  - `HoldoutAlreadyEvaluated(RuntimeError)`

Boundaries are registered as a **date**, not an index: bars arrive daily, so
an index-registered boundary silently slides through the data as the panel
grows, which is the one thing a pre-registration must not do. `resolve()`
maps the date onto a supplied date index and applies the purge in index
space, exactly as `folds.py` does.

- [ ] **Step 1: Write the failing tests**

```python
def test_purge_width_matches_the_walk_forward_gap():
    dates = [date(2024, 1, 1) + timedelta(days=i) for i in range(400)]
    protocol = _protocol_with_split(horizon=21, embargo=21, start=dates[300])
    split = protocol.resolve("s", dates)
    folds = nested_walk_forward(len(dates), 4, 3, 21, 21)
    assert split.gap == 21 + 21 == folds[0].test[0] - folds[0].train[1]


def test_boundary_rows_are_purged_from_training():
    dates = [date(2024, 1, 1) + timedelta(days=i) for i in range(400)]
    protocol = _protocol_with_split(horizon=21, embargo=21, start=dates[300])
    split = protocol.resolve("s", dates)
    assert split.holdout == (300, 400)
    assert split.train == (0, 300 - 42)
    assert split.purge == (258, 300)
    assert set(range(*split.train)).isdisjoint(range(*split.holdout))


def test_second_evaluation_of_the_same_split_raises(tmp_path):
    ...
    protocol.evaluate("s", dates, label="first")
    with pytest.raises(HoldoutAlreadyEvaluated):
        protocol.evaluate("s", dates, label="second")


def test_the_burn_survives_a_reload(tmp_path):
    ...  # a fresh HoldoutProtocol.load(path) still refuses the second call


def test_committed_registry_has_boundaries_and_a_timestamp():
    protocol = HoldoutProtocol.load()
    registration = protocol.registration("incumbent_sleeves_2026")
    assert registration.holdout_start and registration.registered_at
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/research/evaluation/test_holdout.py -v`
Expected: FAIL — module `research.evaluation.holdout` does not exist.

- [ ] **Step 3: Implement**

`gap = horizon + embargo`, `holdout = (start_index, n_dates)`,
`purge = (start_index - gap, start_index)`, `train = (0, start_index - gap)`;
raise `ValueError("not enough dates")` when the training span would be empty,
matching `folds.py`'s message. `evaluate()` appends to the file's
`evaluations` list and rewrites it, so the burn is recorded in git rather
than in memory. The committed registry pre-registers one split,
`incumbent_sleeves_2026`, starting `2026-06-01` — the six-sleeve set was
fixed on 2026-05-26, so nothing from June onward was part of the search that
produced it.

- [ ] **Step 4: Run**

Run: `pytest tests/research/evaluation/test_holdout.py tests/research/test_architecture.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add research/evaluation/holdout.py research/holdout_registry.json tests/research/evaluation/test_holdout.py
git commit -m "KAN-38: add a pre-registered single-use holdout protocol"
```

---

### Task 5: Document the promotion-pipeline binding

**Files:**
- Create: `docs/operations/edge-validation-framework.md`
- Modify: `docs/operations/README.md`

- [ ] **Step 1: Write the runbook**

What gate S and gate P require: a deflated Sharpe computed against the
declared trial count, performance on the pre-registered holdout, and (KAN-39)
parameter stability. How to add a registry entry when a new search happens,
how to register a holdout, and why the burn is one-way.

- [ ] **Step 2: Index it**

Add the runbook to `docs/operations/README.md` under "Reviews & governance".

- [ ] **Step 3: Full suite**

Run: `pytest -q`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add docs/operations/edge-validation-framework.md docs/operations/README.md
git commit -m "KAN-38: document the gate-S/gate-P edge-validation binding"
```
