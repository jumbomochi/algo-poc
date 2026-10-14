# Backtest position source: replay sleeves on their own fills (KAN-116)

**Decided 2026-10-15.** The operator approved option 1 as recommended.
Implemented in the KAN-116 PR. All figures compare `origin/main` at `6b93d6a`
(before) with the KAN-116 branch (after), on the same inputs. Every
measurement was re-run on the final code, not on the investigation's
prototype. The two agree exactly wherever their inputs did not change; see
[Reproducing](#reproducing).

Nothing in the live trading path changed.
`tests/scripts/test_live_roster_unchanged.py` proves it: the 05:15 roster
produces the same decisions on a fixed scenario on both commits.

**Data handling.**

- The bar sets came from saved refresh artifacts under `output/`. They were
  read, never written.
- IB was not contacted.
- No database was written. The parity test uses an in-memory sqlite paper
  state.
- The D10 statistics were computed against a **scratch copy** of
  `research/holdout_registry.json`. The `incumbent_sleeves_2026` split was
  spent on 2026-08-28, so its holdout figures here are a re-measurement, not
  new evidence. Nothing was spent.

## Decision

**Option 1: every replay drives its sleeves from a `PortfolioContext` of its
own filled lots, through the same code path live uses.**

- `backtest.portfolio_context.RunnerBook` stands in for a sleeve's
  `portfolio_context` in a replay.
  - **Binding.** Before each session's decisions, `BacktestRunner.run(book=...)`
    binds it to a `PortfolioContext` built from the runner's own lots. It is
    built by `backtest.runner._runner_context`, with the same fields
    `PaperTradingState.build_portfolio_context` hydrates live:
    - total quantity;
    - quantity-weighted fill price;
    - the high-water mark, which the runner raises at each close after
      decisions, as live's `update_peak_prices` does after its signals;
    - the first fill's session as `entry_date`;
    - any exit that could not fill, as a pending sell.
  - **Once per session.** It is bound once per session, so every ticker's
    decision that session reads the same book, as live's daily context does.
  - **Unbound books fail loudly.** Reading a `RunnerBook` before a runner has
    bound it raises.
- **Every sleeve reads a context.** The six sleeve factories in
  `scripts/run_backtest.py` no longer keep a private `tracked` record. Each
  `portfolio_context is None` branch is deleted, and a factory built without
  a context raises `TypeError`. So the backtest and live now run one signal
  path, not two.
- **Every replay path passes its books** (`PortfolioConfig.book`):
  - the weekly refresh (`run_backtest.py` `main()`, which feeds the pin and
    D10);
  - the rolling shadow (`build_portfolios(portfolio_contexts=None)` →
    `shadow_series.build_shadow_replays` → `replay_window_detail`);
  - the D10 stability surfaces (`run_stability_sweep.py`);
  - `validate_replacement_policy.py`.
- **Live is unchanged.** `build_portfolios(portfolio_contexts=<hydrated>)`
  creates no books.
- **New baseline_id.** `SHADOW_POSITION_SOURCE_VERSION = "runner_book"`
  enters the shadow fingerprint. Every verdict from the first run after
  deploy files under a new `baseline_id`: `shadow:89693c0029cad3e6` becomes
  `shadow:12d4b27466b26cd8` at today's parameters.
- **New diagnostic.** `BacktestResult.entry_funnel` counts what became of
  every buy signal: risk-refused, skipped for whole shares, skipped for
  sleeve cash, queued, filled, expired. The weekly artifact now carries it
  per sleeve.

**Re-pinning is the operator's decision, and this change makes none.**
`divergence.baseline_pin` still names `backtest_multi_20260915_102125.json`.
That pin was already unreproducible by current code before this change: it
predates KAN-109's NaN-surprise fix and KAN-111's cash funding. Its config
re-run on `6b93d6a` is in [the pinned baselines](#the-pinned-baselines).

## The defect

Every sleeve's signal function used to write `tracked[ticker]` just before
returning `"action": "buy"`. It recorded what it *asked* for, never what
filled. `BacktestRunner` then decided whether that buy happened at all. A buy
could fail to happen in four ways:

- **an unreached DAY limit**: entries are day orders at the decision close,
  and the next bar may never trade down to it;
- **a risk refusal**;
- **a KAN-111 sleeve-cash refusal**;
- **a crash-entry freeze**: `make_crash_freeze_signals_fn` dropped the buy
  *after* the inner function had already recorded it.

In every case the sleeve still believed it held the name. It never retried
the entry, and it only "exited" the phantom when the phantom's stop fired.

Phantoms also corrupted three other decisions:

- **Rank replacement.** Thematic momentum and quality value ran it over
  `set(tracked)`, so phantoms took part in replacement.
- **The tail-risk hedge's regime-at-entry** came from the signal date.
- **Earnings drift's 20-session clock** started at the signal bar, one session
  sooner than live's.

A quality-value holding whose fundamentals went missing could never exit,
because the legacy branch returned before evaluating any exit.

**Live never had this problem.** `scripts/run_paper.py` rebuilds every signal
function each morning from a `PortfolioContext` hydrated from the `positions`
table and the order ledger. That context carries everything the sleeves read:
quantity, fill price, peak (`max(peak_price, highest_price_since_entry)`),
`entry_date`, and pending orders. Nothing survives between runs. So a missed
entry is simply asked again the next session. Option 1 needs no state that the
book does not already hold.

Share of buy signals that never became a lot, before the change:

| replay | momentum | sector_rotation | thematic_momentum | quality_value | earnings_drift | tail_risk_hedge |
|---|---|---|---|---|---|---|
| 2y (2024-09-03..2026-10-08, signals gated before the start) | 19.6% | 31.0% | 29.0% | 35.3% | 48.7% | 31.5% |
| 10y weekly refresh (10-13 bars) | 26% | 26% | 27% | — (no fundamentals cache) | 33% | 23% |
| 10y, D10-of-record bars (08-19) | 28% | 26% | 28% | 31% | 50% | 22% |
| Rung-0 pin (momentum, USD 3,700, whole shares) | 66% | | | | | |

The 10y figures come from the artifact's own `entry_signals_sized`: every buy
the inner function emitted, crash-freeze suppressions included. Each is
measured against the lots opened.

## Options

| | **1. Runner book (chosen)** | 2. Reconcile `tracked` after each bar |
|---|---|---|
| Mechanism | each sleeve reads a `RunnerBook` the runner binds to its own filled lots before every session | the runner tells each signal function which tickers it holds, and the function drops the rest of `tracked` |
| Same position source as live | **yes**: the live context branch, fill-price entry, fill-date clock, live peak timing | no: a second code path keeps signal-close entries, the signal-bar earnings-drift clock, the signal-date tail-risk regime, and fundamentals-gated quality-value exits |
| Wrappers (crash freeze, ML filter or shadow) | stateless, so nothing to forward | every wrapper must forward `tracked`, or its own suppressions stay phantoms |
| Re-running the same signal function | identical results (no state survives a run) | the second pass inherits the first pass's book |
| Mis-wiring | an unbound book raises on first read | a missing reconcile call silently restores the phantoms |

**Option 2 was measured and rejected.** The investigation prototype
(`c2c202e`) implemented it as a reconcile hook. On the 2y window it closed
most of the exposure gap. But the semantic differences it keeps moved the
path-dependent sleeves by several points:

| sleeve | invested %: option 2 / option 1 | total return: option 2 / option 1 | Sharpe: option 2 / option 1 |
|---|---|---|---|
| momentum | 86.9 / 86.9 | +29.9% / +29.7% | 0.77 / 0.77 |
| sector_rotation | 90.4 / 89.1 | **+3.2% / −2.9%** | 0.19 / −0.07 |
| thematic_momentum | 76.9 / 77.0 | +75.0% / +74.9% | 1.46 / 1.46 |
| quality_value* | 91.2 / 91.6 | +32.4% / +28.5% | 1.03 / 0.96 |
| earnings_drift* | 30.6 / 30.9 | +11.9% / +10.9% | 0.79 / 0.75 |
| tail_risk_hedge | 22.6 / 23.1 | −5.0% / −3.9% | −0.62 / −0.48 |

Option 2 still fails acceptance criterion 2 (the backtest and live use the
same position source) by construction. A third option was also rejected:
rebuilding each signal function every bar with a fresh context, literally as
live does. Every factory pre-ranks the whole history, so that costs O(T²).
`RunnerBook` gets the same semantics at the cost of one context per session.

\* quality_value and earnings_drift on the 2y window use the repo checkout's
stale 2026-03-26 fundamentals and earnings caches. Their rows are structural,
not strategy evidence.

## Measurements

### 2-year replay (2024-09-03..2026-10-08)

Setup:

- the production `build_portfolios` roster, KAN-105's union-universe bars,
  and capital of 100,000 × each sleeve's allocation;
- signals are gated before the start, so no phantoms are carried in from
  before it.

Columns:

- **Never filled**: the share of buy signals that never became a lot.
- **Retries**: buy signals whose previous signal for the ticker did not fill.
  The number after the slash is how many of those then filled. Before the
  change, a "retry" happened only after a phantom's stop had fired.

| sleeve | never filled before → after | retries / filled on retry, before → after | mean invested % | total return | Sharpe | max DD |
|---|---|---|---|---|---|---|
| momentum | 19.6% → 24.0% | 21/15 → 43/27 | 81.4 → 86.9 | +35.4% → +29.7% | 0.92 → 0.77 | 23.2% → 24.5% |
| sector_rotation | 31.0% → 72.3% | 8/5 → 112/14 | 78.5 → 89.1 | −2.1% → −2.9% | −0.05 → −0.07 | 16.2% → 16.4% |
| thematic_momentum | 29.0% → 66.1% | 50/38 → 338/58 | 61.4 → 77.0 | +61.4% → +74.9% | 1.57 → 1.46 | 12.4% → 13.8% |
| quality_value* | 35.3% → 70.3% | 37/27 → 313/58 | 81.0 → 91.6 | +7.1% → +28.5% | 0.32 → 0.96 | 18.2% → 17.3% |
| earnings_drift* | 48.7% → 66.4% | 81/37 → 261/69 | 28.4 → 30.9 | +9.0% → +10.9% | 0.66 → 0.75 | 3.5% → 6.3% |
| tail_risk_hedge | 31.5% → 26.7% | 39/28 → 36/29 | 14.7 → 23.1 | −0.2% → −3.9% | −0.02 → −0.48 | 5.1% → 6.7% |

**Why the "never filled" share rises after the change.** Every session that a
qualifying entry is refused now produces a new buy signal, and none of them
leaves anything behind. A sleeve that is fully invested emits a buy every
session that its sleeve cash then refuses. Sector rotation is the clearest
case: it holds up to five ETFs at 20% each, because it exits only on stops.
Live does exactly the same: it emits the buy, and `size_to_sleeve_cash` skips
it. What matters is the invested column.

### 10-year weekly refresh and D10

**Method.** This is the weekly refresh's invocation from
`deploy/launchd/run_backtest_refresh.sh`:

```bash
run_backtest.py --years 10 --capital 100000 \
  --universe-snapshots <PIT membership> --allow-degraded-data
```

It loads the bars the refresh had already fetched (`--bars-from-json`), so IB
was not contacted. Each run took about 25 s. D10 is `run_sleeve_evaluation.py`'s
own `build_mapping` and `evaluate_mapping`:

- 8 declared trials;
- DSR threshold 0.95;
- BH-FDR at q = 0.10;
- the `incumbent_sleeves_2026` holdout, resolved against a scratch registry.

**The SR\* caveat.** DSR deflates every sleeve's Sharpe against SR\*, the
Sharpe a noise search of this size would win. SR\* is estimated from the
*spread* of the six sleeves' Sharpes (`expected_max_sharpe`). This change
narrows that spread: tail_risk_hedge becomes less negative and the strong
sleeves weaken. So SR\* falls, and **every reported DSR rises, even for
sleeves whose Sharpe fell**. The figure in parentheses removes that effect: it
is the after-Sharpe deflated against the *before* SR\*. Read that one for each
sleeve's own change. A degraded, flat quality_value (Sharpe 0) distorts the
spread the same way.

**A: the weekly refresh as it runs today**

Inputs: the bars of `backtest_multi_20261013_114528.json` and the deploy
clone's caches (no fundamentals cache; partial Alpha Vantage earnings).
Before the change, the run reproduces that artifact to the cent for every
sleeve except earnings_drift, whose cache was refreshed after it.

SR\* per period: 0.0494 → 0.0404. Holdout: 2026-06-01..2026-10-12, 93
sessions.

| sleeve | never filled (before) | cash-refused retries (after) | mean invested % | total return | Sharpe | max DD | PSR | DSR as reported (at before-SR\*) | holdout Sharpe | D10 |
|---|---|---|---|---|---|---|---|---|---|---|
| momentum | 26% (223/857) | 655 | 73.0 → 81.0 | +187.8% → +162.8% | 0.70 → 0.59 | 19.5% → 24.8% | 0.986 → 0.967 | 0.400 → 0.432 (0.269) | 0.91 → −0.05 | FAIL → FAIL |
| sector_rotation | 26% (45/170) | 300 | 76.8 → 84.5 | +77.1% → +90.2% | 0.58 → 0.61 | 21.7% → 22.6% | 0.965 → 0.971 | 0.266 → 0.461 (0.294) | −0.99 → −0.45 | FAIL → FAIL |
| thematic_momentum | 27% (265/968) | 593 | 51.9 → 64.2 | +122.1% → +139.0% | 0.67 → 0.63 | 39.5% → 47.5% | 0.982 → 0.976 | 0.364 → 0.490 (0.319) | 0.03 → 0.05 | FAIL → FAIL |
| quality_value (no cache) | — | 0 | 0 → 0 | 0 | 0 | 0 | 0.500 | 0.007 → 0.022 (0.007) | 0 | FAIL → FAIL |
| earnings_drift (degraded cache) | 33% (348/1053) | 311 | 32.4 → 39.2 | +52.6% → +56.0% | 0.64 → 0.57 | 9.7% → 16.8% | 0.978 → 0.964 | 0.327 → 0.418 (0.257) | 2.34 → 2.19 | FAIL → FAIL |
| tail_risk_hedge | 23% (110/476) | 0 | 18.4 → 25.5 | −19.6% → −16.2% | −0.63 → −0.42 | 21.2% → 18.8% | 0.024 → 0.092 | 0.000 → 0.000 (0.000) | −4.16 → −1.98 | FAIL → FAIL |

**B: the D10-of-record bars**

Inputs: the bars of `backtest_multi_20260819_183451.json`, the source of
`sleeve_evaluation_20260828`, with the main checkout's 2026-03-26 caches.
Those are the caches that run used, so quality_value and earnings_drift
trade.

SR\* per period: 0.0438 → 0.0362. Holdout: 2026-06-01..2026-08-18, 55
sessions.

| sleeve | never filled (before) | cash-refused retries (after) | mean invested % | total return | Sharpe | max DD | PSR | DSR as reported (at before-SR\*) | holdout Sharpe | D10 |
|---|---|---|---|---|---|---|---|---|---|---|
| momentum | 28% (234/847) | 721 | 77.2 → 83.6 | +137.3% → +132.2% | 0.56 → 0.51 | 20.7% → 26.1% | 0.961 → 0.945 | 0.340 → 0.419 (0.279) | 0.25 → −0.84 | FAIL → FAIL |
| sector_rotation | 26% (45/170) | 229 | 76.3 → 85.1 | +84.1% → +85.6% | 0.62 → 0.59 | 21.5% → 22.8% | 0.974 → 0.966 | 0.411 → 0.520 (0.371) | −0.26 → 0.60 | FAIL → FAIL |
| thematic_momentum | 28% (269/976) | 776 | 52.2 → 65.6 | +113.0% → +113.4% | 0.64 → 0.55 | 39.9% → 49.0% | 0.977 → 0.959 | 0.431 → 0.476 (0.330) | −0.89 → −0.89 | FAIL → FAIL |
| quality_value | 31% (39/126) | 159 | 82.6 → 91.7 | +16.8% → +21.5% | 0.32 → 0.36 | 17.9% → 17.5% | 0.848 → 0.869 | 0.118 → 0.249 (0.145) | 4.63 → 2.01 | FAIL → FAIL |
| earnings_drift | 50% (576/1147) | 933 | 38.5 → 42.8 | +40.5% → +51.3% | 0.53 → 0.59 | 14.3% → 15.6% | 0.952 → 0.967 | 0.304 → 0.520 (0.370) | 0.00 → 0.00 | FAIL → FAIL |
| tail_risk_hedge | 22% (106/473) | 0 | 20.3 → 25.5 | −18.5% → −16.3% | −0.60 → −0.42 | 20.0% → 18.8% | 0.030 → 0.091 | 0.000 → 0.001 (0.000) | −4.78 → −2.00 | FAIL → FAIL |

**What the D10 numbers say.**

- **Every sleeve still FAILs D10 on both bar sets.** The nearest is
  earnings_drift and sector_rotation at 0.520 as reported (0.370 against the
  before-SR\*), still far below 0.95.
- **FDR verdicts are unchanged.**
- **The plateaus are unchanged.** Momentum and sector_rotation lookback both
  remain plateaus (see below).
- **The KAN-33 hold is unaffected.**
- **On its own Sharpe, the fix lowers DSR** for momentum (0.400 → 0.269 and
  0.340 → 0.279) and thematic momentum (0.364 → 0.319 and 0.431 → 0.330). It
  lowers earnings_drift on the refresh (0.327 → 0.257) and sector_rotation on
  the 08-19 bars (0.411 → 0.371). Quality value (0.118 → 0.145) and
  earnings_drift on the 08-19 bars (0.304 → 0.370) rise.
- **The legacy backtest flattered** momentum and thematic momentum:
  - it showed higher Sharpe and lower drawdowns, because phantom lots kept
    those sleeves under-invested;
  - thematic momentum's 10y max drawdown is about 8–9 points worse once the
    sleeve holds what it would actually hold.

### The pinned baselines

The edge pin's config was re-run on its own bars: `backtest_multi_20260915_102125.json`,
PIT universe, main-checkout caches. The before column reproduces the KAN-111
re-measurement in `docs/operations/backtest-baseline.md` exactly. **The
pinned artifact itself (the "pin as stored" column) is unreproducible by
`6b93d6a` already**; see KAN-109 and KAN-111.

| sleeve | pin as stored | total return before → after | Sharpe | max DD | trades |
|---|---|---|---|---|---|
| momentum | +179.66% | +160.15% → +137.90% | 0.63 → 0.53 | 19.04% → 25.93% | 619 → 761 |
| sector_rotation | +71.51% | +71.37% → +70.43% | 0.54 → 0.50 | 22.00% → 23.41% | 118 → 156 |
| thematic_momentum | +129.63% | +112.11% → +116.81% | 0.63 → 0.56 | 40.12% → 48.65% | 696 → 916 |
| quality_value | +15.63% | +15.60% → +19.32% | 0.30 → 0.33 | 17.89% → 17.46% | 75 → 115 |
| earnings_drift | +38.48% | +40.48% → +53.35% | 0.53 → 0.61 | 14.33% → 15.69% | 569 → 671 |
| tail_risk_hedge | −19.91% | −19.91% → −16.40% | −0.65 → −0.42 | 20.20% → 19.50% | 364 → 368 |
| **aggregate** | | **+71.38% → +70.26%** | | | |

**The Rung-0 pin moves the most.** `rung0_momentum_20260925.json` (momentum
only, USD 3,700, whole shares) goes from +114.56% to +76.67%. Its Sharpe goes
from 0.54 to 0.39, its max DD from 19.90% to 30.05%, and its trades from 559
to 661.

At that capital, 66% of its buy signals never became a lot before the change.
That includes the entries `BacktestRunner` truncated to zero whole shares
after the risk check. Those, too, left a phantom in `tracked`, so the sleeve
sat in cash instead of re-trying. This bears on Rung-0 economics (D21,
KAN-60): the pinned Rung-0 numbers flattered a book that could not buy what
it signalled.

### Stability surfaces (D10 plateau check, 08-19 bars)

| surface | before: Sharpe at the five lookbacks (centre bold) | degradation | after | degradation | plateau |
|---|---|---|---|---|---|
| momentum lookback (100/113/**126**/139/152) | 0.44 / 0.60 / **0.56** / 0.49 / 0.47 | 10.5% | 0.52 / 0.52 / **0.55** / 0.47 / 0.51 | 7.9% | yes → yes |
| sector_rotation lookback (42/52/**63**/74/84) | 0.84 / 0.68 / **0.69** / 0.43 / 0.59 | 7.7% | 0.78 / 0.76 / **0.64** / 0.57 / 0.70 | −9.7% | yes → yes |

### Rolling shadow (KAN-105 replay, 11 windows ending 2026-09-23..10-08)

| sleeve | verdicts before → after | invested at window start | mean abs gap |
|---|---|---|---|
| momentum | 11 OK → 10 OK, 1 WARNING | 68% → 79% | 0.39 → 1.59 pp |
| sector_rotation | 11 OK → 11 OK | 71% → 93% | 0.79 → 1.10 pp |
| thematic_momentum | 7 BREACH, 3 WARNING, 1 OK → 8 BREACH, 1 WARNING, 2 OK | 78% → 98% | 3.89 → 4.71 pp |
| quality_value* | 10 OK, 1 WARNING → 9 OK, 2 WARNING | 79% → 85% | 1.26 → 1.91 pp |
| earnings_drift* | 9 OK, 2 BREACH → 11 OK | 78% → 74% | 1.67 → 0.80 pp |
| tail_risk_hedge | 11 OK → 11 OK | 16% → 22% | 0.14 → 0.40 pp |

The model side is now more invested at every window start.
thematic_momentum's BREACHes remain. The shadow now holds more than it did,
which widens the gap KAN-105 traced to live's own under-investment rather
than closing it.

\* Production does not replay quality_value or earnings_drift today
(KAN-109). Their rows use the main-checkout fundamentals and the deploy
clone's earnings cache, and are structural only.

## What the operator should expect after deploy

- **The rolling shadow restarts its `baseline_id`.** `shadow:89693c0029cad3e6`
  becomes `shadow:12d4b27466b26cd8`. `breach_streak` treats the older rows as
  history; nothing is deleted or relabelled.
- **The weekly refresh moves.** On today's bars, momentum's 10y return
  drops (+187.8% → +162.8%) and the other trading sleeves' rise. Drawdowns
  widen for momentum, thematic_momentum and earnings_drift. A refresh that
  "drops" is this model change, not decay.
  The artifact now carries `entry_funnel` per sleeve.
- **Re-pinning is the operator's call**, for both `divergence.baseline_pin`
  and `divergence.rung0_baseline_pin`. This change does not re-pin either.
- **D10 is unchanged in verdict.** KAN-33 stays HELD on D10.

## Not in this change

- **The two retired factories** (`make_signals_fn` for mean reversion, and
  `make_short_term_mr_signals_fn`) are on no live or replay path. They still
  keep their own lot record. Porting them to the context branch is a
  precondition of reviving them, and their docstrings say so.
- **Residual live/replay differences, outside this ticket:**
  - live's `highest_price_since_entry` can be raised by the risk service
    between runs, while the replay raises peaks at closes only;
  - `run_stability_sweep.py` does not apply the crash-entry freeze that
    `main()` does.

## Tests

- **AC1: `tests/backtest/test_runner_book.py`.** For each of the six sleeve
  families, a buy that is not filled leaves no lot and is signalled again on
  the next bar. It covers four causes: an unreached DAY limit, a risk refusal,
  a sleeve-cash refusal, and (for the five frozen families) a crash-freeze
  suppression. It also checks that:
  - an unbound book raises;
  - a factory with no context raises;
  - no sleeve closes over a `tracked` record;
  - a replay is repeatable on the same signal function;
  - `_runner_context` has the live shape.
- **AC2: `tests/backtest/test_position_source_parity.py`.** Each of the six
  production sleeves is replayed, and every fill is pushed through the live
  paper state (`PaperTradingState.record_fill` and `update_peak_prices`). On
  every session it asserts:
  - the context live hydrates (`build_portfolio_context`) equals the
    runner's, field by field;
  - the sleeve reading live's context emits exactly what the replay's did.
- **Live is unchanged: `tests/scripts/test_live_roster_unchanged.py`.** A
  fixed scenario covers holdings past a stop, fresh and old holdings, a
  pending buy and a partly covering pending sell. Its live-roster decisions
  are byte-identical to a golden produced on `6b93d6a`.
- **Fingerprint: `tests/backtest/test_shadow_artifact.py`.** It pins the new
  production `baseline_id`.
- **This document: `tests/docs/test_backtest_position_source_decision.py`.**
  It checks this document's figures against `assets/kan-116/results.json`.

## Reproducing

The final measurements are in `assets/kan-116/results.json`.
`assets/kan-116/remeasure.sh` re-runs the 10y and pin comparisons against two
code trees (`git archive origin/main` and this branch), and
`assets/kan-116/d10_rehearsal.py` computes the D10 statistics against a
scratch holdout registry. Neither writes outside the output directory it is
given.

The 2y and shadow figures used KAN-105's scratch bar extraction:
`docs/decisions/assets/kan-105/extract_inputs.py` describes the same inputs.

**Before the change, the final code reproduces the investigation prototype's
legacy numbers exactly; after it, the prototype's option-1 numbers.** That
holds for every sleeve except earnings_drift on the deploy clone's earnings
cache, which was refreshed between the two runs.
