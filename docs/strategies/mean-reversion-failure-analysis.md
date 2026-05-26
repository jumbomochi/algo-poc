# Mean-Reversion Sleeve Failure Analysis

**Date:** 2026-05-26
**Status:** Sleeves removed from active portfolio. Signal functions preserved in code for future revival.
**Backtest reference:** `output/backtest_multi_20260526_082323.json`
**Horizon analyzed:** 2016-05-31 → 2026-05-22 (9.97 years, 2,510 trading days)

---

## TL;DR

Both mean-reversion sleeves had **negative trade-level expectancy** over a decade.
They are being removed from the live capital allocation and reallocated proportionally
to the six surviving strategies. The implementations remain in the codebase
(`scripts/run_backtest.py:make_signals_fn`, `make_short_term_mr_signals_fn`) so they
can be re-enabled when the conditions in the [Revival Conditions](#revival-conditions)
section are met.

This is not a claim that mean-reversion is a dead concept — it is a claim that
**these two implementations, in their current form, lost money over the specific
2016–2026 regime on the specific universes they were configured to trade.**

---

## Performance summary (capital → final)

| Sleeve | Capital | Final | Return | CAGR | Sharpe | Max DD | Win % | Trades |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `mean_reversion` | $12,000 | $6,555 | **−45.4%** | −5.88% | −0.13 | 68.7% | 32.3% | 235 |
| `short_term_mr` | $10,000 | $62 | **−99.4%** | −39.91% | −0.60 | 99.4% | 44.7% | 6,160 |

---

## Trade-level evidence

### `mean_reversion`

| Metric | Value | Notes |
|---|---:|---|
| Win rate | 32.3% | Far below the ~60%+ MR strategies typically need to be viable |
| Avg win | $247.33 | |
| Avg loss | −$149.75 | |
| Win/loss ratio | 1.65 | Decent — but cannot overcome the 1-in-3 hit rate |
| **Expectancy per trade** | **−$21.33** | Negative edge |
| Median hold | 83 days | Drifted far from "short-term" reversion |
| Mean hold | 133 days | |
| **Max hold** | **824 days** | A 2.3-year "mean reversion" trade — the bug, not the feature |
| Exit reasons | 100% `trailing_stop` | No other exit path exists in code |
| Commissions | $52 (0.4% of capital) | Not the cause |
| Worst losers | ARKW −$856, NVDA −$752, PBW −$529 | All bought "at support" that then broke |

### `short_term_mr`

| Metric | Value | Notes |
|---|---:|---|
| Win rate | 44.7% | Acceptable for short-term MR |
| Avg win | $2.51 | |
| Avg loss | −$4.94 | |
| **Win/loss ratio** | **0.51** | Catastrophic asymmetry — losses ~2× wins |
| **Expectancy per trade** | **−$1.61** | × 6,160 trades ≈ −$9,917, matches the −$9,937 loss almost exactly |
| Median hold | 5 days | Mechanically working as designed |
| Max hold | 11 days | |
| Exit reasons | `rsi_recovery` 71.8%, `time_exit` 28.2% | Both exit mechanisms operate, neither is profitable |
| Commissions | $251 (2.5% of capital) | Meaningful but secondary — **edge, not friction, is the killer** |

---

## Root causes

### 1. `mean_reversion` has no max-loss stop by design

The trailing stop only activates *after* the position has been profitable
(`scripts/run_backtest.py`, in `make_signals_fn`):

```python
# Trailing stop: only activates after position has been profitable.
if peak > entry and (peak - current_price) / peak >= effective_trailing:
    should_sell = True
```

If a buy at "support" never rallies above entry, **there is no exit path at all**.
The position is held indefinitely. This is the mechanical reason for the 824-day max hold
and the three worst losers (ARKW, NVDA, PBW) — all support breaks that the strategy
could not escape.

The original design intent (per the docstring) was that mean-reversion buys at support
*expect a further drop before recovery*, so a max-loss stop would prematurely stop out
trades that would otherwise recover. The realized data shows that **a non-trivial
fraction of "support" breaks never recover within a holding period that is acceptable
for capital efficiency**, and the strategy has no way to recognize this.

### 2. Universe mismatch — MR applied to mega-cap trenders

Both sleeves trade S&P 500 mega-caps:

- `mean_reversion` → `SP500_TOP50`
- `short_term_mr` → `SP500_TOP100`

Classic mean-reversion research (Faber, Connors, Lo & MacKinlay) is validated on
**broad index instruments** (SPY, QQQ) and **range-bound or low-momentum names**.
Applied to AAPL, NVDA, TSLA, MSFT — among the strongest momentum stocks of the decade —
the strategy systematically buys weakness in instruments that *continue* in the
weakness direction, then exits the eventual bounce at small profit (`avg win $2.51`
on `short_term_mr`) while occasionally riding catastrophic continuation
(`avg loss $4.94`).

This is also visible at the strategy *family* level: the same regime that killed
mean-reversion fed `momentum`, `sector_rotation`, `thematic_momentum` — all of which
exceeded 19% CAGR. The two strategy families are taking the opposite side of the
same market property.

### 3. `short_term_mr` is missing Connors' trend filter

The implementation (`scripts/run_backtest.py:make_short_term_mr_signals_fn`) fires on
RSI(2) < 10 + Bollinger lower-band touch + 1.5× volume. Larry Connors' original RSI-2
specification additionally requires `close > SMA(200)` — i.e., **only buy oversold
within an established uptrend**. Without that filter, the strategy fires on dips
during downtrends, which is precisely where losses balloon.

This is fixable in code without changing the spirit of the strategy. It was the first
fix considered, and is logged here as the most actionable structural change if the
sleeve is ever revived.

### 4. Regime mismatch

The 2016-2026 horizon is unusually trend-persistent: passive-flow accumulation,
ZIRP → QT → renormalization cycles, mega-cap concentration, AI capex super-cycle.
The "mean" that mean-reversion strategies assume is *stationary* was in fact drifting
upward at high speed. Dips were shallow (so MR signals fired less often and from
shallower depths) and rallies were violent (so trailing stops cut winners too early
even when the entry was good).

This factor is not under the strategy's control — it is the regime itself. It is the
primary reason the [Revival Conditions](#revival-conditions) below center on regime
signals rather than code changes.

---

## Revival Conditions

The author's stated position: *the stock market is an unstable environment; past
success is not a predictor of future success.* This document does not declare these
strategies dead. It declares them **unfit for the current regime**.

Revisit the sleeves when **any two** of the following hold over a rolling 12-month window:

1. **Trend persistence collapse.** The 12-month autocorrelation of weekly SPY returns
   turns negative for at least 6 consecutive months (i.e., past weekly returns become
   *negative* predictors of next-week returns — the mean-reversion regime).

2. **Volatility regime shift.** VIX 12-month average rises above 25 and stays there
   for 6+ months, with realized SPY volatility above 20% annualized.

3. **Mega-cap leadership reversal.** Equal-weight S&P (RSP) outperforms cap-weight S&P
   (SPY) over a rolling 12-month window — i.e., the passive-flow concentration trade
   inverts.

4. **Drawdown depth.** A peak-to-trough SPY drawdown of ≥ 25% has occurred, followed
   by a sideways range of ≥ 6 months (not a V-shaped recovery).

5. **Macro liquidity reversal sustained.** Fed balance sheet declining for 18+ months
   alongside positive real Fed funds rate. (Distinguishes a structural environment
   change from a tactical pause.)

When revisiting, the **first changes** to make before re-enabling are:

- Add a hard max-loss stop (−8% from entry) to `make_signals_fn`
- Add the SMA(200) trend filter to `make_short_term_mr_signals_fn`
- Switch the universes to broad-index ETFs (SPY, QQQ, IWM) and low-vol sleeves
  (XLU, XLP, USMV) — not mega-cap individual names
- Add a regime gate that disables entries during `bear` and `crash` regimes
  (the engine already computes `regime_by_date`)

---

## Code locations preserved

The signal functions remain in `scripts/run_backtest.py` and are not called from
`main()` after the 2026-05-26 reallocation:

- `make_signals_fn(...)` — defines `mean_reversion`
- `make_short_term_mr_signals_fn(...)` — defines `short_term_mr`

To revive: re-instantiate the signal functions in `main()` and re-add the
`PortfolioConfig` entries to the `portfolios` dict, with allocations drawn from
whichever sleeves should be reduced.

---

## Author's note on epistemic humility

This document records what was *observed* over a specific 9.97-year window on a
specific universe with specific implementations. It does not claim that
mean-reversion is "wrong" as a market hypothesis. It claims that *these two
implementations underperformed in this regime*. The revival conditions above are
the discipline against survivorship bias — they specify in advance what evidence
would justify changing the conclusion, so the decision is not made on the basis
of "the market feels different now."
