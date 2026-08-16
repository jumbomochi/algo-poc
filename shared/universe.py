"""Ticker universes — the single source of truth.

Used by the backtest runner, the paper runner, and the data_ingestion
service, so the sleeves' universes and the data the pipeline ingests can
never silently drift apart. (Historically these lists lived in
scripts/run_backtest.py and the data_ingestion service had no universe at
all — it idled on ``no_tickers`` while the docs claimed it fetched the
watchlist.)
"""

from __future__ import annotations

import bisect
import json
from collections.abc import Collection, Iterable, Mapping
from datetime import date

from shared.historical_sectors import HISTORICAL_SECTOR_MAP

# Top 50 S&P 500 by market cap **as of early 2025**.
#
# SURVIVORSHIP BIAS: this is a snapshot of today's winners, so using it over a
# multi-year backtest lets the strategy trade names *because* they went on to
# become mega-caps, and never sees the names that were dropped or delisted.
# The 2026-08-06 implementation review (finding 4.1) called it out as inflating
# every reported metric. Prefer a point-in-time ``MembershipCalendar`` for any
# backtest whose numbers are used to justify capital; these static lists remain
# as the live watchlist source (where "today's members" is exactly right) and as
# a clearly-labelled fallback.
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

# Portfolio names starting with this prefix are *synthetic*: they exist in the
# tables the graded readers query but must never contribute to the evidence
# record (NAV, peak NAV, divergence scoring, gate metrics). The convention
# predates this constant — it was introduced for the "_aggregate" rollup row
# after peak_nav double-counted it and read 2x NAV, tripping the circuit
# breaker. The exclusion contract for every reader lives in
# docs/operations/drill-evidence-isolation.md.
EXCLUDED_PORTFOLIO_PREFIX = "_"

# Sleeve that epoch drills book into: a drill places a real paper order and
# takes a real fill, so its trades land in the same tables the go-live gate
# reads. Tagging them keeps a drill that proves the safety machinery works
# from corrupting the record proving the strategy works (direction doc D15).
DRILL_PORTFOLIO = "__drill__"


def is_excluded_portfolio(name: str) -> bool:
    """Return True if ``name`` is a synthetic portfolio excluded from evidence.

    Covers the drill tag ("__drill__"), the "_aggregate" rollup row, and the
    "__liquidation__" kill-path fallback. Every graded reader must consult this
    rather than repeating the prefix test — see the exclusion contract in
    docs/operations/drill-evidence-isolation.md.
    """
    return name.startswith(EXCLUDED_PORTFOLIO_PREFIX)


class MembershipCalendar:
    """Point-in-time index membership: which tickers were in the universe when.

    Built from *sparse snapshots* — each snapshot date lists the constituents
    effective from that date until the next snapshot. This is what removes
    survivorship bias from a backtest: a name is only tradable on dates when it
    was actually a member, and names that were later dropped or delisted are
    still present in the history via :meth:`all_tickers`.

    Dates before the first snapshot have **no** members. Back-filling the
    earliest snapshot backwards would silently reintroduce the bias, so a
    backtest that starts before the membership history simply cannot trade —
    which is loud and obvious rather than quietly optimistic.

    ``always`` holds instruments that are tradable on every date but are not
    index constituents (the sector / thematic / inverse ETFs the sleeves use).

    The ``snapshots`` mapping is deliberately the same shape that
    ``research.factors.panel.build_factor_panel`` takes as
    ``universe_membership_by_date``, so one membership file drives both the
    backtest and the research factor panel without translation.
    """

    def __init__(
        self,
        snapshots: Mapping[date | str, Collection[str]],
        always: Iterable[str] | None = None,
    ) -> None:
        if not snapshots:
            raise ValueError(
                "MembershipCalendar requires at least one snapshot; "
                "pass point-in-time membership or omit the calendar entirely"
            )
        parsed = sorted(
            (_as_date(day), frozenset(members)) for day, members in snapshots.items()
        )
        self._dates: list[date] = [day for day, _ in parsed]
        self._members: list[frozenset[str]] = [members for _, members in parsed]
        self.always: frozenset[str] = frozenset(always or ())

    @property
    def first_snapshot_date(self) -> date:
        return self._dates[0]

    @property
    def last_snapshot_date(self) -> date:
        return self._dates[-1]

    def members_as_of(self, day: date) -> frozenset[str]:
        """Constituents effective on ``day`` (excluding ``always`` members)."""
        index = bisect.bisect_right(self._dates, day) - 1
        if index < 0:
            return frozenset()
        return self._members[index]

    def contains(self, ticker: str, day: date) -> bool:
        """Whether ``ticker`` was tradable on ``day``."""
        if ticker in self.always:
            return True
        return ticker in self.members_as_of(day)

    def all_tickers(self) -> list[str]:
        """Every ticker that was ever a member, plus the ``always`` set.

        Includes names that were later dropped or delisted — the whole point of
        a point-in-time universe is that those bars have to be fetched too.
        """
        seen: set[str] = set(self.always)
        for members in self._members:
            seen |= members
        return sorted(seen)

    @classmethod
    def from_mapping(
        cls,
        payload: Mapping,
        always: Iterable[str] | None = None,
    ) -> MembershipCalendar:
        """Build from a loaded JSON object.

        Accepts either a bare ``{date: [tickers]}`` mapping or an envelope with
        a ``snapshots`` key alongside provenance metadata.
        """
        snapshots = payload.get("snapshots", payload) if "snapshots" in payload else payload
        return cls(snapshots, always=always)

    @classmethod
    def from_json_file(
        cls,
        path: str,
        always: Iterable[str] | None = None,
    ) -> MembershipCalendar:
        """Load membership snapshots from a JSON file.

        See ``docs/operations/backtest-baseline.md`` for the file format and how
        to generate one.
        """
        with open(path) as handle:
            return cls.from_mapping(json.load(handle), always=always)


def _as_date(value: date | str) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))


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


# Sector labels for individual equities (GICS-style buckets). Lives here —
# not in scripts/ — because the risk service and fill projector must be able
# to resolve sectors: from 2026-07-19 to 2026-08-07 the projector wrote
# NULL-sector position rows and the risk service lumped the whole book into
# one "Unknown" pseudo-sector, freezing all new entries once it crossed the
# concentration limit.
SECTOR_MAP: dict[str, str] = {
    "AAPL": "Technology", "MSFT": "Technology", "NVDA": "Technology", "AMZN": "Consumer Discretionary",
    "GOOGL": "Communication Services", "META": "Communication Services", "BRK B": "Financials",
    "LLY": "Healthcare", "AVGO": "Technology", "JPM": "Financials", "TSLA": "Consumer Discretionary",
    "UNH": "Healthcare", "XOM": "Energy", "V": "Financials", "MA": "Financials",
    "PG": "Consumer Staples", "COST": "Consumer Staples", "JNJ": "Healthcare", "HD": "Consumer Discretionary",
    "ABBV": "Healthcare", "WMT": "Consumer Staples", "NFLX": "Communication Services",
    "CRM": "Technology", "BAC": "Financials", "CVX": "Energy", "MRK": "Healthcare",
    "KO": "Consumer Staples", "AMD": "Technology", "PEP": "Consumer Staples",
    "TMO": "Healthcare", "LIN": "Materials", "ACN": "Technology", "CSCO": "Technology",
    "ADBE": "Technology", "MCD": "Consumer Discretionary", "ABT": "Healthcare",
    "WFC": "Financials", "DHR": "Healthcare", "TXN": "Technology", "PM": "Consumer Staples",
    "GE": "Industrials", "QCOM": "Technology", "ISRG": "Healthcare", "INTU": "Technology",
    "CMCSA": "Communication Services", "AMAT": "Technology", "VZ": "Communication Services",
    "NOW": "Technology", "IBM": "Technology", "AMGN": "Healthcare",
    "CAT": "Industrials", "MS": "Financials", "NEE": "Utilities", "LOW": "Consumer Discretionary",
    "UPS": "Industrials", "SPGI": "Financials", "RTX": "Industrials", "HON": "Industrials",
    "ELV": "Healthcare", "BLK": "Financials", "SYK": "Healthcare", "BKNG": "Consumer Discretionary",
    "MDLZ": "Consumer Staples", "ADP": "Industrials", "VRTX": "Healthcare",
    "SCHW": "Financials", "GILD": "Healthcare", "AMT": "Real Estate", "REGN": "Healthcare",
    "LRCX": "Technology", "PANW": "Technology", "BSX": "Healthcare", "CB": "Financials",
    "MMC": "Financials", "KLAC": "Technology", "TMUS": "Communication Services",
    "SHW": "Materials", "SO": "Utilities", "EQIX": "Real Estate", "MO": "Consumer Staples",
    "PGR": "Financials", "ZTS": "Healthcare", "CME": "Financials", "CI": "Healthcare",
    "DUK": "Utilities", "ICE": "Financials", "SNPS": "Technology", "CL": "Consumer Staples",
    "AON": "Financials", "MCO": "Financials", "WM": "Industrials", "CDNS": "Technology",
    "TGT": "Consumer Discretionary", "BDX": "Healthcare", "NOC": "Industrials",
    "APH": "Technology", "ITW": "Industrials", "FI": "Financials", "HUM": "Healthcare",
}

# Sector labels for the ETF universe. Sector ETFs are labelled as the
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


def lookup_sector(ticker: str) -> str:
    """Resolve a ticker's sector: curated map, then ETFs, then index history.

    Precedence is deliberate. ``SECTOR_MAP`` is hand-curated and is what the
    live risk engine buckets currently-held names by, so it wins; the
    autogenerated :data:`~shared.historical_sectors.HISTORICAL_SECTOR_MAP`
    (KAN-23) only covers names the curated map has never had — the delisted and
    dropped members a point-in-time backtest has to price. Without that third
    tier they all resolved to "Unknown" and shared one pseudo-sector, which
    freezes every entry in an unmapped name once the bucket crosses
    ``sector_concentration_pct`` (the 2026-08-07 incident).

    Returns "Unknown" only for tickers outside every traded universe.
    """
    return (
        SECTOR_MAP.get(ticker)
        or ETF_SECTORS.get(ticker)
        or HISTORICAL_SECTOR_MAP.get(ticker)
        or "Unknown"
    )


# Tickers the connected IB Gateway cannot resolve from their current symbol,
# because its contract database still carries a pre-corporate-action symbol.
# Pinning the IB conId sidesteps the symbol entirely — conId is stable across
# ticker/exchange changes, so this keeps working even after a future rename.
#
# WORKAROUND, not a real fix: the underlying cause is a stale contract view on
# the gateway (observed 2026-08-09 — `reqContractDetails` returned the *old*
# symbols below, and the current symbols resolved to nothing → 0 bars → the
# names were silently dropped from the paper universe). If the gateway's
# contract data is ever refreshed, re-verify these conIds (`reqContractDetails`
# by conId) and delete any entry the gateway can once again resolve by symbol.
CONTRACT_CONID_OVERRIDES: dict[str, int] = {
    "MMC": 9705,    # Marsh & McLennan Cos — gateway lists stale symbol "MRSH"
    "FI": 269315,   # Fiserv Inc — gateway lists stale symbol "FISV" (pre-2023)
}


def contract_conid_for(ticker: str) -> int | None:
    """Return a pinned IB conId for *ticker*, or None if the symbol resolves
    normally. See :data:`CONTRACT_CONID_OVERRIDES`."""
    return CONTRACT_CONID_OVERRIDES.get(ticker)


def make_stock_contract(ticker: str):
    """Build an ib_insync ``Stock`` for *ticker*.

    For tickers in :data:`CONTRACT_CONID_OVERRIDES` the contract is pinned by
    conId (with SMART routing) so a stale gateway symbol can't drop the name;
    everything else uses the plain ``Stock(ticker, "SMART", "USD")`` form.
    ib_insync is imported lazily so importing this module never requires it.
    """
    from ib_insync import Stock

    conid = CONTRACT_CONID_OVERRIDES.get(ticker)
    if conid is not None:
        return Stock(conId=conid, exchange="SMART", currency="USD")
    return Stock(ticker, "SMART", "USD")
