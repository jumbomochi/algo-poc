"""Ticker universes — the single source of truth.

Used by the backtest runner, the paper runner, and the data_ingestion
service, so the sleeves' universes and the data the pipeline ingests can
never silently drift apart. (Historically these lists lived in
scripts/run_backtest.py and the data_ingestion service had no universe at
all — it idled on ``no_tickers`` while the docs claimed it fetched the
watchlist.)
"""

from __future__ import annotations

# Top 50 S&P 500 by market cap (as of early 2025)
SP500_TOP50 = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "BRK B", "LLY",
    "AVGO", "JPM", "TSLA", "UNH", "XOM", "V", "MA", "PG", "COST",
    "JNJ", "HD", "ABBV", "WMT", "NFLX", "CRM", "BAC", "CVX",
    "MRK", "KO", "AMD", "PEP", "TMO", "LIN", "ACN", "CSCO", "ADBE",
    "MCD", "ABT", "WFC", "DHR", "TXN", "PM", "GE", "QCOM", "ISRG",
    "INTU", "CMCSA", "AMAT", "VZ", "NOW", "IBM", "AMGN",
]

# Inverse ETFs for bear market plays
BEAR_TICKERS = {"SH", "PSQ"}  # SH = inverse S&P 500, PSQ = inverse NASDAQ-100

# Inverse/defensive ETFs for tail-risk hedge
DEFENSIVE_TICKERS = ["SH", "PSQ", "SDS", "TLT", "GLD"]

# SPDR sector ETFs
SECTOR_ETFS = [
    "XLK", "XLE", "XLF", "XLV", "XLY", "XLP",
    "XLI", "XLB", "XLU", "XLRE", "XLC",
]

# Thematic ETFs
THEMATIC_ETFS = [
    "ARKK", "TAN", "HACK", "BOTZ", "LIT", "CIBR", "SKYY", "DRIV",
    "FINX", "GAMR", "HERO", "IDRV", "CLOU", "WCLD", "SNSR", "PRNT",
    "IZRL", "GNOM", "ARKG", "ARKQ", "ARKW", "ARKF", "ICLN", "QCLN", "PBW",
]

# S&P 500 extended (top 100 for short-term MR)
SP500_TOP100 = SP500_TOP50 + [
    "CAT", "MS", "NEE", "LOW", "UPS", "SPGI", "RTX", "HON", "ELV",
    "BLK", "SYK", "BKNG", "MDLZ", "ADP", "VRTX", "SCHW", "GILD",
    "AMT", "REGN", "LRCX", "PANW", "BSX", "CB", "MMC", "KLAC",
    "TMUS", "SHW", "SO", "EQIX", "MO", "PGR", "ZTS", "CME",
    "CI", "DUK", "ICE", "SNPS", "CL", "AON", "MCO", "WM",
    "CDNS", "TGT", "BDX", "NOC", "APH", "ITW", "FI", "HUM",
]

# Per-strategy ticker universes
UNIVERSE_REGISTRY: dict[str, list[str]] = {
    "mean_reversion": SP500_TOP50,
    "momentum": SP500_TOP50 + [t for t in sorted(BEAR_TICKERS) if t not in SP500_TOP50],
    "sector_rotation": SECTOR_ETFS,
    "quality_value": SP500_TOP100,
    "earnings_drift": SP500_TOP100,
    "short_term_mr": SP500_TOP100,
    "thematic_momentum": THEMATIC_ETFS,
    "tail_risk_hedge": DEFENSIVE_TICKERS,
}

# The sleeves actually running (mean_reversion / short_term_mr dropped
# 2026-05-26; see docs/strategies/mean-reversion-failure-analysis.md).
# Must agree with scripts/run_paper.py::CAPITAL_ALLOCATIONS.
ACTIVE_SLEEVES = [
    "momentum",
    "sector_rotation",
    "thematic_momentum",
    "quality_value",
    "earnings_drift",
    "tail_risk_hedge",
]


def get_union_universe(strategy_names: list[str]) -> list[str]:
    """Return deduplicated union of tickers across the given strategies."""
    seen: set[str] = set()
    result: list[str] = []
    for name in strategy_names:
        for ticker in UNIVERSE_REGISTRY[name]:
            if ticker not in seen:
                seen.add(ticker)
                result.append(ticker)
    return result


def resolve_watchlist(watchlist_source: str, custom_tickers: list[str]) -> list[str]:
    """Resolve the data-ingestion watchlist from config.

    Sources:
        - ``sleeves``: union universe of the active sleeves (the default —
          ingest exactly what the strategies trade)
        - ``sp500``: SP500_TOP100
        - ``custom``: only ``custom_tickers``

    ``custom_tickers`` are additive for the non-custom sources.
    Unknown sources raise so a config typo cannot silently ingest nothing.
    """
    if watchlist_source == "sleeves":
        base = get_union_universe(ACTIVE_SLEEVES)
    elif watchlist_source == "sp500":
        base = list(SP500_TOP100)
    elif watchlist_source == "custom":
        base = []
    else:
        raise ValueError(
            f"Unknown universe.watchlist_source {watchlist_source!r}; "
            "expected 'sleeves', 'sp500', or 'custom'"
        )
    seen = set(base)
    return base + [t for t in custom_tickers if t not in seen]


# Sector labels for the ETF universe (the equity map lives in
# scripts/fetch_fundamentals.SECTOR_MAP). Sector ETFs are labelled as the
# sector they hold; broad thematic/defensive instruments get honest coarse
# buckets so sector-concentration limits act on real information instead of
# a wall of "Unknown".
ETF_SECTORS: dict[str, str] = {
    "XLK": "Technology", "XLE": "Energy", "XLF": "Financials",
    "XLV": "Healthcare", "XLY": "Consumer Discretionary",
    "XLP": "Consumer Staples", "XLI": "Industrials", "XLB": "Materials",
    "XLU": "Utilities", "XLRE": "Real Estate", "XLC": "Communication Services",
    **{t: "Thematic ETF" for t in THEMATIC_ETFS},
    "TLT": "Bonds", "GLD": "Commodities",
    "SH": "Inverse ETF", "PSQ": "Inverse ETF", "SDS": "Inverse ETF",
}
