"""Read-only near-miss ranking: which tickers were CLOSE to a buy per sleeve.

Reuses the repo's exact universes, bar fetch, regime detection, and scoring
formulas from run_backtest.py / run_paper.py. It ONLY reads IB market data +
the fundamentals cache. It does NOT touch the database and NEVER places orders.

For each ranked-selection sleeve it prints the full ranking around the top_n
cutoff: the names that WOULD buy (top_n), then the near-misses just below.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.universe import (  # noqa: E402
    ACTIVE_SLEEVES,
    BEAR_TICKERS,
    UNIVERSE_REGISTRY,
    get_union_universe,
)
from scripts.run_backtest import (  # noqa: E402
    compute_regime_by_date,
    fetch_bars_from_ib,
)
from scripts.fetch_fundamentals import (  # noqa: E402
    load_fundamentals_cache,
    build_fundamentals_lookup,
)

NEAR_MISS_SHOW = 6  # how many names below the cutoff to display


def _lookback_return_ranking(bars_by_ticker, universe, lookback_days):
    """Rank `universe` by simple close-to-close return over lookback_days.

    Mirrors momentum / sector_rotation / thematic scoring exactly.
    """
    # date -> {ticker: close}
    price_by_date: dict = {}
    for ticker, bars in bars_by_ticker.items():
        if ticker not in universe:
            continue
        for bar in bars:
            price_by_date.setdefault(bar["date"], {})[ticker] = bar["close"]
    sorted_dates = sorted(price_by_date.keys())
    if len(sorted_dates) <= lookback_days:
        return [], None
    d = sorted_dates[-1]
    past = sorted_dates[-1 - lookback_days]
    cur_prices = price_by_date[d]
    past_prices = price_by_date.get(past, {})
    returns = []
    for t in cur_prices:
        if t in past_prices and past_prices[t] > 0:
            returns.append((t, (cur_prices[t] - past_prices[t]) / past_prices[t]))
    returns.sort(key=lambda x: x[1], reverse=True)
    return returns, d


def _above_ma(bars, ma_period=50):
    closes = [b["close"] for b in bars]
    if len(closes) < ma_period + 1:
        return None
    ma = sum(closes[-ma_period:]) / ma_period
    return closes[-1] > ma, closes[-1], ma


def _quality_score(f):
    roe = f.get("roe", 0.0)
    de = f.get("debt_equity", 0.0)
    margin = f.get("profit_margin", 0.0)
    return (roe / 0.20 + max(0.0, 1.0 - de / 2.0) + margin / 0.25) / 3.0


def _print_ranked(title, ranked, top_n, fmt=lambda v: f"{v:+.2%}", extra=None):
    print(f"\n{'='*70}\n{title}  (buys top {top_n})\n{'='*70}")
    if not ranked:
        print("  (insufficient history)")
        return
    print(f"  {'rank':>4}  {'ticker':<7}{'score':>11}   status")
    for i, (t, v) in enumerate(ranked[: top_n + NEAR_MISS_SHOW], start=1):
        if i <= top_n:
            status = "BUY  <-- in cutoff"
        elif i == top_n + 1:
            status = "NEAR MISS (#1 below cutoff)"
        else:
            status = f"near miss (+{i - top_n} below)"
        note = ""
        if extra:
            note = extra(t)
        print(f"  {i:>4}  {t:<7}{fmt(v):>11}   {status}{note}")


def main():
    all_tickers = get_union_universe(ACTIVE_SLEEVES)
    print(f"Fetching bars for {len(all_tickers)} tickers (1 year) -- read-only...")
    bars = fetch_bars_from_ib(tickers=all_tickers, years=1)
    if not bars:
        print("ERROR: no bars fetched (IB Gateway reachable?)")
        sys.exit(1)

    regime_by_date = compute_regime_by_date(bars)
    latest = max(max(b["date"] for b in bl) for bl in bars.values() if bl)
    regime = regime_by_date.get(latest, "neutral")
    print(f"\nData as-of {latest}; detected regime: {regime}")
    print(f"(bear inverse ETFs BEAR_TICKERS={sorted(BEAR_TICKERS)} only rank in bear regime)")

    # --- momentum: all tickers, 126d return, top 5 ---
    mom_universe = set(bars)
    mom, _ = _lookback_return_ranking(bars, mom_universe, 126)
    if regime != "bear":
        mom = [(t, v) for t, v in mom if t not in BEAR_TICKERS]
    _print_ranked("MOMENTUM (126-day return, full universe)", mom, 5)

    # --- sector_rotation: all tickers, 63d return, top 3 ---
    sec, _ = _lookback_return_ranking(bars, set(bars), 63)
    if regime == "bear":
        defensive = {"XLU", "XLP", "XLV"}
        sec = [(t, v) for t, v in sec if t in defensive]
    _print_ranked("SECTOR_ROTATION (63-day return, full universe)", sec, 3)

    # --- thematic_momentum: thematic ETFs, 63d return, top 8, +50d MA filter ---
    them_universe = set(UNIVERSE_REGISTRY["thematic_momentum"])
    them, _ = _lookback_return_ranking(bars, them_universe, 63)

    def _them_note(t):
        res = _above_ma(bars.get(t, []), 50)
        if res is None:
            return "  [MA n/a]"
        ok, px, ma = res
        return f"  [{'ABOVE' if ok else 'BELOW-MA*'} 50d MA {ma:.2f}]"

    _print_ranked(
        "THEMATIC_MOMENTUM (63-day return, thematic ETFs)",
        them, 8, extra=_them_note,
    )
    print("  *BELOW-MA names inside the top-8 are ranked in but BLOCKED by the 50d MA entry filter")

    # --- quality_value: fundamentals composite, top 15 ---
    fundamentals = build_fundamentals_lookup(
        load_fundamentals_cache("data/cache/fundamentals.json")
    )
    qv_universe = UNIVERSE_REGISTRY["quality_value"]
    qv_scores = []
    for t in qv_universe:
        f = fundamentals(t, latest)
        if f is not None:
            qv_scores.append((t, _quality_score(f)))
    qv_scores.sort(key=lambda x: x[1], reverse=True)
    _print_ranked(
        "QUALITY_VALUE (fundamentals composite score)",
        qv_scores, 15, fmt=lambda v: f"{v:8.3f}",
    )

    print("\nNote: tail_risk_hedge is rule-based (regime-driven TLT/GLD) and")
    print("earnings_drift is event-driven -- neither is a ranked selection,")
    print("so 'near miss' does not apply to them.")


if __name__ == "__main__":
    main()
