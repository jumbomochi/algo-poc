# KAN-60 — Rung-0 capital-specific divergence baseline: design

**Issue:** https://huiliang.atlassian.net/browse/KAN-60 (P2-24)
**Status:** approved in chat 2026-09-25 (scope option "artifact + shadow")

## Why the ticket's AC3 is restated

KAN-60 was written 2026-08-20. D19 (2026-09-04) moved the nightly monitor off
the pinned artifact onto a rolling shadow (`output/shadow_<date>.json`), and
`divergence_daily.baseline_id` is now the shadow's `shadow:<hex>` id. AC3 as
written ("`divergence_daily` rows carry this artifact's `baseline_id`") is
reachable only by re-coupling the monitor to a pinned artifact — the frozen
window D19 exists to prevent. The ticket's purpose, a capital-appropriate
measurement instrument for Rung 0, splits along D19's own line:

| | Rung-0 instrument | Delivered by |
|---|---|---|
| Edge / economics evidence, divergence thresholds (§9.6(3)) | pinned artifact | this ticket |
| Daily drift | momentum-only, whole-share shadow | plumbing here; switched on by KAN-33 |

## 1. The artifact

`scripts/run_backtest.py` gains:

- `--sleeves NAME [NAME ...]` (choices: `ACTIVE_SLEEVES`, default all six).
  Allocations are renormalised over the selection, so `--sleeves momentum
  --capital 3700` gives momentum the whole USD 3,700 (D8). The six fractions,
  currently duplicated literals per sleeve, become one table. Omitting the flag
  is byte-identical to today.
- A run with `--sleeves` always writes the multi-portfolio envelope. Without
  this, a one-sleeve run takes `save_results` (`len(portfolios) == 1`) and
  writes the single-portfolio schema, which nothing downstream reads.
- `--output PATH`: exact file path, parent created. Overrides `--output-dir`
  naming.

Generated with no IB time, from the D20 bars:

```bash
python scripts/run_backtest.py --years 10 --capital 3700 --whole-shares \
    --sleeves momentum \
    --bars-from-json output/backtest_multi_20260915_102125.json \
    --universe-snapshots data/universe/sp500_membership.json \
    --output output/baselines/rung0_momentum_<YYYYMMDD>.json
```

The name deliberately does not match `backtest_multi_*`: the 90-day prune
(`find` recurses into subdirectories), `record_epoch._newest_baseline` and the
stale-age check all glob that pattern, and none should ever see this file.

## 2. Pin and coverage

- `DivergenceConfig.rung0_baseline_pin: str | None`, set in
  `config/default.yaml`. `scripts/ops/baseline_pin.py --rung0` resolves it
  with the same rules as the existing pin (env override
  `ALGO_RUNG0_BASELINE_PIN`, absolute path out, exit 1 when unset).
- Not an edge-verdict baseline: `run_sleeve_evaluation.py` still refuses an
  artifact missing any incumbent sleeve. Unchanged.
- Coverage is a property of membership and bars, not sleeves, so it reads
  `BLOCKED`, expected at D20's figures (checked on the generated file,
  not assumed). Recorded as **D21**: an entry in
  `research/bias_acceptances.json` pinned to the new sha256, and a D21 section
  in `docs/designs/project-direction.md`. Merging the PR is the acceptance.
  The entry also puts the file under `protected_artifacts`.
- `docs/operations/backtest-baseline.md` names it the Rung-0 artifact of
  record; `rung0-economics.md` §9.6(2) points at it.

## 3. The shadow

- `produce_shadow_artifact(..., whole_shares=False)` passes it to
  `build_shadow_series`.
- `shadow_id_for` includes `whole_shares` in the identity **only when true**,
  so today's ids are byte-identical and no evidence row moves, while a
  whole-share shadow can never share a `baseline_id` with a fractional one.
- Nothing is switched on in production: the paper book still sizes
  fractionally, and a whole-share shadow against it would manufacture drift.
  KAN-33 flips paper and shadow together.
- Amended AC3, verified by test: a momentum-only whole-share shadow gets its
  own `shadow:` id, and `divergence_daily` rows persisted from it carry that id.
  The 20-session Rung-0 window starts from the first real run after KAN-33;
  §9.6(4) (no transfer to a multi-sleeve book) is restated unchanged.

## Testing

TDD per unit: `--sleeves` renormalisation and envelope choice, `--output`,
default-run invariance; `rung0_baseline_pin` resolution; D21 registry entry
resolves `VALID_WITH_ACCEPTED_BIAS` against the on-disk artifact (skipped when
absent, like the D18/D20 on-disk checks); `shadow_id_for` invariance and
distinctness; shadow `whole_shares` reaching `replay_window`; doc guards for
the D21 section and the artifact-of-record line.

## Operator step

`output/` is gitignored. Copy the artifact into
`~/algo-poc-deploy/output/baselines/` (new file, nothing overwritten).

## Out of scope

Switching paper/shadow to whole shares or one sleeve (KAN-33); the pin
mechanism (KAN-51); the PIT sourcing decision (KAN-59/D18).
