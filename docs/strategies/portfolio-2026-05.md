# Active Portfolio Configuration — 2026-05

**Status:** Current as of 2026-05-26 (paper-trading)
**Capital basis:** $100,000 reference
**Backtest horizon:** 2016-05-31 → 2026-05-22 (9.97 years, 2,510 trading days)
**Backtest JSON:** `output/backtest_multi_20260526_235302.json`

This document describes the **active sleeves** used by both
`scripts/run_backtest.py` and `scripts/run_paper.py`. For the historical
"Dual Mean-Reversion + Momentum" description and the per-signal math,
see `docs/strategy.md`. For why two sleeves were dropped, see
`docs/strategies/mean-reversion-failure-analysis.md`.

---

## Sleeve allocations

| Sleeve | Weight | $ on $100K | Universe | Signal source |
|---|---:|---:|---|---|
| `momentum` | 23.08% | $23,080 | SP500_TOP50 + BEAR_TICKERS | `make_momentum_signals_fn` |
| `earnings_drift` | 19.23% | $19,230 | SP500_TOP100 | `make_earnings_drift_signals_fn` |
| `sector_rotation` | 15.38% | $15,380 | SECTOR_ETFS (XL*) | `make_sector_rotation_signals_fn` |
| `quality_value` | 15.38% | $15,380 | SP500_TOP100 | `make_quality_value_signals_fn` |
| `thematic_momentum` | 14.10% | $14,100 | THEMATIC_ETFS (ARKK, TAN, …) | `make_thematic_momentum_signals_fn` |
| `tail_risk_hedge` | 12.83% | $12,830 | DEFENSIVE_TICKERS (TLT, GLD, SH, …) | `make_tail_risk_hedge_signals_fn` |
| **Total** | **100.00%** | **$100,000** | | |

**Source-of-truth dicts** (must agree):
- `scripts/run_backtest.py::main()` — `args.capital * 0.NNNN` in each `make_*_signals_fn` call and the matching `PortfolioConfig.capital`
- `scripts/run_paper.py::CAPITAL_ALLOCATIONS`

---

## 9.97-year backtest performance

### Aggregate

| Metric | Value |
|---|---:|
| Total return | **+420.4%** |
| **CAGR** | **17.98%** |
| Sharpe ratio | 1.97 |
| Max drawdown | 10.85% |
| Win rate | 53.82% |
| Total trades | 4,262 |
| Starting capital | $100,000 |
| Final value | $520,442.64 |

### Per-sleeve

| Sleeve | Return | CAGR | Sharpe | Max DD | Win % | Trades |
|---|---:|---:|---:|---:|---:|---:|
| thematic_momentum | +835.2% | 25.13% | 1.88 | 12.6% | 50.5% | 1,351 |
| sector_rotation | +790.0% | 24.51% | 1.80 | 14.3% | 60.8% | 571 |
| momentum | +495.6% | 19.59% | 1.57 | 14.8% | 50.6% | 639 |
| earnings_drift | +254.9% | 13.54% | 1.44 | 8.2% | 60.2% | 1,207 |
| quality_value | +118.8% | 8.17% | 0.85 | 21.3% | 59.3% | 108 |
| tail_risk_hedge | −4.1% | −0.42% | −0.08 | 14.3% | 39.4% | 386 |

---

## Benchmark comparison (same horizon, same bars)

| | Total Return | CAGR | Max DD |
|---|---:|---:|---:|
| **6-sleeve system** | +420.4% | **17.98%** | 10.85% |
| S&P 500 cap-weight (sector-ETF proxy) ≈ Amundi 500U | +310.2% | 15.20% | 34.1% |
| MSCI World (estimated) ≈ Amundi CW8 | ~+185% | ~11% | ~30% |
| SP500 Top-50 equal-weight | +980.6% | 26.95% | 40.0% |
| XLK (tech-only) | +716.2% | 23.43% | 34.0% |
| 60/40 sectors + TLT | +173.0% | 10.59% | 29.0% |

**Honest decomposition** of the +7 pp edge vs CW8:
- ~+2.78 pp = durable skill alpha (vs cap-weight S&P 500)
- ~+4 pp = US-vs-international concentration premium (regime-dependent — could
  fade or invert)

See `docs/strategies/mean-reversion-failure-analysis.md` § *Author's note on
epistemic humility* for the limits of this comparison.

---

## What was dropped on 2026-05-26

| Sleeve | Old weight | Final return | Why dropped |
|---|---:|---:|---|
| `mean_reversion` | 12% | −45.4% | No max-loss stop; trapped positions for up to 824 days. See failure analysis doc. |
| `short_term_mr` | 10% | −99.4% | Missing Connors' SMA(200) trend filter; ran in wrong universe. |

Combined $22K was redistributed proportionally (each survivor's old weight ×
100/78). Signal-function definitions remain in `scripts/run_backtest.py` for
future revival — see the failure-analysis doc for revival conditions.

---

## Source files

- **Backtest runner:** `scripts/run_backtest.py` (signals + simulator + metrics)
- **Paper runner:** `scripts/run_paper.py` (daily signal run, persists to DB)
- **Divergence monitor:** `scripts/divergence_monitor.py` (live vs backtest)
- **Backtest results:** `output/backtest_multi_20260526_235302.json` + `.html`
- **Failure analysis:** `docs/strategies/mean-reversion-failure-analysis.md`
- **Go-live checklist:** `docs/operations/go-live-checklist.md`
- **Divergence monitor operations:** `docs/operations/divergence-monitor.md`
