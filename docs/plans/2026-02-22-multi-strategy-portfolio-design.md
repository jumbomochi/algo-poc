# Multi-Strategy Portfolio Design

## Objective

10x returns in 10 years (26% CAGR) with max 25% drawdown across 8 independent strategies, each with separate capital, risk engine, and signal function. Performance-adaptive rebalancing shifts capital toward higher-Sharpe strategies over time.

## Constraints

- Capital: $100k-$500k (retail scale, no market impact concerns)
- Max drawdown: 25% on aggregate portfolio
- Asset classes: US equities, inverse ETFs, sector ETFs, thematic ETFs
- At least one strategy focused on capital preservation (tail-risk hedge)
- ML/RL exploration deferred to Phase 6

## Architecture: Independent Portfolios with Rebalancer (Approach A)

Each strategy is a fully independent portfolio with its own `PortfolioConfig` (capital, signals_fn, risk_engine). A rebalancer sits above them and periodically shifts capital allocations based on rolling Sharpe. Strategies never see each other.

Why this approach over alternatives:
- **vs Central Risk Overlay:** Simpler, each strategy independently testable, clean attribution
- **vs Signal Ensemble:** Preserves working backtest infrastructure, debuggable, attributable P&L
- Cross-strategy duplicate positions are a non-issue at retail scale

## Strategy Roster

### Strategy 1: Mean-Reversion (existing)

- **Edge:** Buys large-cap dips at support
- **Universe:** S&P 500 top 50
- **Max positions:** 10
- **Entry:** Support proximity > 0.8, support strength confidence > 0.7, RSI(14) < 30, volume > 2x avg, rising supports. All 5 conditions required.
- **Exit:** Trailing stop 10% (activates after profit only). No hard stop.
- **Expected:** 8-12% CAGR, Sharpe 1.0-1.3
- **Implementation:** Existing `make_signals_fn()`, no changes

### Strategy 2: Momentum (existing)

- **Edge:** Rides 6-month relative strength winners
- **Universe:** S&P 500 top 50 + inverse ETFs (SH, PSQ)
- **Max positions:** 10
- **Entry:** Top 5 by 126-day return, not already held
- **Exit:** Trailing stop 10% + hard stop 8%. Regime-change exit for inverse ETFs.
- **Expected:** 12-18% CAGR, Sharpe 1.2-1.5
- **Implementation:** Existing `make_momentum_signals_fn()`, no changes

### Strategy 3: Sector Rotation

- **Edge:** Overweights strongest sectors, underweights weakest
- **Universe:** 11 SPDR sector ETFs (XLK, XLE, XLF, XLV, XLY, XLP, XLI, XLB, XLU, XLRE, XLC)
- **Max positions:** 6
- **Entry:** Rank sectors by 3-month return, buy top 3. Rebalance monthly.
- **Exit:** Drops out of top 3 at next monthly rebalance, or trailing stop 8%
- **Regime overlay:** Bear regime rotates to defensive only (XLU, XLP, XLV)
- **Expected:** 10-15% CAGR, Sharpe 1.0-1.3
- **New signals:** Sector rank by 3-month return

### Strategy 4: Quality Value

- **Edge:** Buys undervalued high-quality stocks
- **Universe:** S&P 500 full (~500 stocks)
- **Max positions:** 15
- **Entry:** PE < sector median AND ROE > sector median AND debt/equity < sector median. Rank by composite score, buy top 15.
- **Exit:** Fundamentals deteriorate (PE above median OR ROE below median), or trailing stop 12%
- **Rebalance:** Quarterly
- **Expected:** 10-14% CAGR, Sharpe 1.1-1.4
- **Data dependency:** Fundamentals (PE, ROE, D/E) — existing ValuationSignal + QualitySignal, needs backtest integration
- **New signals:** Fundamental composite score

### Strategy 5: Earnings Drift (PEAD)

- **Edge:** Post-earnings-announcement drift
- **Universe:** S&P 500 full
- **Max positions:** 20
- **Entry:** Earnings surprise > 5% (beat estimate by 5%+), buy within 2 days of announcement
- **Exit:** Fixed hold 20 trading days, or trailing stop 6%
- **Expected:** 12-18% CAGR, Sharpe 1.0-1.3
- **Data dependency:** Earnings event dates + surprise magnitude from Alpha Vantage
- **New signals:** Earnings event trigger

### Strategy 6: Short-Term Mean-Reversion

- **Edge:** 3-5 day oversold bounces
- **Universe:** S&P 500 top 100
- **Max positions:** 15
- **Entry:** RSI(2) < 10 AND price touches lower Bollinger Band (2 sigma, 20-day) AND volume > 1.5x avg
- **Exit:** RSI(2) > 70 OR 5 trading days elapsed, whichever first. No trailing stop.
- **Expected:** 15-25% CAGR, Sharpe 1.0-1.2
- **New signals:** RSI(2) (parameterize existing RSI), Bollinger Band signal

### Strategy 7: Thematic Momentum

- **Edge:** Multi-month trends in thematic ETFs
- **Universe:** ~25 thematic ETFs (ARKK, TAN, HACK, BOTZ, LIT, CIBR, SKYY, DRIV, FINX, GAMR, HERO, IDRV, CLOU, WCLD, SNSR, PRNT, IZRL, GNOM, ARKG, ARKQ, ARKW, ARKF, ICLN, QCLN, PBW)
- **Max positions:** 8
- **Entry:** 3-month return > 10% AND above 50-day MA AND volume trend rising. Buy top 8.
- **Exit:** Drops below 50-day MA OR trailing stop 10%
- **Expected:** 12-20% CAGR, Sharpe 0.9-1.2
- **New signals:** Thematic MA cross (50-day)

### Strategy 8: Tail-Risk Hedge

- **Edge:** Inverse ETFs + defensive rotation when regime turns bear
- **Universe:** SH, PSQ, SDS, TLT, GLD
- **Max positions:** 5
- **Entry by regime:**
  - Bull: 50% GLD + 50% TLT
  - Neutral: 40% GLD + 40% TLT + 20% SH
  - Bear: 40% SH + 30% PSQ + 20% SDS + 10% GLD
- **Exit:** Regime change triggers full rotation
- **Expected:** -2% to +3% CAGR (insurance, not alpha)
- **No new signals:** Uses existing `compute_regime_by_date()`

## Capital Allocation

### Initial Allocation

| Strategy | Allocation | Rationale |
|---|---|---|
| Mean-Reversion | 12% | Proven, selective, lower frequency |
| Momentum | 18% | Proven, highest historical Sharpe |
| Sector Rotation | 12% | Diversifier, ETF-based low cost |
| Quality Value | 12% | Steady compounder, low turnover |
| Earnings Drift | 15% | High expected CAGR, short holds |
| Short-Term MR | 10% | High turnover, less capital per trade |
| Thematic Momentum | 11% | Higher vol, capped |
| Tail-Risk Hedge | 10% | Insurance, fixed floor |

### Performance-Adaptive Rebalancing

- **Frequency:** Monthly evaluation, rebalance only if drift > 3%
- **Metric:** Trailing 6-month Sharpe ratio per strategy
- **Mechanism:** Above-median Sharpe strategies gain from below-median
- **Shift cap:** Max 5% per strategy per rebalance
- **Floor:** 5% minimum per strategy (Tail-Risk: 8% floor)
- **Ceiling:** 25% maximum per strategy
- **Tail-Risk special rule:** Allocation increases to 15% in bear regime

## Risk Architecture

### Level 1: Per-Strategy Risk Engine

| Strategy | Position Limit | Exposure Limit | Max Lots | Trailing Stop | Hard Stop |
|---|---|---|---|---|---|
| Mean-Reversion | 15% | 120% | 2 | 10% | None |
| Momentum | 12% | 150% | 1 | 10% | 8% |
| Sector Rotation | 20% | 100% | 1 | 8% | None |
| Quality Value | 10% | 100% | 1 | 12% | None |
| Earnings Drift | 8% | 100% | 1 | 6% | None (time exit) |
| Short-Term MR | 8% | 100% | 1 | None (time exit) | None |
| Thematic Momentum | 15% | 120% | 1 | 10% | 8% |
| Tail-Risk Hedge | 25% | 100% | 1 | None (regime exit) | None |

Each strategy uses its own `RiskEngine` instance. No modifications to the engine needed.

### Level 2: Cross-Portfolio Monitoring

Post-execution daily monitor (does not block trades):
- **Aggregate drawdown alert:** Warning at -15% from peak
- **Aggregate circuit breaker:** Freeze new entries at -22%, resume at -15%
- **Strategy divergence alert:** Flag if any strategy exceeds 2x historical max drawdown

### Level 3: Tail-Risk Override

- Bear regime: Tail-risk allocation 10% -> 15%, tighten all trailing stops by 2%
- Crash regime (>90% below 200-day MA): Freeze all new entries, only exits and tail-risk hedge operate

## Data Architecture

### Universe Registry

Total unique tickers: ~540 (50 top S&P + ~450 remaining S&P + 11 sector ETFs + ~25 thematic ETFs + 5 inverse/defensive)

Bar data fetched once for the union, each strategy receives its subset.

### Data Requirements

| Strategy | OHLCV | Fundamentals | Events | Regime |
|---|---|---|---|---|
| Mean-Reversion | Yes | No | No | Optional |
| Momentum | Yes | No | No | Yes |
| Sector Rotation | Yes | No | No | Yes |
| Quality Value | Yes | Yes (PE, ROE, D/E) | No | No |
| Earnings Drift | Yes | No | Yes (earnings dates) | No |
| Short-Term MR | Yes | No | No | No |
| Thematic Momentum | Yes | No | No | No |
| Tail-Risk Hedge | Yes | No | No | Yes |

### Fundamentals/Events in Backtest

Cache to local files (JSON/CSV), replay with proper date alignment. Avoids look-ahead bias and makes backtests reproducible. IB provides historical fundamentals but not point-in-time, so caching is required.

## New Signals Needed

| Signal | Strategy | Complexity |
|---|---|---|
| RSI(2) | Short-Term MR | Low (parameterize existing RSI) |
| Bollinger Band | Short-Term MR | Low (20-day MA +/- 2 sigma) |
| Sector Rank | Sector Rotation | Medium (3-month return ranking) |
| Fundamental Composite | Quality Value | Medium (compose ValuationSignal + QualitySignal) |
| Earnings Event | Earnings Drift | Medium (event dates + surprise magnitude) |
| Thematic MA Cross | Thematic Momentum | Low (50-day MA crossover) |

## Backtest Execution Flow

```
main()
  +-- Parse args
  +-- Fetch bars for union universe (once)
  +-- Compute regime_by_date (once, shared)
  +-- Build 8 PortfolioConfigs
  +-- For each portfolio:
  |     +-- BacktestRunner(executor, capital).run(bars, signals_fn, risk_engine)
  +-- Compute aggregate metrics (element-wise sum of equity curves)
  +-- Run rebalancer simulation (post-processing on equity curves)
  +-- Print per-strategy + aggregate results
  +-- Save JSON output
```

### Rebalancer Simulation

Post-processing approach (Option A): run all strategies independently, then simulate monthly rebalancing on the equity curves by adjusting weights. Approximation is valid at retail scale where position scaling has no market impact. Integrated runner (Option B) deferred to Phase 5 (live trading).

### Output Structure

```json
{
  "config": { "total_capital": ..., "years": ..., "rebalance_frequency": ... },
  "portfolios": {
    "mean_reversion": { "config": {}, "trades": [], "portfolio_values": [], "dates": [], "metrics": {} },
    ...
  },
  "aggregate": {
    "static": { "portfolio_values": [], "metrics": {} },
    "rebalanced": { "portfolio_values": [], "metrics": {}, "allocation_history": [] }
  },
  "regime": { "dates": [], "regimes": [] },
  "bars": {}
}
```

## Implementation Phases

### Phase 1: Split & Validate

Split existing dual strategy into independent MR and Momentum portfolios. Validate aggregate results match current combined performance. Add universe registry.

### Phase 2: Low-Complexity Strategies

Implement Sector Rotation, Short-Term MR, and Thematic Momentum. These need only OHLCV data and simple new signals (RSI(2), Bollinger Band, sector rank, MA cross).

### Phase 3: Data-Dependent Strategies

Implement Quality Value and Earnings Drift. Wire fundamentals and events pipelines into backtest. Build cached data store for reproducibility.

### Phase 4: Tail-Risk Hedge & Rebalancer

Implement Tail-Risk Hedge strategy. Build performance-adaptive rebalancer simulation. Add cross-portfolio monitoring (drawdown alerts, circuit breaker).

### Phase 5: Live Trading

Integrated MultiPortfolioRunner for live execution. Wire IB execution with strategy-tagged orders. Per-strategy notifications. Paper trade all 8 strategies.

### Phase 6: ML/RL Exploration

Feature logging during backtest (signal vectors + outcomes as training data). Supervised model to predict returns from signal features. RL agent for capital allocation optimization (replace rule-based rebalancer). Walk-forward validation to prevent overfitting.

## ML/RL Integration Points

Flagged throughout the design for future Phase 6:

1. **Signal enhancement:** Replace rule-based thresholds with learned thresholds per strategy
2. **Capital allocation:** RL agent learns optimal weight vector (state = regime + strategy performance + correlations, action = allocation shifts, reward = risk-adjusted return)
3. **Feature logging:** BacktestRunner records (date, ticker, signals, action, outcome) per bar for training data
4. **Meta-learning:** Learn which strategies perform best in which regimes, beyond simple Sharpe ranking
