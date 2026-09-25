# KAN-60 Rung-0 Baseline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce and pin the Rung-0 capital-specific baseline (momentum only, whole shares, $1 floor, USD 3,700), record its accepted coverage bias as D21, and let the rolling shadow carry whole-share sizing under its own id.

**Architecture:** `scripts/run_backtest.py` learns to run a sleeve subset at full capital and write to an exact path; the artifact is generated offline from the D20 bars. A second config pin (`divergence.rung0_baseline_pin`) names it, resolved by the existing `scripts/ops/baseline_pin.py`. The shadow's `whole_shares` flag is plumbed through `produce_shadow_artifact` and folded into `shadow_id_for` only when true, so today's ids are untouched.

**Tech Stack:** Python 3.12, pytest (`asyncio_mode=auto`), pydantic config, SQLite in tests.

**Spec:** `docs/superpowers/specs/2026-09-25-kan-60-rung0-baseline-design.md`

## Global Constraints

- Every module keeps `from __future__ import annotations`.
- A default `run_backtest.py` invocation (no `--sleeves`, no `--output`) must stay byte-identical in behaviour: same six sleeves, same capitals, same `backtest_multi_<TS>.json` name.
- Artifact path: `output/baselines/rung0_momentum_<YYYYMMDD>.json` — must NOT match `backtest_multi_*`.
- Coverage floor stays `5.0`; the artifact stays `BLOCKED`; nothing in `backtest/membership.py` changes.
- `shadow_id_for(portfolios)` with `whole_shares=False` returns exactly today's id.
- `backtest/sleeve_comparability.py` must not import `backtest.divergence`; `backtest.divergence` must not import `bias_acceptance` (existing tests pin both).
- Never overwrite or delete anything under `output/`; the only write there is the new artifact file.
- Run tests from the worktree with `python -m pytest` (the editable install may point at another checkout).

## Review Focus

1. `--sleeves momentum` with `--capital 3700` must give momentum 3,700, not 3,700 × 0.2308 — test in Task 1.
2. A one-sleeve run must not fall into the single-portfolio writer or crash in the rebalancer/aggregate branch — test in Task 1.
3. `--output` into a directory that does not exist yet must create it — test in Task 1.
4. `--rung0` with the rung-0 key unset but `baseline_pin` set must NOT fall back to the edge pin — test in Task 2.
5. A whole-share shadow and a fractional shadow of the same roster must never share a `baseline_id` in `divergence_daily` — test in Task 3.

---

### Task 1: `run_backtest.py` — `--sleeves` and `--output`

**Files:**
- Modify: `scripts/run_backtest.py` (allocation table near the top-level constants; `save_multi_portfolio_results` ~2413; `main()` argparse ~2506-2575, signal-fn/portfolio block ~2740-2893, print branch ~2956, save branch ~3048)
- Test: `tests/scripts/test_run_backtest_sleeve_subset.py` (create)

**Interfaces:**
- Produces: `SLEEVE_ALLOCATIONS: dict[str, float]` (the six fractions), `sleeve_capital_fractions(selected: Sequence[str] | None) -> dict[str, float]`, `save_multi_portfolio_results(..., path: str | None = None)`, CLI flags `--sleeves NAME [NAME ...]` and `--output PATH`.

- [ ] **Step 1: Write the failing tests**

```python
"""KAN-60: a sleeve subset runs at full capital, to an exact path.

Rung 0 runs one sleeve (D8, rung0-economics §9). The baseline for it has to be
a momentum-only run at the rung's whole capital, written somewhere the weekly
refresh's globs never see.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from scripts import run_backtest

SESSIONS = 320
FIRST_SESSION = date(2023, 1, 3)


def _bars(n: int, drift: float = 0.001) -> list[dict]:
    price, out = 100.0, []
    for i in range(n):
        price *= 1 + drift
        d = FIRST_SESSION + timedelta(days=i)
        out.append({"date": d.isoformat(), "open": round(price, 4),
                    "high": round(price * 1.01, 4), "low": round(price * 0.99, 4),
                    "close": round(price, 4), "volume": 1_000_000})
    return out


def _run(tmp_path: Path, monkeypatch, *extra: str) -> Path:
    snapshots = tmp_path / "membership.json"
    snapshots.write_text(json.dumps({FIRST_SESSION.isoformat(): ["AAA", "BBB"]}))
    bars = tmp_path / "bars.json"
    bars.write_text(json.dumps({"bars": {"AAA": _bars(SESSIONS), "BBB": _bars(SESSIONS)}}))
    out_dir = tmp_path / "output"
    out_dir.mkdir()
    monkeypatch.setattr(run_backtest.sys, "argv", [
        "run_backtest.py", "--bars-from-json", str(bars),
        "--universe-snapshots", str(snapshots), "--output-dir", str(out_dir),
        "--years", "2", *extra,
    ])
    run_backtest.main()
    return out_dir


def test_the_allocation_table_is_the_six_live_fractions() -> None:
    """Default runs stay byte-identical only if the table IS the old literals."""
    assert run_backtest.SLEEVE_ALLOCATIONS == {
        "momentum": 0.2308, "sector_rotation": 0.1538,
        "thematic_momentum": 0.1410, "quality_value": 0.1538,
        "earnings_drift": 0.1923, "tail_risk_hedge": 0.1283,
    }
    assert run_backtest.sleeve_capital_fractions(None) == run_backtest.SLEEVE_ALLOCATIONS


def test_a_single_sleeve_gets_the_whole_capital() -> None:
    assert run_backtest.sleeve_capital_fractions(["momentum"]) == {"momentum": 1.0}


def test_a_subset_is_renormalised_over_itself() -> None:
    fractions = run_backtest.sleeve_capital_fractions(["momentum", "sector_rotation"])
    assert sum(fractions.values()) == pytest.approx(1.0)
    assert fractions["momentum"] == pytest.approx(0.2308 / (0.2308 + 0.1538))


def test_an_unknown_sleeve_is_refused(tmp_path, monkeypatch) -> None:
    with pytest.raises(SystemExit):
        _run(tmp_path, monkeypatch, "--sleeves", "mean_reversion")


def test_momentum_only_writes_the_multi_envelope_at_the_exact_path(
    tmp_path, monkeypatch
) -> None:
    target = tmp_path / "output" / "baselines" / "rung0_momentum_20260925.json"
    out_dir = _run(tmp_path, monkeypatch, "--capital", "3700", "--whole-shares",
                   "--sleeves", "momentum", "--output", str(target))

    assert target.exists(), "--output must create its parent and write there"
    assert not list(out_dir.glob("backtest_*.json")), (
        "an --output run must not also drop a backtest_* file the refresh globs see"
    )
    artifact = json.loads(target.read_text())
    assert set(artifact) == {"config", "portfolios", "aggregate", "bars"}
    assert set(artifact["portfolios"]) == {"momentum"}
    assert artifact["portfolios"]["momentum"]["config"]["capital"] == pytest.approx(3700.0)
    assert artifact["config"]["portfolios"] == {"momentum": pytest.approx(3700.0)}
    assert artifact["config"]["whole_shares"] is True
    assert artifact["config"]["commission_minimum"] == pytest.approx(1.0)


def test_a_default_run_is_unchanged(tmp_path, monkeypatch) -> None:
    out_dir = _run(tmp_path, monkeypatch, "--capital", "100000")
    [artifact_path] = out_dir.glob("backtest_multi_*.json")
    artifact = json.loads(artifact_path.read_text())
    assert set(artifact["portfolios"]) == set(run_backtest.SLEEVE_ALLOCATIONS)
    assert artifact["config"]["portfolios"]["momentum"] == pytest.approx(23080.0)
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/scripts/test_run_backtest_sleeve_subset.py -v`
Expected: FAIL — `AttributeError: module 'scripts.run_backtest' has no attribute 'SLEEVE_ALLOCATIONS'`, and `unrecognized arguments: --sleeves`.

- [ ] **Step 3: Implement**

Add near the other module-level constants (after the `ACTIVE_SLEEVES` import block):

```python
#: Each sleeve's share of --capital. Must agree with
#: scripts/run_paper.py::CAPITAL_ALLOCATIONS. One table rather than a literal
#: per call site, so a --sleeves subset can renormalise it.
SLEEVE_ALLOCATIONS: dict[str, float] = {
    "momentum": 0.2308,
    "sector_rotation": 0.1538,
    "thematic_momentum": 0.1410,
    "quality_value": 0.1538,
    "earnings_drift": 0.1923,
    "tail_risk_hedge": 0.1283,
}


def sleeve_capital_fractions(selected: Sequence[str] | None) -> dict[str, float]:
    """Capital fractions for the sleeves that will run.

    ``None`` is every sleeve at its live allocation, exactly as before. A subset
    is renormalised over itself: Rung 0 runs momentum alone at the rung's whole
    capital (D8), and 23% of USD 3,700 would describe a book nobody holds.
    """
    if selected is None:
        return dict(SLEEVE_ALLOCATIONS)
    total = sum(SLEEVE_ALLOCATIONS[name] for name in selected)
    return {name: SLEEVE_ALLOCATIONS[name] / total for name in selected}
```

(Import `Sequence` from `collections.abc` if not already imported.)

In `main()` argparse, after `--output-dir`:

```python
    parser.add_argument(
        "--sleeves", nargs="+", choices=list(SLEEVE_ALLOCATIONS), default=None,
        help="Run only these sleeves, their allocations renormalised over the "
             "selection (a single sleeve gets the whole --capital). Always "
             "writes the multi-portfolio envelope. Default: all six.",
    )
    parser.add_argument(
        "--output", default=None,
        help="Exact results path, parent created. Overrides --output-dir naming.",
    )
```

After `args = parser.parse_args()`: `fractions = sleeve_capital_fractions(args.sleeves)`. Replace each `args.capital * 0.2308` / `0.1538` / `0.1410` / `0.1538` / `0.1923` / `0.1283` (both the `initial_capital=` in the signal fn and the `capital=` in `PortfolioConfig`) with `args.capital * fractions.get("<sleeve>", SLEEVE_ALLOCATIONS["<sleeve>"])` — the `.get` default keeps unselected sleeves' closures buildable; they are filtered out next. Right after the `portfolios` dict literal:

```python
    if args.sleeves is not None:
        portfolios = {name: pc for name, pc in portfolios.items() if name in fractions}
```

Define `multi = len(portfolios) > 1 or args.sleeves is not None` and use `if not multi:` in place of both `if len(portfolios) == 1:` (print branch ~2956 and save branch ~3048). Inside the multi print branch, wrap the rebalancer simulation and its print block in `if len(portfolios) > 1:` — a one-sleeve book has nothing to rebalance.

`save_multi_portfolio_results` gains `path: str | None = None`; replace the dir/timestamp lines with:

```python
    if path is None:
        os.makedirs(output_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(output_dir, f"backtest_multi_{timestamp}.json")
    else:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
```

and pass `path=args.output` from `main()`.

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/scripts/test_run_backtest_sleeve_subset.py tests/scripts/test_run_backtest_coverage_artifact.py tests/scripts/test_run_backtest_baseline.py tests/backtest/test_multi_portfolio.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/run_backtest.py tests/scripts/test_run_backtest_sleeve_subset.py
git commit -m "feat(KAN-60): run_backtest --sleeves subset at full capital, --output exact path"
```

---

### Task 2: `divergence.rung0_baseline_pin` and `baseline_pin.py --rung0`

**Files:**
- Modify: `shared/config.py` (`DivergenceConfig`, ~217-247), `scripts/ops/baseline_pin.py`
- Test: `tests/ops/test_baseline_pin.py`

**Interfaces:**
- Produces: `DivergenceConfig.rung0_baseline_pin: str | None`, `resolve_pin(config_path=DEFAULT_CONFIG, *, rung0: bool = False) -> str | None`, `RUNG0_ENV_OVERRIDE = "ALGO_RUNG0_BASELINE_PIN"`, CLI `baseline_pin.py --rung0`.

- [ ] **Step 1: Write the failing tests** (append to `tests/ops/test_baseline_pin.py`)

```python
def _rung0_config(tmp_path: Path, *, edge: object = None, rung0: object = None) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(
        {"divergence": {"baseline_pin": edge, "rung0_baseline_pin": rung0}}
    ))
    return path


def test_rung0_resolves_its_own_key(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ALGO_RUNG0_BASELINE_PIN", raising=False)
    config = _rung0_config(tmp_path, edge="output/backtest_multi_x.json",
                           rung0="output/baselines/rung0_momentum_20260925.json")

    assert resolve_pin(str(config), rung0=True) == str(
        tmp_path / "output" / "baselines" / "rung0_momentum_20260925.json"
    )
    assert resolve_pin(str(config)) == str(tmp_path / "output" / "backtest_multi_x.json")


def test_an_unset_rung0_pin_never_falls_back_to_the_edge_pin(tmp_path: Path, monkeypatch):
    """The two pins describe different books. Silently answering with the
    six-sleeve USD 100k artifact would quote the wrong economics for Rung 0."""
    monkeypatch.delenv("ALGO_RUNG0_BASELINE_PIN", raising=False)
    config = _rung0_config(tmp_path, edge="output/backtest_multi_x.json")

    assert resolve_pin(str(config), rung0=True) is None
    assert main(["--config", str(config), "--rung0"]) == 1


def test_each_pin_has_its_own_env_override(tmp_path: Path, monkeypatch):
    config = _rung0_config(tmp_path, edge="a.json", rung0="b.json")
    monkeypatch.setenv("ALGO_BASELINE_PIN", str(tmp_path / "edge_override.json"))
    monkeypatch.delenv("ALGO_RUNG0_BASELINE_PIN", raising=False)

    assert resolve_pin(str(config), rung0=True) == str(Path("b.json").absolute()), (
        "the edge override must not redirect the rung-0 pin"
    )
    monkeypatch.setenv("ALGO_RUNG0_BASELINE_PIN", str(tmp_path / "r0.json"))
    assert resolve_pin(str(config), rung0=True) == str(tmp_path / "r0.json")


def test_the_committed_config_parses_both_pins():
    from shared.config import load_config
    div = load_config(str(REPO / "config/default.yaml")).divergence
    assert div.baseline_pin
    assert hasattr(div, "rung0_baseline_pin")
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/ops/test_baseline_pin.py -v`
Expected: FAIL — `TypeError: resolve_pin() got an unexpected keyword argument 'rung0'`.

- [ ] **Step 3: Implement**

`shared/config.py`, in `DivergenceConfig` after `baseline_pin`:

```python
    #: The Rung-0 baseline of record (KAN-60): momentum only, whole shares,
    #: the $1 commission floor, USD 3,700 — the book D8 says Rung 0 holds.
    #: It is the reference for Rung-0 economics and divergence thresholds
    #: (rung0-economics §9.6), NOT the edge-evidence baseline above and not the
    #: nightly drift feed, which is the rolling shadow (D19). ``None`` is
    #: unpinned; it never falls back to ``baseline_pin``.
    rung0_baseline_pin: str | None = None
```

`scripts/ops/baseline_pin.py`:

```python
#: The rung-0 pin's own override, so redirecting one pin can never move the other.
RUNG0_ENV_OVERRIDE = "ALGO_RUNG0_BASELINE_PIN"


def resolve_pin(config_path: str = DEFAULT_CONFIG, *, rung0: bool = False) -> str | None:
    """...existing docstring...

    ``rung0`` selects ``divergence.rung0_baseline_pin`` (KAN-60) and its own
    override. An unset rung-0 pin is None, never the edge pin.
    """
    env_name = RUNG0_ENV_OVERRIDE if rung0 else ENV_OVERRIDE
    override = os.environ.get(env_name)
    if override and override.strip():
        return str(Path(override.strip()).absolute())

    try:
        divergence = load_config(config_path).divergence
    except Exception as exc:  # noqa: BLE001 - stdout must stay a path or empty
        print(f"baseline_pin: could not read {config_path}: {exc}", file=sys.stderr)
        return None

    pin = divergence.rung0_baseline_pin if rung0 else divergence.baseline_pin
    if not pin or not pin.strip():
        return None
    return str(Path(pin.strip()).absolute())
```

In `main()`: add `parser.add_argument("--rung0", action="store_true", help="Resolve divergence.rung0_baseline_pin (KAN-60) instead.")`, call `resolve_pin(args.config, rung0=args.rung0)`, and make the error name the right key/env: `key = "divergence.rung0_baseline_pin" if args.rung0 else "divergence.baseline_pin"`, `env = RUNG0_ENV_OVERRIDE if args.rung0 else ENV_OVERRIDE`.

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/ops/test_baseline_pin.py tests/shared/test_config.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add shared/config.py scripts/ops/baseline_pin.py tests/ops/test_baseline_pin.py
git commit -m "feat(KAN-60): divergence.rung0_baseline_pin, resolved by baseline_pin.py --rung0"
```

---

### Task 3: whole-share sizing reaches the shadow and its id

**Files:**
- Modify: `backtest/shadow_artifact.py` (`shadow_id_for`, ~77-111), `scripts/run_paper.py` (`produce_shadow_artifact`, ~154-208)
- Test: `tests/backtest/test_shadow_artifact.py`, `tests/scripts/test_run_paper_shadow_production.py`, `tests/scripts/test_divergence_monitor_shadow_e2e.py`

**Interfaces:**
- Produces: `shadow_id_for(portfolios, *, whole_shares: bool = False) -> str`; `produce_shadow_artifact(..., whole_shares: bool = False)`.
- Consumes: `build_shadow_series(..., whole_shares=...)` (exists, `backtest/shadow_series.py:101`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/backtest/test_shadow_artifact.py`:

```python
class _Sleeve:
    def __init__(self, params):
        self.shadow_params = params


def test_fractional_ids_are_unchanged_by_the_whole_shares_flag() -> None:
    """Every divergence_daily row on file keys on today's id; moving it would
    orphan the breach streak for a change nobody made."""
    from backtest.shadow_artifact import shadow_id_for
    roster = {"momentum": _Sleeve({"top_n": 5})}
    assert shadow_id_for(roster, whole_shares=False) == shadow_id_for(roster)


def test_whole_share_sizing_is_a_different_model() -> None:
    from backtest.shadow_artifact import shadow_id_for
    roster = {"momentum": _Sleeve({"top_n": 5})}
    whole = shadow_id_for(roster, whole_shares=True)
    assert whole.startswith("shadow:")
    assert whole != shadow_id_for(roster)
```

Append to `tests/scripts/test_run_paper_shadow_production.py`:

```python
def test_whole_share_sizing_reaches_the_replay(tmp_path, monkeypatch) -> None:
    import scripts.run_paper as run_paper
    seen = {}
    real = run_paper.build_shadow_series

    def spy(**kwargs):
        seen["whole_shares"] = kwargs.get("whole_shares")
        return real(**kwargs)

    monkeypatch.setattr(run_paper, "build_shadow_series", spy)
    _produce(tmp_path, whole_shares=True)
    assert seen["whole_shares"] is True


def test_a_rung0_shadow_is_filed_under_its_own_id(tmp_path) -> None:
    """Rung 0: momentum is the only sleeve with live history, sized in whole
    shares. Its evidence must never share an id with the fractional book."""
    live = {"momentum": _live_equity()["momentum"]}
    rung0 = load_shadow(_produce(tmp_path, output_path=tmp_path / "r0.json",
                                 live_equity=live, capital=3_700.0, whole_shares=True))
    fractional = load_shadow(_produce(tmp_path, output_path=tmp_path / "fr.json",
                                      live_equity=live, capital=3_700.0))

    assert set(rung0.series) == {"momentum"}
    assert rung0.shadow_id != fractional.shadow_id
```

Append to `tests/scripts/test_divergence_monitor_shadow_e2e.py`:

```python
def test_a_whole_share_shadow_files_verdicts_under_its_own_id(
    tmp_path, monkeypatch
) -> None:
    """KAN-60 AC3 as restated after D19: the Rung-0 instrument is the
    whole-share shadow, and divergence_daily rows carry ITS baseline_id."""
    from backtest.shadow_artifact import shadow_id_for

    class _S:
        shadow_params = {"top_n": 5}

    whole_id = shadow_id_for({"momentum": _S()}, whole_shares=True)
    frac_id = shadow_id_for({"momentum": _S()})
    db_url = _db(tmp_path, "rung0")
    path = _shadow(tmp_path)
    raw = json.loads(path.read_text())
    raw["shadow_id"] = whole_id
    path.write_text(json.dumps(raw))

    _run(monkeypatch, db_url=db_url, shadow=path, output=tmp_path / "divergence.json")

    ids = {r.baseline_id for r in _verdict_rows(db_url)}
    assert ids == {whole_id}
    assert frac_id not in ids
```

(If `dump_shadow` stores the id under a key other than `shadow_id`, read `backtest/shadow_artifact.py::dump_shadow` and use that key — or pass `shadow_id=whole_id` by extending `_shadow` with a `shadow_id` keyword defaulting to `"shadow:aaaabbbbccccdddd"`; prefer the keyword.)

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/backtest/test_shadow_artifact.py tests/scripts/test_run_paper_shadow_production.py tests/scripts/test_divergence_monitor_shadow_e2e.py -v`
Expected: FAIL — `TypeError: shadow_id_for() got an unexpected keyword argument 'whole_shares'` / `produce_shadow_artifact() got an unexpected keyword argument 'whole_shares'`.

- [ ] **Step 3: Implement**

`backtest/shadow_artifact.py`:

```python
def shadow_id_for(portfolios: Mapping[str, Any], *, whole_shares: bool = False) -> str:
    """...existing docstring...

    ``whole_shares`` changes what the replay would do — a budget below one
    share opens nothing — so it is part of the model (KAN-60). It enters the
    fingerprint only when true, which keeps every fractional id byte-identical
    to the ones already filed in ``divergence_daily``.
    """
    ...  # unfingerprinted check unchanged
    fingerprint = sorted(
        (name, json.dumps(sleeve.shadow_params, sort_keys=True, default=str))
        for name, sleeve in portfolios.items()
    )
    if whole_shares:
        fingerprint.append(("__sizing__", "whole_shares"))
    ...  # digest unchanged
```

`scripts/run_paper.py::produce_shadow_artifact`: add keyword `whole_shares: bool = False`, document it ("Truncate the replay's sizing as live execution does. Off until the paper book itself sizes in whole shares (KAN-33): a whole-share shadow graded against a fractional book would manufacture drift."), pass `whole_shares=whole_shares` to `build_shadow_series(...)` and `shadow_id_for(shadow_portfolios, whole_shares=whole_shares)`. Do not change the call site at ~2381 — production stays fractional.

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/backtest/test_shadow_artifact.py tests/scripts/test_run_paper_shadow_production.py tests/scripts/test_divergence_monitor_shadow_e2e.py tests/backtest/test_shadow_series.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add backtest/shadow_artifact.py scripts/run_paper.py tests/backtest/test_shadow_artifact.py tests/scripts/test_run_paper_shadow_production.py tests/scripts/test_divergence_monitor_shadow_e2e.py
git commit -m "feat(KAN-60): whole-share shadow sizing, folded into the shadow id only when on"
```

---

### Task 4: generate the artifact, pin it, record D21

**Files:**
- Create (gitignored, not committed): `output/baselines/rung0_momentum_<YYYYMMDD>.json` in the dev checkout `/Users/huiliang/GitHub/algo-poc`
- Modify: `research/bias_acceptances.json`, `config/default.yaml` (`divergence:` block), `docs/designs/project-direction.md` (decision table + new D21 section + D19 addendum), `docs/operations/backtest-baseline.md`, `docs/operations/rung0-economics.md` §9.6(2)
- Test: `tests/docs/test_rung0_baseline_decision.py` (create)

**Interfaces:**
- Consumes: Task 1's `--sleeves`/`--output`; Task 2's `rung0_baseline_pin`; `backtest.bias_acceptance.load_acceptances`, `resolve_admissibility`; `backtest.divergence.execution_model_from_backtest_config`.

- [ ] **Step 1: Generate the artifact** (from the worktree, reading the dev checkout's bars; ~minutes, no IB)

```bash
ROOT=/Users/huiliang/GitHub/algo-poc
PYTHONPATH=. python scripts/run_backtest.py --years 10 --capital 3700 --whole-shares \
  --sleeves momentum \
  --bars-from-json $ROOT/output/backtest_multi_20260915_102125.json \
  --universe-snapshots data/universe/sp500_membership.json \
  --output $ROOT/output/baselines/rung0_momentum_$(date +%Y%m%d).json
```

Then record its facts (these become the constants below — do not guess them):

```bash
F=$ROOT/output/baselines/rung0_momentum_<YYYYMMDD>.json
shasum -a 256 "$F"
python3 -c "import json,sys;c=json.load(open(sys.argv[1]))['config'];print(c['coverage']['state'],c['coverage']['excluded_pct'],c['coverage']['excluded_membership_days'],c['coverage']['total_membership_days'],c['portfolios'],c['whole_shares'],c['commission_minimum'])" "$F"
```

Expected: `BLOCKED`, excluded_pct ≈ 11.21, `{'momentum': 3700.0}`, `True`, `1.0`. If the state is `OK` or the pct is far from 11.21, STOP and investigate with superpowers:systematic-debugging before recording anything.

- [ ] **Step 2: Write the failing doc/registry tests**

```python
"""KAN-60 / D21: the Rung-0 baseline of record and its accepted coverage bias.

Same guards as D20 (tests/docs/test_pit_coverage_decision.py): the registry is
what admits a run, the prose is what a human reads, and they must not drift.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from backtest.bias_acceptance import (
    REQUIREMENT_COVERAGE_FLOOR, ADMISSIBLE_WITH_ACCEPTED_BIAS,
    load_acceptances, resolve_admissibility,
)

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "research/bias_acceptances.json"
DIRECTION = ROOT / "docs/designs/project-direction.md"
BASELINE_DOC = ROOT / "docs/operations/backtest-baseline.md"
ECONOMICS = ROOT / "docs/operations/rung0-economics.md"

D21_ARTIFACT = "output/baselines/rung0_momentum_<YYYYMMDD>.json"   # from Step 1
D21_SHA256 = "<sha256 from Step 1>"
D21_EXCLUDED_PCT = "<excluded_pct from Step 1, 2dp>"


def _raw(decision: str) -> dict:
    rows = [e for e in json.loads(REGISTRY.read_text())["acceptances"]
            if e["decision"] == decision]
    assert len(rows) == 1, (decision, len(rows))
    return rows[0]


def _d21():
    rows = [a for a in load_acceptances(REGISTRY) if a.decision == "D21"]
    assert len(rows) == 1, "exactly one D21 acceptance, or its figures are ambiguous"
    return rows[0]


def test_d21_accepts_the_coverage_floor_for_one_artifact() -> None:
    entry = _d21()
    assert entry.requirement == REQUIREMENT_COVERAGE_FLOOR
    assert entry.source_sha256 == D21_SHA256
    assert _raw("D21")["source"] == D21_ARTIFACT
    assert entry.floor_pct == 5.0
    assert f"{entry.excluded_pct:.2f}" == D21_EXCLUDED_PCT


def test_d21_keeps_the_d18_re_evidence_date() -> None:
    assert "2029-08-18" in _d21().re_evidence


def test_d21_does_not_disturb_d18_or_d20() -> None:
    shas = {_raw(d)["source_sha256"] for d in ("D18", "D20", "D21")}
    assert len(shas) == 3


def test_the_rung0_pin_names_the_d21_artifact() -> None:
    config = (ROOT / "config/default.yaml").read_text()
    pinned = [ln.split(":", 1)[1].strip() for ln in config.splitlines()
              if ln.strip().startswith("rung0_baseline_pin:")]
    assert pinned == [D21_ARTIFACT]


def test_the_rung0_artifact_is_outside_every_refresh_glob() -> None:
    assert not Path(D21_ARTIFACT).name.startswith("backtest_multi_")


def test_the_prose_carries_the_decision() -> None:
    direction = DIRECTION.read_text()
    assert "(D21)" in direction and f"{D21_EXCLUDED_PCT}%" in direction
    assert "KAN-60" in direction
    assert Path(D21_ARTIFACT).name in BASELINE_DOC.read_text()
    assert Path(D21_ARTIFACT).name in ECONOMICS.read_text()


def test_the_on_disk_artifact_resolves_with_the_accepted_bias() -> None:
    """Skipped where output/ is absent (CI), like the D18/D20 on-disk checks."""
    import hashlib
    from backtest.divergence import execution_model_from_backtest_config
    from backtest.membership import CoverageReport

    path = ROOT / D21_ARTIFACT
    if not path.exists():
        pytest.skip(f"{D21_ARTIFACT} not present in this checkout")
    config = json.loads(path.read_text())["config"]
    assert config["portfolios"] == {"momentum": pytest.approx(3700.0)}
    assert config["whole_shares"] is True
    verdict = resolve_admissibility(
        execution_model_from_backtest_config(config),
        source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        coverage=CoverageReport(**config["coverage"]),
        acceptances=load_acceptances(REGISTRY),
    )
    assert verdict.state == ADMISSIBLE_WITH_ACCEPTED_BIAS
```

Fill the three constants from Step 1. The on-disk test looks under the worktree's `ROOT`, where `output/` is absent, so it SKIPs there exactly as the D18 on-disk check does in CI. Prove admissibility once by hand instead: run the same `resolve_admissibility` call in a `python -c` against the dev-checkout file and paste the printed `verdict.state` (expect `ADMISSIBLE_WITH_ACCEPTED_BIAS`).

- [ ] **Step 3: Run to verify they fail**

Run: `python -m pytest tests/docs/test_rung0_baseline_decision.py -v`
Expected: FAIL — `exactly one D21 acceptance` (0 found).

- [ ] **Step 4: Record the decision**

1. `research/bias_acceptances.json` — append (figures from Step 1):

```json
{
  "decision": "D21",
  "requirement": "coverage_floor",
  "source_sha256": "<sha256>",
  "source": "output/baselines/rung0_momentum_<YYYYMMDD>.json",
  "excluded_pct": <pct>,
  "floor_pct": 5.0,
  "accepted_at": "<today>",
  "direction": "survivorship-biased upward by an unmeasured amount -- the same structural gap D18/D20 accepted, measured on the same bars and membership, so the same figure",
  "re_evidence": "3 years of continuous forward-captured daily bars for the whole index, then re-run the baseline over that window -- unchanged from D18, dated from the 2026-08-18 capture start (2029-08-18)",
  "doc": "docs/designs/project-direction.md",
  "note": "The Rung-0 baseline of record (KAN-60): momentum only, whole shares, $1 commission floor, USD 3,700, rebuilt from the D20 bars with no IB time. Coverage is a property of membership and bars, not of which sleeves ran, so it inherits D20's exclusion exactly; a new sha256 still needs its own entry because an acceptance never widens. Pinned as divergence.rung0_baseline_pin. It is the reference for Rung-0 economics and divergence thresholds (rung0-economics 9.6), not the edge-evidence baseline and not the nightly drift feed (D19)."
}
```

2. `config/default.yaml`, in `divergence:` after `baseline_pin`:

```yaml
  # The Rung-0 baseline of record (KAN-60, D21): momentum only, whole shares,
  # the $1 floor, USD 3,700. Reference for Rung-0 economics and divergence
  # thresholds — NOT the edge pin above and NOT the nightly drift feed (the
  # rolling shadow, D19). Resolve with scripts/ops/baseline_pin.py --rung0.
  rung0_baseline_pin: output/baselines/rung0_momentum_<YYYYMMDD>.json
```

3. `docs/designs/project-direction.md`: add a D21 row to the decision table after D20 (`| D21 | Rung 0 needs a capital-specific baseline, and daily drift moved to the shadow | **DECIDED <date>: one artifact for evidence, the whole-share shadow for drift** (KAN-60). ... |`), and a section `## The Rung-0 baseline of record (D21)` after the D20 section, covering: the artifact and how it was generated (command); the numbers block (as D20's, from Step 1); why a new acceptance (never widens); **the D19 addendum** — AC3 restated: the Rung-0 daily-drift instrument is the whole-share momentum shadow, filed under its own `shadow:` id (whole-share sizing is part of the id), switched on together with paper whole-share sizing by KAN-33; the 20-session Rung-0 divergence-OK window starts from the first real run on that shadow, and §9.6(4) holds — it does not transfer to a multi-sleeve book at a later rung; what it does not rescue (same bias as D18/D20).

4. `docs/operations/backtest-baseline.md`: after "The pinned baseline of record" section, add `### The Rung-0 baseline of record (KAN-60)` naming `output/baselines/rung0_momentum_<YYYYMMDD>.json`, the regeneration command, the pin key, `baseline_pin.py --rung0`, and why the name avoids `backtest_multi_*` (90-day prune recursion, `record_epoch._newest_baseline`, stale-age check).

5. `docs/operations/rung0-economics.md` §9.6(2): append "**Done <date> (KAN-60):** `output/baselines/rung0_momentum_<YYYYMMDD>.json`, pinned as `divergence.rung0_baseline_pin`, coverage accepted as D21. Daily drift at Rung 0 is the whole-share shadow (D19/D21), switched on by KAN-33."

- [ ] **Step 5: Run to verify they pass**

Run: `python -m pytest tests/docs/ tests/backtest/test_bias_acceptance.py tests/ops/ tests/deploy/test_backtest_refresh_snapshot.py tests/deploy/test_baseline_age.py -v`
Expected: all PASS (the on-disk D21 check may SKIP in the worktree; paste the one-off admissibility output instead).

- [ ] **Step 6: Commit**

```bash
git add research/bias_acceptances.json config/default.yaml docs/designs/project-direction.md docs/operations/backtest-baseline.md docs/operations/rung0-economics.md tests/docs/test_rung0_baseline_decision.py
git commit -m "feat(KAN-60): pin the Rung-0 momentum baseline and accept its coverage bias (D21)"
```

---

### Task 5: verify, deploy-clone copy, PR

- [ ] **Step 1:** Full suite: `python -m pytest -q` — paste the summary line. No PR on red.
- [ ] **Step 2:** `git fetch origin develop && git rebase origin/develop`, re-run the touched test files.
- [ ] **Step 3:** Operator step (ask the user first): copy the artifact to `~/algo-poc-deploy/output/baselines/` (new file; `mkdir -p` the dir; never overwrite).
- [ ] **Step 4:** Self-review via superpowers:requesting-code-review; push; `gh pr create --base develop --title "KAN-60: Build the Rung-0 capital-specific divergence baseline artifact"`, body with what/why/testing, the AC3 restatement, the operator step, and the JIRA link.
- [ ] **Step 5:** `gh pr checks <url> --watch`; on green, transition KAN-60 to Done and comment the PR link.
