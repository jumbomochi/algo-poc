# Strategy Redesign: Mean-Reversion on Large-Cap Industry Leaders

## Goal

Redesign the trading algorithm from a support-level strategy with capped exits to a mean-reversion strategy inspired by the MANU theoretical trades — buying at support on large-cap industry leaders, cutting losses fast, and letting winners run via trailing stops. Support pyramiding into winning positions.

## Current State

The existing algorithm buys S&P 500 top 50 stocks near support levels and exits at a fixed 8% profit target, 5% stop loss, or 40-bar time limit. Backtest over 2 years on 5 tickers showed +3.4% return with 48% win rate and Sharpe 0.45. The fixed profit target caps winners — the exit strategy is the primary weakness.

## Inspiration

**MANU theoretical trades**: 5 trades over ~2 years on a range-bound stock. Cut losses at ~5%, let winners run to resistance (one trade gained +48%). Total PnL: +$6,112 on $10k deployed (61% return). Key insight: asymmetric risk/reward via trailing stops instead of fixed targets.

**AlgoTraders AT06 (Mean Reversion on Market Turmoil)**: Exploits mean reversion following market disruptions. 8.73% yearly return, 8.77% volatility.

## Design

### Universe Selection

Pool: All US equities on NYSE/NASDAQ with market cap >= $50B (~150-200 names). Market cap serves as a proxy for industry leadership, which affords company stability.

Daily screening before market open:
- Filter to $50B+ market cap from IB fundamentals
- Require minimum average daily volume of 500k shares/day over 20 days for liquidity

Replaces the hardcoded SP500_TOP50 list with a dynamic screener.

### Entry Logic

**First entry** -- all conditions must be true:
1. Support proximity: price within 2% of support level with >= 3 touches (SupportProximitySignal value > 0.6, SupportStrengthSignal confidence > 0.5)
2. Oversold confirmation: 14-day RSI < 35
3. Volume confirmation: current day volume > 1.5x 20-day average volume
4. Support trend: SupportTrendSignal value >= 0 (stable or rising supports)

**Adding to winners (pyramid)** -- when already holding a lot on the ticker:
1. Existing position is in profit (current price > average entry price)
2. Price pulls back to a new support level (support proximity triggers again)
3. RSI < 40 (slightly relaxed vs first entry)
4. Volume > 1.5x 20-day average
5. Current lots on ticker < 2 (max 2 lots per ticker)

**Entry order**: Limit buy at the support level price.

### Exit Logic

No fixed profit target. No time limit.

**Trailing stop (primary exit)**:
- Track highest price since entry per lot
- Exit when price drops 5% from peak
- Applied per-lot: each lot has its own peak and trailing stop

**Hard stop (loss protection)**:
- Exit if price drops 5% below entry price for that specific lot
- Fires before trailing stop has moved up

**Exit order**: Market sell at next open.

### Position Sizing

- 7% of current portfolio NAV per lot
- Max 2 lots per ticker (max 14% exposure per name)

### Risk Management

Portfolio-level limits (via RiskEngine):
- Max total exposure: 100% of NAV (no leverage)
- Max per-ticker exposure: 14% of NAV (2 lots x 7%)
- Max sector concentration: 30% of NAV

Kill switch / drawdown protection:
- 10% drawdown from peak NAV: pause new entries
- 20% drawdown from peak NAV: liquidate all positions

### Risk Engine Parameter Changes

| Parameter | Old Value | New Value |
|---|---|---|
| position_entry_limit_pct | 5% | 7% |
| sector_concentration_pct | 20% | 30% |
| total_exposure_limit_pct | 150% | 100% |
| max_lots_per_ticker | N/A (1 implicit) | 2 |

## Implementation Scope

### New signals
- `RSISignal` in `services/signal_generation/technical.py` -- 14-day RSI
- `VolumeSignal` in `services/signal_generation/technical.py` -- volume vs 20-day average ratio

### Modify backtest runner
- `backtest/runner.py` -- support multiple lots per ticker (positions becomes dict of lists). Per-lot trailing stop tracking.

### Modify signal function
- `scripts/run_backtest.py` -- replace `make_signals_fn()` with new entry/exit logic (support + RSI + volume, trailing stop, pyramiding). Replace SP500_TOP50 with universe filter.

### Modify risk engine
- `services/risk_management/engine.py` -- add `max_lots_per_ticker`, adjust defaults, `check_entry` accepts existing lot count.

### No changes needed
- data_ingestion, ml_model, execution, notifications, api
- shared modules (schemas, redis, logging, config, models)
