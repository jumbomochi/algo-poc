# Trading Strategy: Dual Mean-Reversion + Momentum

## Overview

A dual-strategy system that trades the top 50 S&P 500 stocks by market cap, combining mean-reversion (buying dips at support) with relative-strength momentum (buying sustained uptrends). Both strategies share a common risk engine and trailing stop exit mechanism. Inverse ETFs (SH, PSQ) are included in the momentum universe for natural bear market hedging.

**10-year backtest (2016-2026):** 18.0% CAGR, Sharpe 1.47, 16.14% max drawdown, $100k -> $523k.

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

Ranks all tickers (including inverse ETFs) by their 6-month (126 trading day) return. Buys the top 3 performers that aren't already held.

| Parameter | Value | Rationale |
|---|---|---|
| Lookback | 126 days (6 months) | Standard institutional momentum window |
| Top N | 3 | Concentrated on strongest performers only |
| Re-ranking | Daily | Rankings update every bar, but entries only when a stock freshly enters the top 3 |

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
| Position size | 14% of NAV | Large enough to capture meaningful P&L per trade |
| Position entry limit | 14% of NAV | Risk engine caps any single entry |
| Sector concentration | 30% | No more than 30% in one sector |
| Total exposure | 100% | No margin/leverage |
| Max lots per ticker | 2 | Allows pyramiding but prevents over-concentration |
| Trailing stop | 10% | Exits winners that reverse; only after profitable |

### Why 14% Position Size

The position size was the single most impactful parameter for returns while maintaining Sharpe ratio. Progression during tuning:

| Position Size | CAGR | Sharpe | Max DD |
|---|---|---|---|
| 7% | 14.2% | 1.45 | 13.23% |
| 10% | 14.9% | 1.46 | 14.31% |
| 12% | 16.5% | 1.47 | 15.31% |
| 14% | 18.0% | 1.47 | 16.14% |

Sharpe held constant at ~1.47 as position size increased from 10% to 14% because the signal quality is high — larger bets on the same good signals scale returns linearly without increasing the variance ratio.

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

Mean-reversion has dead years (2019, 2023) where stocks trend up without pulling back to support. Momentum captures these sustained trends. Together they cover both market behaviors, adding ~270% to the 10-year return (+7.71% mean-reversion-only -> +423.82% combined).

### Simple Composition Over Complex Blending

The strategies are composed with a simple priority chain (sell > mean-reversion buy > momentum buy) rather than weighted blending or ensemble scoring. This keeps the system interpretable and each strategy's P&L attributable.

## Implementation

All strategy logic lives in `scripts/run_backtest.py`:

| Function | Purpose |
|---|---|
| `make_signals_fn()` | Mean-reversion signal generator |
| `make_momentum_signals_fn()` | Momentum/relative-strength signal generator |
| `make_combined_signals_fn()` | Composes both strategies with sell priority |
| `compute_regime_by_date()` | Market regime classification |
| `REGIME_PARAMS` | Regime-specific parameter overrides |
| `BEAR_TICKERS` | Inverse ETF tickers for bear market plays |

Supporting infrastructure:

| Module | Purpose |
|---|---|
| `services/signal_generation/technical.py` | Signal classes (SupportProximity, SupportStrength, SupportTrend, RSI, Volume) |
| `services/risk_management/engine.py` | Risk engine (position limits, sector concentration, max lots) |
| `backtest/runner.py` | Backtest engine (daily bar replay, order simulation, P&L tracking) |
| `scripts/visualize_backtest.py` | Plotly HTML report generation |
