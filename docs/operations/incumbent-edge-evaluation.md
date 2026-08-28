# Incumbent Sleeve Edge Evaluation

**Status:** protocol adopted 2026-08-17 (KAN-40); verdicts of record **RECORDED
2026-08-28 — all six sleeves FAIL**, on a baseline carrying the accepted D18
coverage bias. The `incumbent_sleeves_2026` holdout is spent. See
[Verdicts](#verdicts).
**Owner:** Huiliang Lui (operator)
**Governs:** the six sleeves in `shared/universe.py::ACTIVE_SLEEVES`
**Rule this implements:** [project-direction.md](../designs/project-direction.md)
D10 — *each incumbent sleeve formally evaluated by the edge framework before
Rung 0 arms.*

Related: [edge-validation framework](edge-validation-framework.md) ·
[per-sleeve kill criteria](sleeve-kill-criteria.md) ·
[backtest baseline](backtest-baseline.md) ·
[divergence monitor](divergence-monitor.md) ·
[mean-reversion failure analysis](../strategies/mean-reversion-failure-analysis.md)

---

## Divergence-fidelity is not edge

The divergence monitor establishes that live execution reproduces the baseline
backtest. This evaluation establishes whether the baseline had an edge to
reproduce. **Neither substitutes for the other**, and a sleeve needs both.

This is worth stating flatly because the two are easy to conflate on gate day.
A sleeve can track its baseline to within a basis point for sixty sessions and
have no edge at all — perfect fidelity to a curve that was fitted. Equally, a
sleeve with a real edge can breach divergence because the implementation is
wrong. The first failure mode is the one this document exists for; the second
belongs to [sleeve-kill-criteria.md](sleeve-kill-criteria.md).

## Why the incumbents need this at all

The six sleeves were selected post-hoc. Eight were built; `mean_reversion` and
`short_term_mr` were dropped on **2026-05-26** after both posted negative
trade-level expectancy over the 9.97-year backtest, and their combined $22K
allocation was redistributed across the survivors (each surviving weight scaled
by 100/78). The record is in the code: the comment block above
`scripts/run_paper.py::CAPITAL_ALLOCATIONS` (`run_paper.py:98-103`), mirrored
above `shared/universe.py::ACTIVE_SLEEVES` (`universe.py:80-90`), with the
analysis in
[`docs/strategies/mean-reversion-failure-analysis.md`](../strategies/mean-reversion-failure-analysis.md).

Selecting six survivors out of eight candidates and then reporting the
survivors' Sharpe is exactly the bias deflated Sharpe corrects for. Being
grandfathered into the pipeline via epoch v2 (direction D9) is not an
exemption.

## The declared trial count is 8

Every deflation in this evaluation uses **n_trials = 8** — the eight candidate
sleeves searched on the way to six. The count is not hardcoded; it is read from
the sleeve-selection entry in `research/trial_registry.json`, whose `source`
field cites the failure analysis above.

It is *scoped* to that entry rather than taken from the registry total, which
also carries the 2026-08-02 native-factor search (4 more). A sleeve chosen in
May 2026 was not selected against factors specified in August, so counting
them would be over-deflation — a false negative at the gate, which the
framework doc names as a real cost rather than free safety margin
([edge-validation-framework.md](edge-validation-framework.md), "the sum is
unscoped").

Eight is a floor, not a ceiling. It counts sleeves that reached a backtest, not
the parameterizations tried inside each one. `--n-trials` overrides it upward.

## The method

`scripts/run_sleeve_evaluation.py` is the bridge. `research/evaluation` scores
*factors* — it ranks names cross-sectionally and measures the forward return of
the top quantile — while a sleeve is a strategy that already made its own
sizing and timing decisions and produced one equity curve. Everything
downstream of that curve is the same for both, so the driver converts curves to
return series and feeds the existing machinery rather than duplicating it.

It lives in `scripts/` because `research/` may not import `backtest` or
`scripts` (`tests/research/test_architecture.py`). The boundary is crossed as a
file — a *mapping file* of return series plus declared conventions — the same
way `--bars-from-json` and `run_stability_sweep.py` already cross it.

| Instrument | Source | Passing bar |
|---|---|---|
| Deflated Sharpe (DSR) | `research/evaluation/multiple_testing.py` `control(..., n_trials=8)` | `passes_dsr` at 0.95 |
| Probabilistic Sharpe (PSR) | `probabilistic_sharpe(..., sr_star=0)` | reported; the input to the FDR p-value |
| BH-FDR across the six | `benjamini_hochberg` at q = 0.10 | `passes_fdr` |
| Holdout performance | `research/evaluation/holdout.py`, split `incumbent_sleeves_2026` | registered before the look, spent once |
| Parameter stability | `research/evaluation/stability.py` fed by `scripts/run_stability_sweep.py` | `is_plateau` for the named parameter |

**The p-value for the FDR step** is `1 − PSR(sr, n, skew, kurt, 0)`: the
probability the sleeve's true Sharpe is *not* above zero, computed with the
same skew and kurtosis adjustment as the deflation. The factor path's Gaussian
IC t-test would be wrong here — a sleeve has no cross-sectional information
coefficient, and strategy returns are the case Bailey & López de Prado's moment
correction exists for.

### Three conventions the bridge normalizes

`backtest` and `research` disagree, and every disagreement is silent:

1. **Max-drawdown sign.** `backtest.metrics._max_drawdown` returns a positive
   fraction (`+0.162`); `research.evaluation.metrics.max_drawdown` returns a
   negative one (`-0.162`). Forwarding the number unchanged inverts every
   "worse than" comparison downstream. The mapping file is negative-convention
   throughout and says so in its `conventions` block.
2. **Annualization.** `backtest` uses a fixed 252; `research` takes
   periods-per-year from the caller, and the factor evaluator passes
   `252 / horizon` (12 at the default 21-day horizon). Daily sleeve returns
   need 252 — passing the factor evaluator's value would inflate every sleeve
   Sharpe by about 4.6x.
3. **The day-0 peak.** `research.max_drawdown` builds its equity curve from the
   returns alone, so the curve starts at the *first return* and the initial
   capital is never a candidate peak. A sleeve whose worst point is a day-one
   fall measures **zero** drawdown through that path. The bridge pads a leading
   `0.0` return to reinstate day 0, which is what makes the two paths agree
   exactly.

A fourth alignment matters as much: `portfolio_values` carries the pre-session
initial capital, so it is one longer than `dates`. Zipping the two naively
shifts every return back one session — a one-day look-ahead through the entire
evaluation. The bridge refuses a curve whose length does not match rather than
guessing which end to drop. It also refuses an artifact whose six sleeves do
not share one date index: the holdout is resolved once and its *indices* slice
every sleeve, so a sleeve on a shorter calendar would be reported under
another's session count.

The first three are covered by parity tests against the real implementations in
`tests/scripts/test_sleeve_evaluation.py` rather than against hand-written
expectations; the length alignment is a direct assertion on a three-session
curve, since there is no second implementation to compare it to.

**The full-sample Sharpe contains the holdout.** So the evaluation also reports
`in_sample.sharpe` over the purged training span, which is the honest
comparator for `holdout.sharpe`. Reading the full-sample number as the
in-sample one overstates the contrast in whichever direction the holdout ran.

### The holdout, and its two limits

`incumbent_sleeves_2026` starts **2026-06-01**, horizon 21, embargo 21 (gap 42).
The six-sleeve set was fixed on 2026-05-26, so no bar from June onward was part
of the search that produced it. Both limits belong in any citation of it:

- **Registered 2026-08-16, after the window opened.** That makes it
  out-of-*search*-sample, not out-of-sight-sample — the paper book ran over
  June–August and was watched daily. It is the cleanest split available, not a
  clean one.
- **Short, and growing.** Against a baseline ending 2026-08-03 it is 44
  sessions. Report the length beside the result; a window too small to be
  significant is evidence of nothing. The driver prints the warning itself
  below 60 sessions.

The split is single-use and the burn is recorded in
`research/holdout_registry.json`. **Commit the burn** — a holdout whose use is
not in git is a holdout on the honour system.

### The named load-bearing parameter, per sleeve

Named here, *before* any sweep, so the memo cannot pick a convenient parameter
after seeing the surface. The table is pinned against `ACTIVE_SLEEVES` in
`scripts/run_sleeve_evaluation.py::SLEEVE_PARAMETERS` and tested, so adding a
seventh sleeve fails the suite rather than producing a memo that quietly covers
six of seven.

| Sleeve | Parameter | Shipped | Why this one |
|---|---|---|---|
| `momentum` | `lookback_days` | 126 | The ranking window is the signal; every holding decision is a function of the 126-day return and nothing else. |
| `sector_rotation` | `lookback_days` | 63 | Same ranking window, one quarter long — it decides which three sector ETFs are held. |
| `thematic_momentum` | `lookback_days` | 63 | Ranks the thematic ETF basket; the replacement policy only reshuffles what this window already scored. |
| `quality_value` | `top_n` | 15 | A fundamental ranking with no lookback — breadth is the knob deciding how far down the ranking capital goes. |
| `earnings_drift` | `surprise_threshold_pct` | 5.0 | The surprise cutoff *is* the entry signal; every other parameter shapes an already-taken position. |
| `tail_risk_hedge` | `position_size_pct` | 0.25 | Entries are driven by the external regime series, so hedge size is the only parameter the sleeve itself owns. |

Values mirror the shipped call sites at `scripts/run_backtest.py:2310-2369`.

**Only two of the six can be swept today.** `scripts/run_stability_sweep.py`
covers `momentum` and `sector_rotation`; `thematic_momentum`, `quality_value`,
`earnings_drift` and `tail_risk_hedge` additionally need the regime series, the
fundamentals cache or the earnings cache, and KAN-39 left that wiring out
deliberately rather than half-done — a sweep against a missing cache would
silently measure a sleeve that never trades. The evaluation reports those four
as `stability.available = false` with the command that would produce the
surface. **An unmeasured parameter is a different verdict from a flat one**, and
the four are not to be read as passing stability. Wiring them in is tracked
separately; a sleeve carrying an unmeasured parameter into Rung 0 is an
operator decision that has to be logged as one.

## What a failing verdict means — decided now, not under pressure

**A sleeve that fails DSR is not deleted.** Deciding this in advance is what
stops the evaluation from being renegotiated when a favourite sleeve fails.

- The verdict is **recorded**, per sleeve, in this document and in the
  evaluation artifact.
- The **recommended stage** is one step down the ladder (`live → paper →
  shadow`), never two, and never retirement. The six sit at `paper` today, so a
  failing sleeve's recommended stage is `shadow`.
- **The operator decides.** Nothing moves automatically. Retirement (deletion)
  requires a separately logged decision, per direction D3.3, and demoted
  sleeves may re-earn promotion through the pipeline.
- This is the same ladder [sleeve-kill-criteria.md](sleeve-kill-criteria.md)
  uses. Per direction D16 one event produces one response: a sleeve that fails
  the edge framework *and* trips a kill trigger in the same week is demoted
  once, not twice.

## Verdicts

**RECORDED 2026-08-28. All six sleeves FAIL.** The single-use
`incumbent_sleeves_2026` holdout was spent on this run and cannot be spent
again; the burn is recorded in `research/holdout_registry.json`.

### The limitation this verdict rests on (D18)

**Every number below comes from a survivorship-biased baseline, and the
verdicts inherit that bias.** Per [D18](../designs/project-direction.md), the
point-in-time baseline cannot meet the coverage floor from IB data: 142,856 of
1,265,893 membership-days (11.28%, across 164 tickers) could not be priced
against a **5.00%** floor. `output/backtest_multi_20260819_183451.json`
therefore reports `coverage.state: BLOCKED` and `is_like_for_like` False, and
the floor was deliberately *not* moved to make it pass.

Index departures skew toward underperformers, so excluding roughly a tenth of
membership-days **flatters** these returns rather than merely adding noise —
survivorship-biased upward by an unmeasured amount. "Unmeasured" is exact:
sizing the bias would require the very history that is missing.

This run was admissible **only** because that bias was accepted in writing
beforehand, pinned to this artifact's sha256 in
`research/bias_acceptances.json`. `gate_valid` reads
`VALID_WITH_ACCEPTED_BIAS`, not `VALID`. Re-evidence after 3 years of
continuous forward-captured daily bars for the whole index.

The bias runs **against** these verdicts, not with them: a flattered baseline
makes a sleeve easier to pass, and all six failed anyway. Correcting for it
would push the numbers down, not up — so the FAILs are, if anything,
understated. A future PASS on a comparable baseline would be the finding that
needs re-examining, not these.

### Provenance

| | |
|---|---|
| Evaluation artifact | [`output/edge/sleeve_evaluation_20260828_033747.json`](../../output/edge/sleeve_evaluation_20260828_033747.json) — **tracked** |
| Returns mapping | [`output/edge/sleeve_returns_20260828_033747.json`](../../output/edge/sleeve_returns_20260828_033747.json) — **tracked** |
| Stability surfaces | [`momentum`](../../output/stability/momentum-lookback_days.json), [`sector_rotation`](../../output/stability/sector_rotation-lookback_days.json) — **tracked** |
| Baseline | `output/backtest_multi_20260819_183451.json` (sha256 `19e130ad…f480136`) — **not tracked**, 249MB |
| Git revision | `c31233ab5c204a2658183044fc890e6d0709091b` |
| `gate_valid` | `VALID_WITH_ACCEPTED_BIAS` |
| Trials deflated against | 8 (`n_trials`), DSR threshold 0.95, BH-FDR q = 0.10 |
| Holdout | `incumbent_sleeves_2026`, 2026-06-01 → 2026-08-18, **55 sessions**, 42-session purge/embargo gap |

**Why these four are committed despite `output/` being gitignored.** A single-use
holdout was spent to produce them and cannot be spent again, so they are not
regenerable output — re-running the driver on this window is no longer possible.
They are force-added as evidence of record, and `.gitignore` documents the
exception. The 249MB baseline stays out; the returns mapping carries its
`source_sha256`, so the chain from baseline to verdict is checkable in-repo even
though the baseline itself is not.

The one thing this does **not** preserve: with the baseline absent, the returns
mapping cannot be re-derived from source here. Verifying that the mapping
faithfully represents the baseline requires the operator's copy of the 249MB
artifact, matched by sha256.

### The verdicts

| Sleeve | Sharpe (full) | Max DD | PSR | DSR | FDR | In-sample SR | Holdout SR (55 sessions) | Stability | Verdict | Recommended stage |
|---|---|---|---|---|---|---|---|---|---|---|
| `momentum` | 0.59 | −24.0% | 0.967 | 0.358 | pass | 0.55 | +0.24 | plateau | **FAIL** | `shadow` |
| `sector_rotation` | 0.62 | −21.5% | 0.974 | 0.401 | pass | 0.60 | −0.26 | plateau | **FAIL** | `shadow` |
| `thematic_momentum` | 0.68 | −36.9% | 0.983 | 0.473 | pass | 0.62 | −0.90 | not swept | **FAIL** | `shadow` |
| `quality_value` | 0.32 | −17.9% | 0.848 | 0.113 | fail | 0.03 | +4.63 | not swept | **FAIL** | `shadow` |
| `earnings_drift` | 0.50 | −14.4% | 0.942 | 0.262 | pass | 0.54 | +0.88 | not swept | **FAIL** | `shadow` |
| `tail_risk_hedge` | −0.60 | −20.0% | 0.030 | 0.000 | fail | −0.54 | −4.78 | not swept | **FAIL** | `shadow` |

`Sharpe (full)` is the whole sample and *contains* the holdout; `In-sample SR`
is the purged training span and is the honest comparator for `Holdout SR`.
Verdict is `passes_dsr AND passes_fdr`.

### How to read this

**DSR is what kills them, and it kills them by a distance.** Every sleeve lands
between 0.000 and 0.473 against a 0.95 threshold. This is not a set of
borderline calls: no sleeve is one good quarter away from passing. Four of six
clear BH-FDR and still fail, which is the same pattern the rehearsal showed —
FDR is close to inert across six sleeves with a decade of data, and DSR carries
the decision.

**The 55-session holdout is too short to carry weight in either direction.**
The driver says so itself, and the column proves it: `quality_value` posts a
holdout SR of **+4.63** against an in-sample 0.03, which is not a sleeve that
works but a 55-session window doing what short windows do. The four negative
holdout SRs are weak evidence for the same reason. Read the holdout column as
context, not as the verdict — the verdicts rest on DSR.

**Four of six have no stability surface.** `run_stability_sweep.py` covers only
the sleeves whose signals need nothing but bars; `thematic_momentum`,
`quality_value`, `earnings_drift` and `tail_risk_hedge` additionally need the
regime series, the fundamentals cache or the earnings cache. Their `Stability`
is unmeasured, not passed. It changes no verdict here — all four fail on DSR
regardless — but a future re-run that turns a FAIL into a PASS cannot lean on
an unmeasured surface.

**Nothing moves automatically.** Per direction D3.3 and the ladder above, the
recommended stage is one step down (`paper → shadow`) for each, never
retirement. The operator decides; a demoted sleeve may re-earn promotion.

### How a non-admissible baseline is refused

Retained as reference, since the distinction is what made this run possible. A
baseline that is not like-for-like is **refused** with exit 3 unless
`--allow-non-comparable-baseline` is passed, which stamps `gate_valid: INVALID`
on the output — and even then it requires an explicit `--holdout-registry`, so
a look at gate-invalid numbers cannot spend the split of record by default.

There is one narrow exception, and it is not that flag. A baseline whose *only*
unmet requirement is the coverage floor, and whose bias is accepted for that
exact artifact in `research/bias_acceptances.json`, resolves
`VALID_WITH_ACCEPTED_BIAS` and may be evaluated — see
[D18](../designs/project-direction.md). The two are not interchangeable: the
override is blanket and never citable, the acceptance is pinned to one sha256
and excuses nothing but coverage. A same-bar baseline holding a valid
acceptance is still refused.

### Rehearsal — not evidence, not citable

Run 2026-08-17 against the pre-rebaseline artifact with
`--allow-non-comparable-baseline` and a **copy** of the holdout registry, to
prove the bridge produces sane output end to end. Every number below is
inflated by survivorship and same-bar fills. **Do not cite these as verdicts,
and do not carry them into the table above.**

"FDR" below is the Benjamini–Hochberg step alone; the **Verdict** column is
`passes_dsr AND passes_fdr`, which is what the driver reports and what the real
table records. Five of the six clear FDR and fail on DSR.

| Sleeve | Sharpe (full) | Max DD | PSR | DSR | FDR | In-sample SR | Holdout SR (44 sessions) | Verdict |
|---|---|---|---|---|---|---|---|---|
| `momentum` | 1.49 | −16.2% | 1.000 | 0.868 | pass | 1.48 | +1.16 | FAIL |
| `sector_rotation` | 1.90 | −15.6% | 1.000 | 0.993 | pass | 1.90 | −0.10 | PASS |
| `thematic_momentum` | 1.73 | −10.2% | 1.000 | 0.967 | pass | 1.71 | −0.73 | PASS |
| `quality_value` | 0.87 | −11.1% | 0.998 | 0.191 | pass | 0.64 | +3.01 | FAIL |
| `earnings_drift` | 1.55 | −6.5% | 1.000 | 0.913 | pass | 1.61 | +1.06 | FAIL |
| `tail_risk_hedge` | −0.18 | −14.6% | 0.288 | 0.000 | fail | −0.09 | −4.67 | FAIL |

Two things the rehearsal already shows, which the real run should be read
against. First, **the deflation bites**: `quality_value` clears the raw Sharpe
bar comfortably and collapses to 0.19 once deflated against a search of eight —
that gap is the whole point of the exercise. Second, **BH-FDR is close to
inert here**: with 2,510 daily observations the p-values are ~1e-6 and every
sleeve but one passes trivially. FDR is doing real work across a wide factor
sweep; across six sleeves with a decade of data it is close to a formality, and
DSR plus the holdout carry the decision.

## Producing the verdicts of record

1. Regenerate the baseline with next-open fills, the commission floor and a
   point-in-time universe — [backtest-baseline.md](backtest-baseline.md).

   `config.coverage.state` will read **`BLOCKED`, not `OK`**, and that is
   expected: D18 accepted the PIT coverage bias permanently and deliberately
   left the 5.00% floor unmoved, so `OK` is unreachable from IB data. What must
   be true instead is that `research/bias_acceptances.json` carries an
   acceptance pinned to *this* artifact's sha256 whose figures still match its
   coverage block. A `BLOCKED` baseline with no matching acceptance is still not
   a pass, and the driver refuses it with exit 3.
2. Sweep the two available parameters:
   ```
   python scripts/run_stability_sweep.py --sleeve momentum \
       --parameter lookback_days --grid 100,113,126,139,152 --center 126 \
       --bars-from-json data/cache/bars.json \
       --universe-snapshots data/universe/sp500_snapshots.json \
       --out output/stability/momentum-lookback_days.json
   python scripts/run_stability_sweep.py --sleeve sector_rotation \
       --parameter lookback_days --grid 42,52,63,74,84 --center 63 \
       --bars-from-json data/cache/bars.json \
       --universe-snapshots data/universe/sp500_snapshots.json \
       --out output/stability/sector_rotation-lookback_days.json
   ```
3. Run the evaluation — this **spends** `incumbent_sleeves_2026`:
   ```
   python scripts/run_sleeve_evaluation.py \
       --backtest output/backtest_multi_<new>.json \
       --stability-dir output/stability \
       --output-dir output/edge
   ```
   The driver validates every stability surface — sleeve, parameter and
   center — *before* it spends the holdout, so a mis-typed `--center` costs a
   re-run rather than the split. It refuses a surface centered anywhere but the
   shipped value, and taints `gate_valid` if a surface was swept without
   `--universe-snapshots`.
4. Confirm `gate_valid` in the artifact reads `VALID` or
   `VALID_WITH_ACCEPTED_BIAS`. `INVALID` means the run is not admissible for
   Rung 0, whatever the numbers say — and note that all three are non-empty
   strings, so this is a comparison, never a truthiness check.

   A `VALID_WITH_ACCEPTED_BIAS` run is citable **only with the citation**. The
   artifact's `baseline.accepted_bias` block carries what that citation needs:
   the decision (D18), the 11.28% exclusion, the 5.00% floor, and the direction
   of the bias. Per D18, a verdict that spends the single-use holdout without
   citing the limitation is not a valid verdict.
5. Fill the verdict table above from the artifact, and commit both it and the
   holdout burn in `research/holdout_registry.json`.
6. Record the result against the gate. **Rung 0 does not arm without this
   section filled in** (direction D10).

## Known limits

- **The deflation under-corrects.** `n_trials` supplies the count, but the
  spread of trial Sharpes is estimated from the six that *survived*. The
  dropped tail is counted in `m` and absent from the sample, so `SR*` is too
  small. The residual error is at least in the known direction.
- **Eight counts sleeves, not parameterizations.** Every lookback and threshold
  tried inside a sleeve is an untracked trial. The true search is larger than 8.
- **The holdout is out-of-search-sample only**, and 44 sessions long today.
- **Four of six parameters are unmeasured for stability**, as above.
- **This evaluation judges the backtest, not the live book.** It says whether
  the strategy had an edge over 2016–2026. Whether the live implementation
  delivers it is the divergence monitor's question — see the top of this page.

## What is not done yet

Stated plainly so the gate review is not surprised by it:

- **The verdicts.** Blocked on a like-for-like baseline, which is an operator
  run, not a code change. The protocol, the tooling and the decision rules are
  in place; the numbers are not.
- **Four of six stability surfaces.** `thematic_momentum`, `quality_value`,
  `earnings_drift` and `tail_risk_hedge` cannot be swept until
  `run_stability_sweep.py` is wired to the regime series, the fundamentals
  cache and the earnings cache. Their named parameters are recorded above; the
  measurements are not.

Neither gap is silent: the driver reports `stability.available: false` per
sleeve, and the verdict table records those four as `not swept` rather than as
passed. The table was filled on 2026-08-28 by an admissible run
(`gate_valid: VALID_WITH_ACCEPTED_BIAS`); **Rung 0 does not arm on an empty
table**, and it does not arm on this one either — all six sleeves failed.
