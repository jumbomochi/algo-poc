# Trading Strategy: Dual Mean-Reversion + Momentum

## Overview

A dual-strategy system that trades the top 50 S&P 500 stocks by market cap, combining mean-reversion (buying dips at support) with relative-strength momentum (buying sustained uptrends). Both strategies share a common risk engine and trailing stop exit mechanism. Inverse ETFs (SH, PSQ) are included in the momentum universe for natural bear market hedging.

**10-year backtest (2016-2026):** 19.1% CAGR, Sharpe 1.51, 17.89% max drawdown, $100k -> $576k.

## Strategy 1: Mean-Reversion

### Entry Logic

Buys large-cap stocks when they pull back to historically tested support levels with high-conviction confirmation signals. All five conditions must be met simultaneously:

| Signal | Threshold | What It Measures |
|---|---|---|
| Support Proximity | > 0.8 | Price is very close to a detected support level |
| Support Strength | confidence > 0.7 | The support level has been tested multiple times |
| RSI (14-day) | signal > 0.4 (RSI < 30) | Stock is deeply oversold |
| Volume | signal > 0.5 (volume > 2x avg) | Institutional activity / capitulation |
| Support Trend | signal > 0.0 | Support levels are rising over time |

These thresholds are deliberately strict. The strategy only trades when all five signals align, resulting in ~8 mean-reversion trades per year. This selectivity is critical to performance — relaxing any threshold significantly degrades risk-adjusted returns.

### Add-On Entry (Pyramiding)

If already holding 1 lot and the position is in profit, a second lot can be added with slightly relaxed thresholds (RSI < 35 instead of < 30). Maximum 2 lots per ticker.

### Exit Logic

Trailing stop only. No max loss stop and no time cap.

- The trailing stop activates only after the position has been profitable (peak > entry).
- Losers are held until they recover. This is intentional: mean-reversion buys at support, so a further drop is expected before the reversal. Cutting losers at a fixed % would exit right before the recovery.
- The trailing stop percentage is 10% by default.

### Why No Max Loss Stop

Early iterations included an 8% max loss stop. It destroyed win rate (52% -> 43%) because large-cap stocks regularly dip 8-10% from support before recovering. The mean-reversion thesis is that the stock *will* bounce — a max loss stop contradicts the strategy's core assumption.

## Strategy 2: Momentum (Relative Strength)

### Entry Logic

Ranks all tickers (including inverse ETFs) by their 6-month (126 trading day) return. Buys the top 5 performers that aren't already held.

| Parameter | Value | Rationale |
|---|---|---|
| Lookback | 126 days (6 months) | Standard institutional momentum window |
| Top N | 5 | Broad enough to deploy capital across more positions |
| Re-ranking | Daily | Rankings update every bar, but entries only when a stock freshly enters the top 5 |

### Exit Logic

Two mechanisms:

1. **Trailing stop (10%):** Same as mean-reversion — activates after profit, exits when price drops 10% from peak.
2. **Max loss stop (8%):** Unlike mean-reversion, momentum positions *do* have a max loss stop. If a momentum stock drops 8% from entry without ever being profitable, it's exited. Rationale: momentum buys strength, so a big drop means the thesis is broken.

### Inverse ETFs

The universe includes SH (inverse S&P 500) and PSQ (inverse NASDAQ-100). These aren't treated specially — the momentum ranking naturally selects them during bear markets because they're the top performers when everything else is falling. When the market recovers, regular stocks overtake them in the ranking and they get exited via trailing stop.

A regime-change exit forces immediate sale of inverse ETFs when the market regime shifts from bear to neutral/bull, since inverse ETFs decay in non-bear environments.

## Strategy Composition

The two strategies are composed via `make_combined_signals_fn()` with this priority:

1. **Sell signals first** — if either strategy says sell, sell immediately.
2. **Mean-reversion buy** — checked first because it's more selective (~8 trades/year vs ~40 for momentum). When mean-reversion fires, it's a higher-conviction signal.
3. **Momentum buy** — checked if mean-reversion has no signal.

Both strategies maintain independent internal state (tracked positions with entry prices and peak prices). The shared risk engine prevents over-allocation.

## Risk Management

| Parameter | Value | Purpose |
|---|---|---|
| Position size | 12% of NAV | Balances meaningful P&L per trade with diversification |
| Position entry limit | 12% of NAV | Risk engine caps any single entry |
| Sector concentration | 30% | No more than 30% in one sector |
| Total exposure | 150% | Allows up to 150% gross exposure for more capital deployment |
| Max lots per ticker | 2 | Allows pyramiding but prevents over-concentration |
| Trailing stop | 10% | Exits winners that reverse; only after profitable |

### Position Size and Exposure Optimization

The current 12% / 150% / top 5 configuration was found by optimizing across two dimensions: position size (raw returns) and diversification (risk-adjusted returns via top_n and exposure limit).

**Phase 1 — Position size scaling (top_n=3, 100% exposure):**

| Position Size | CAGR | Sharpe | Max DD | Trades |
|---|---|---|---|---|
| 7% | 14.2% | 1.45 | 13.23% | 458 |
| 10% | 14.9% | 1.46 | 14.31% | 458 |
| 12% | 16.5% | 1.47 | 15.31% | 458 |
| 14% | 18.0% | 1.47 | 16.14% | 458 |

Sharpe held constant at ~1.47 as position size increased, but the concentrated 14% config maxed out at ~7 positions and left momentum signals rejected when fully allocated.

**Phase 2 — Diversification optimization (higher exposure + top_n):**

| Config | CAGR | Sharpe | Max DD | Trades |
|---|---|---|---|---|
| 5% / 150% / top 3 | 9.6% | 1.46 | 10.15% | 458 |
| 10% / 120% / top 3 | 14.9% | 1.47 | 14.28% | 458 |
| 10% / 150% / top 5 | 17.2% | 1.51 | 16.84% | 631 |
| 12% / 150% / top 5 | 19.1% | 1.51 | 17.89% | 631 |

The key insight: increasing top_n from 3 to 5 was what unlocked the extra exposure capacity. With top_n=3, signal frequency was too low to fill more than ~7 positions regardless of exposure limit. Top_n=5 generated 631 trades (vs 458), deployed more capital, and improved Sharpe from 1.47 to 1.51 through better diversification.

## Regime Detection

A market regime indicator is computed based on 200-day MA breadth:

| Regime | Condition | Frequency (10yr) |
|---|---|---|
| Bull | > 60% of stocks above their 200-day MA | 72-75% of days |
| Neutral | 40-60% above | 16-19% of days |
| Bear | < 40% above | 8-9% of days |

The regime detection infrastructure is built into the codebase (`compute_regime_by_date`, `REGIME_PARAMS`) and can be used for regime-adaptive parameter switching. However, the current optimal configuration **does not use regime-adjusted parameters** in the main() entry point — backtesting showed that regime-adaptive trailing stops (wider in bull, tighter in bear) reduced Sharpe from 1.47 to 1.22 because:

- Bull markets (75% of time) got a 15% trailing stop, increasing variance without bigger winners.
- Bear markets (9% of time) are too rare to meaningfully impact overall performance.
- The max loss stop in bear triggered false exits on positions that would have recovered.

The regime is still used for the inverse ETF force-exit mechanism (exit SH/PSQ when regime leaves bear).

## Key Design Decisions

### High-Conviction Entry Filters

The mean-reversion strategy was progressively tightened from lenient thresholds to the current strict ones. This reduced trade count from ~200/year to ~8/year but transformed the 10-year return from -66% to +7.71% (before momentum was added). Quality over quantity.

### No Hard Stop on Mean-Reversion

The strategy explicitly holds losing mean-reversion positions until they recover. This was counter-intuitive but critical: removing the hard stop improved 5-year returns from -50.78% to -42.55%, and subsequent parameter tuning turned the strategy profitable. The insight is that mean-reversion *buys weakness*, so further weakness is expected before the reversal.

### Trailing Stop Only After Profit

The trailing stop only activates after `peak > entry`. This prevents the trailing stop from acting as a de facto hard stop on positions that haven't yet had a chance to work. Combined with the 10% trailing distance, this lets winners run while protecting gains.

### Momentum Complements Mean-Reversion

Mean-reversion has dead years (2019, 2023) where stocks trend up without pulling back to support. Momentum captures these sustained trends. Together they cover both market behaviors, adding ~470% to the 10-year return (+7.71% mean-reversion-only -> +476.18% combined).

### Simple Composition Over Complex Blending

The strategies are composed with a simple priority chain (sell > mean-reversion buy > momentum buy) rather than weighted blending or ensemble scoring. This keeps the system interpretable and each strategy's P&L attributable.

## Implementation

All strategy logic lives in `scripts/run_backtest.py`:

| Function | Purpose |
|---|---|
| `make_signals_fn()` | Mean-reversion signal generator |
| `make_momentum_signals_fn()` | Momentum/relative-strength signal generator |
| `make_sector_rotation_signals_fn()` | Sector rotation signal generator (top N sectors by 3-month return) |
| `make_short_term_mr_signals_fn()` | Short-term mean-reversion (RSI(2) + Bollinger Band oversold bounces) |
| `make_thematic_momentum_signals_fn()` | Thematic ETF momentum (top N above 50-day MA) |
| `make_quality_value_signals_fn()` | Quality value (composite ROE/D-E/margin ranking) |
| `make_earnings_drift_signals_fn()` | Earnings drift / PEAD (post-earnings surprise entry) |
| `make_tail_risk_hedge_signals_fn()` | Tail-risk hedge: regime-based rotation between inverse/defensive ETFs |
| `simulate_rebalancer()` | Post-processing: performance-adaptive weight rebalancing |
| `make_combined_signals_fn()` | Composes MR + momentum with sell priority (legacy, not used in multi-portfolio mode) |
| `compute_regime_by_date()` | Market regime classification |
| `compute_aggregate_metrics()` | Aggregate metrics across multiple portfolios |
| `print_multi_portfolio_results()` | Print per-portfolio + aggregate results |
| `save_multi_portfolio_results()` | Save multi-portfolio results to JSON |
| `PortfolioConfig` | Dataclass: name, capital, signals_fn, risk_engine |
| `REGIME_PARAMS` | Regime-specific parameter overrides |
| `BEAR_TICKERS` | Inverse ETF tickers for bear market plays |

Supporting infrastructure:

| Module | Purpose |
|---|---|
| `services/signal_generation/technical.py` | Signal classes (SupportProximity, SupportStrength, SupportTrend, RSI, Volume, BollingerBand) |
| `services/signal_generation/fundamental.py` | Signal classes (Valuation, Quality, Growth) |
| `services/signal_generation/event.py` | Signal classes (EarningsSurprise, NewsSentiment, InsiderActivity) |
| `services/risk_management/engine.py` | Risk engine (position limits, sector concentration, max lots) |
| `backtest/runner.py` | Backtest engine (daily bar replay, order simulation, P&L tracking) |
| `scripts/fetch_fundamentals.py` | Fundamentals data cache (yfinance fetcher, point-in-time lookup) |
| `scripts/fetch_earnings.py` | Earnings data cache (yfinance fetcher, event window lookup) |
| `scripts/visualize_backtest.py` | Plotly HTML report generation |

## Multi-Portfolio Infrastructure

The backtest supports running multiple independent portfolios, each with its own capital allocation, signal function, and risk engine. This allows strategies to be tested in isolation without competing for capital.

### How It Works

Each portfolio is defined by a `PortfolioConfig` with:

- **name** — identifier used in output and trade tagging
- **capital** — independent capital allocation
- **signals_fn** — any callable matching `(ticker, bars) -> signal | None`
- **risk_engine** — independent `RiskEngine` instance with its own limits

Bar data is fetched once and shared across all portfolios. Each portfolio gets its own `BacktestRunner` instance. Results are collected independently and then aggregated.

### Aggregation

- **Equity curves** are summed element-wise (the combined portfolio value at each date)
- **Trades** are pooled and tagged with a `"portfolio"` key for attribution
- **Aggregate Sharpe** is computed from the combined equity curve — not averaged across portfolios, which would be mathematically incorrect

### Adding a New Strategy

To add a new portfolio, add an entry to the `portfolios` dict in `main()`:

```python
portfolios["sector_rotation"] = PortfolioConfig(
    name="sector_rotation",
    capital=args.capital * 0.12,
    signals_fn=sector_rotation_signals_fn,
    risk_engine=RiskEngine(
        position_entry_limit_pct=20.0,
        total_exposure_limit_pct=100.0,
    ),
)
```

Also add the strategy's universe to `UNIVERSE_REGISTRY` and its ticker list.

When multiple portfolios are configured, the output automatically switches to multi-portfolio format with per-portfolio summaries, aggregate metrics, and a combined JSON output file.

### Backward Compatibility

With a single portfolio, the output format is identical to the original — same `print_results()` and `save_results()` functions are used. Multi-portfolio output only activates when 2+ portfolios are configured.

### Current Portfolio Configuration

The backtest runs eight independent portfolios:

| Portfolio | Capital | Strategy | Risk Limits |
|---|---|---|---|
| `mean_reversion` | 12% of total | Support-level dip buying (S&P 50) | 15% entry, 120% exposure, 2 lots |
| `momentum` | 18% of total | 6-month relative strength (S&P 50 + inverse ETFs) | 12% entry, 150% exposure, 1 lot |
| `sector_rotation` | 12% of total | Top 3 sector ETFs by 3-month return | 20% entry, 100% exposure, 1 lot |
| `quality_value` | 12% of total | Top 15 by ROE/D-E/margin composite (S&P 100) | 10% entry, 100% exposure, 1 lot |
| `earnings_drift` | 15% of total | Post-earnings drift on >5% surprise (S&P 100) | 8% entry, 100% exposure, 1 lot |
| `short_term_mr` | 10% of total | RSI(2) + Bollinger Band oversold bounces (S&P 100) | 8% entry, 100% exposure, 1 lot |
| `thematic_momentum` | 11% of total | Top 8 thematic ETFs above 50-day MA | 15% entry, 120% exposure, 1 lot |
| `tail_risk_hedge` | 10% of total | Regime-based defensive rotation (inverse ETFs + GLD/TLT) | 25% entry, 100% exposure, 1 lot |

Each strategy has independent capital, signal function, and risk engine. Strategies never compete for capital.

Quality value and earnings drift require cached data from `data/cache/`. To populate:
```bash
python scripts/fetch_fundamentals.py  # Fetches quarterly financials from yfinance
python scripts/fetch_earnings.py      # Fetches earnings dates/surprises from yfinance
```
If cache files are missing, these strategies will produce no signals (graceful degradation).

### Universe Registry

Each strategy defines its own ticker universe via `UNIVERSE_REGISTRY`. Bar data is fetched once for the union of all universes. Currently defined universes:

| Strategy | Universe | Ticker Count |
|---|---|---|
| `mean_reversion` | S&P 500 top 50 | 50 |
| `momentum` | S&P 500 top 50 + inverse ETFs | 52 |
| `sector_rotation` | SPDR sector ETFs | 11 |
| `quality_value` | S&P 500 top 100 | 100 |
| `earnings_drift` | S&P 500 top 100 | 100 |
| `short_term_mr` | S&P 500 top 100 | 100 |
| `thematic_momentum` | Thematic ETFs | 25 |
| `tail_risk_hedge` | Inverse + defensive ETFs | 5 |

## Performance-Adaptive Rebalancer

A post-processing simulation that re-weights strategy equity curves based on trailing performance.

### How It Works

After all portfolios run independently, `simulate_rebalancer()` takes their equity curves and:

1. Every 21 trading days (~monthly), computes trailing 6-month Sharpe per strategy
2. Strategies with above-median Sharpe gain weight; below-median lose weight
3. Max shift: 5% per strategy per rebalance
4. Floor: 5% per strategy (tail-risk hedge: 8%)
5. Ceiling: 25% per strategy
6. Weights re-normalized to sum to 1.0

### Output

Returns a rebalanced combined equity curve and weights history for analysis. This is an approximation valid at retail scale where position scaling has no market impact.
