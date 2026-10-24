#!/usr/bin/env python3
"""Refresh fundamentals.json for quality_value from SEC EDGAR companyfacts.

Replaces the Alpha Vantage statement fill (KAN-110), which the free tier's 25
calls a day paced at three tickers a run, so quality_value stayed degraded for
weeks. EDGAR is free, keyless and unmetered (fair access: at most 10 requests a
second, and a User-Agent naming a contact), so one run fetches every live
ticker.

Every figure is taken **as first filed**: of all the facts reported for a
period (the original 10-Q, then the comparatives in later filings, which may be
restated), the earliest-filed one. Each row carries that ``filing_date``, which
``build_fundamentals_lookup`` uses as its availability date, so the cache is
point-in-time and the whole cache can be rebuilt from scratch every run.

Per quarter end:

* net income: ``NetIncomeLoss``, else ``ProfitLoss``;
* revenue: the first of ``_REVENUE`` that has the quarter;
* equity: ``StockholdersEquity``, else the figure including noncontrolling
  interest;
* debt: long-term debt including current maturities plus short-term
  borrowings (``_total_debt``). Concepts vary by filer, so this is one rule
  applied to every name, not each company's own definition.

A quarter's flows are the three-month facts. The fourth quarter has none (the
10-K reports the year), so it is the annual figure less the year's first three
quarters, all from the same concept.

``fetched_at`` is stamped only when the run reached every live ticker and at
most 20% of them have no quarter (THIN otherwise). The secret
``SEC_USER_AGENT`` (``"algo-poc you@example.com"``) comes from the environment
or the login keychain, as the Alpha Vantage key does, and is never printed.

Exit codes: 0 current, 1 could not run, 2 failed or THIN, 75 another refresh
holds the cache lock.
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from shared.data_cache import (  # noqa: E402
    FUNDAMENTALS_FILE,
    read_cache_document,
    resolve_cache_dir,
    write_cache_document,
)
from shared.universe import SECTOR_MAP  # noqa: E402

SOURCE = "edgar"
USER_AGENT_VAR = "SEC_USER_AGENT"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"

_NET_INCOME = ("NetIncomeLoss", "ProfitLoss")
_REVENUE = (
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenuesNetOfInterestExpense",
    "SalesRevenueNet",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
)
_EQUITY = ("StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest")
_DEBT_INCL_CURRENT = (
    "LongTermDebt",
    "LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities",
    "DebtAndCapitalLeaseObligations",
)
_DEBT_NONCURRENT = ("LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligations")
_DEBT_CURRENT = ("LongTermDebtCurrent", "DebtCurrent")
_SHORT_TERM = ("ShortTermBorrowings", "CommercialPaper")

_QUARTER_DAYS = (80, 100)
_YEAR_DAYS = (350, 380)

Transport = Callable[[str, str, float], bytes]


def _urllib_transport(url: str, user_agent: str, timeout: float) -> bytes:
    import ssl
    import urllib.request

    import certifi

    context = ssl.create_default_context(cafile=certifi.where())
    request = urllib.request.Request(
        url, headers={"User-Agent": user_agent, "Accept-Encoding": "gzip"}
    )
    with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
        body = response.read()
        return gzip.decompress(body) if response.headers.get("Content-Encoding") == "gzip" else body


class EdgarClient:
    """Rate-spaced GETs that always carry the contact User-Agent."""

    def __init__(self, user_agent: str, *, transport: Transport | None = None,
                 min_interval_seconds: float = 0.15, timeout_seconds: float = 30.0,
                 sleep: Callable[[float], None] = time.sleep,
                 monotonic: Callable[[], float] = time.monotonic):
        if not user_agent:
            raise ValueError(f"{USER_AGENT_VAR} is required by SEC fair access")
        self._ua = user_agent
        self._transport = transport or _urllib_transport
        self._interval = min_interval_seconds
        self._timeout = timeout_seconds
        self._sleep = sleep
        self._monotonic = monotonic
        self._last: float | None = None
        self.calls = 0

    def get_json(self, url: str) -> Any:
        if self._last is not None:
            wait = self._interval - (self._monotonic() - self._last)
            if wait > 0:
                self._sleep(wait)
        self.calls += 1
        try:
            return json.loads(self._transport(url, self._ua, self._timeout))
        finally:
            self._last = self._monotonic()


def edgar_ticker(ticker: str) -> str:
    """SEC's spelling: ``BRK B`` and ``BRK.B`` are ``BRK-B``."""
    return ticker.strip().upper().replace(" ", "-").replace(".", "-")


def cik_map(payload: Mapping[str, Any]) -> dict[str, int]:
    return {str(v["ticker"]).upper(): int(v["cik_str"]) for v in payload.values()}


# ---------------------------------------------------------------------------
# Parsing companyfacts
# ---------------------------------------------------------------------------


def _facts(gaap: Mapping[str, Any], concept: str) -> list[dict]:
    units = (gaap.get(concept) or {}).get("units") or {}
    return [f for f in units.get("USD", []) if str(f.get("form", "")).startswith(("10-Q", "10-K"))]


def _days(fact: Mapping[str, Any]) -> int | None:
    if not fact.get("start"):
        return None
    return (date.fromisoformat(fact["end"]) - date.fromisoformat(fact["start"])).days


def _first_filed(facts: list[dict]) -> tuple[float, str] | None:
    if not facts:
        return None
    first = min(facts, key=lambda f: f["filed"])
    return float(first["val"]), first["filed"]


def _instant(gaap: Mapping[str, Any], concepts: tuple[str, ...], end: str) -> tuple[float, str] | None:
    for concept in concepts:
        hit = _first_filed([f for f in _facts(gaap, concept) if f["end"] == end and not f.get("start")])
        if hit:
            return hit
    return None


def _quarterly(gaap: Mapping[str, Any], concept: str) -> dict[str, tuple[float, str]]:
    """``end -> (value, filed)`` for every quarter of ``concept``, Q4 derived."""
    facts = _facts(gaap, concept)
    by_end: dict[str, list[dict]] = {}
    years: dict[tuple[str, str], list[dict]] = {}
    for f in facts:
        d = _days(f)
        if d is None:
            continue
        if _QUARTER_DAYS[0] <= d <= _QUARTER_DAYS[1]:
            by_end.setdefault(f["end"], []).append(f)
        elif _YEAR_DAYS[0] <= d <= _YEAR_DAYS[1]:
            years.setdefault((f["start"], f["end"]), []).append(f)
    out = {end: hit for end, fs in by_end.items() if (hit := _first_filed(fs))}
    for (start, end), fs in years.items():
        if end in out:
            continue
        inside = [v for e, v in out.items() if start < e < end]
        annual = _first_filed(fs)
        if annual and len(inside) == 3:
            out[end] = (annual[0] - sum(v for v, _ in inside), max(annual[1], *(f for _, f in inside)))
    return out


def _total_debt(gaap: Mapping[str, Any], end: str) -> tuple[float, str | None]:
    long = _instant(gaap, _DEBT_INCL_CURRENT, end)
    if long is None:
        noncurrent = _instant(gaap, _DEBT_NONCURRENT, end)
        current = _instant(gaap, _DEBT_CURRENT, end)
        if noncurrent or current:
            long = ((noncurrent or (0.0, ""))[0] + (current or (0.0, ""))[0],
                    max((noncurrent or (0.0, ""))[1], (current or (0.0, ""))[1]))
    short = _instant(gaap, _SHORT_TERM, end)
    parts = [p for p in (long, short) if p]
    # No debt concept at all reads as no debt, as Alpha Vantage's all-"None" did.
    return sum(v for v, _ in parts), max((f for _, f in parts), default=None)


def parse_companyfacts(ticker: str, payload: Mapping[str, Any]) -> list[dict]:
    """Quarterly rows in the cache's shape, oldest first, each as first filed."""
    gaap = (payload.get("facts") or {}).get("us-gaap") or {}
    income = next((q for c in _NET_INCOME if (q := _quarterly(gaap, c))), {})
    revenues = [_quarterly(gaap, c) for c in _REVENUE]
    rows = []
    for end, (net_income, ni_filed) in income.items():
        revenue = next((r[end] for r in revenues if end in r), None)
        equity = _instant(gaap, _EQUITY, end)
        if revenue is None or equity is None:
            continue
        total_revenue, total_equity = revenue[0], equity[0]
        total_debt, debt_filed = _total_debt(gaap, end)
        rows.append({
            "report_date": end,
            "roe": round(net_income / total_equity, 4) if total_equity > 0 else 0.0,
            "debt_equity": round(total_debt / total_equity, 4) if total_equity > 0 else 0.0,
            "profit_margin": round(net_income / total_revenue, 4) if total_revenue > 0 else 0.0,
            "net_income": net_income,
            "total_revenue": total_revenue,
            "total_equity": total_equity,
            "total_debt": total_debt,
            "sector": SECTOR_MAP.get(ticker, "Unknown"),
            "source": SOURCE,
            "reported_currency": "USD",
            "filing_date": max(f for f in (ni_filed, revenue[1], equity[1], debt_filed) if f),
        })
    return sorted(rows, key=lambda r: r["report_date"])


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def run_edgar_refresh(
    *,
    client: EdgarClient,
    cache_dir: Path,
    live: list[str],
    symbol_overrides: Mapping[str, str] | None = None,
    max_missing_fraction: float = 0.20,
    stamp_allowed: bool = True,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    log: Callable[[str], None] = print,
) -> tuple[str, int]:
    """Rebuild every live ticker's rows; returns ``(summary_line, exit_code)``."""
    from scripts.fetch_earnings import EXIT_CURRENT, EXIT_FAILED

    started = now()
    path = cache_dir / FUNDAMENTALS_FILE
    previous = read_cache_document(path)
    if previous.error:
        raise RuntimeError(f"{path} exists but is unreadable ({previous.error}); refusing to overwrite it")
    # Only EDGAR rows survive: one ticker never mixes sources, and a cross-
    # sectional rank never mixes two filers' definitions of debt.
    cache = {t: rows for t, rows in previous.data.items() if rows and rows[0].get("source") == SOURCE}

    ciks = cik_map(client.get_json(TICKERS_URL))
    unmapped, failed, no_data = [], [], []
    for i, ticker in enumerate(live, 1):
        cik = ciks.get(edgar_ticker((symbol_overrides or {}).get(ticker, ticker)))
        if cik is None:
            unmapped.append(ticker)
            continue
        rows = None
        for attempt in (1, 2):  # one retry: SEC's API times out transiently
            try:
                rows = parse_companyfacts(ticker, client.get_json(FACTS_URL.format(cik=cik)))
                break
            except Exception as exc:  # noqa: BLE001 -- one bad filer must not stop the run
                log(f"  [{i}/{len(live)}] {ticker} attempt {attempt}: {type(exc).__name__}: {exc}")
        if rows is None:
            failed.append(ticker)
            continue
        if rows:
            cache[ticker] = rows
        else:
            no_data.append(ticker)
    missing = [t for t in live if not cache.get(t)]
    thin = bool(live) and len(missing) / len(live) > max_missing_fraction
    current = stamp_allowed and not thin and not failed
    write_cache_document(
        path, cache, fetched_at=started if current else previous.fetched_at,
        fetch_time_unknown=not current and previous.fetched_at is None, source=SOURCE,
    )
    status = "CURRENT" if current else ("SUBSET" if not stamp_allowed else ("THIN" if thin else "FAILED"))
    for label, names in (("unmapped", unmapped), ("failed", failed), ("no_data", no_data)):
        if names:
            log(f"{label}: {', '.join(names)}")
    if no_data or unmapped:
        log(f"LIVE_NO_DATA: {len(no_data) + len(unmapped)} live ticker(s) with no EDGAR quarter: "
            f"{', '.join(no_data + unmapped)}")
    stamp = started.isoformat() if current else (previous.fetched_at.isoformat() if previous.fetched_at else "unknown")
    summary = (f"FUNDAMENTALS_REFRESH source=edgar status={status} calls={client.calls} "
               f"tickers={len(live) - len(missing)}/{len(live)} failed={len(failed)} fetched_at={stamp}")
    return summary, (EXIT_CURRENT if current or not stamp_allowed else EXIT_FAILED)


def main(argv: list[str] | None = None, *, transport: Transport | None = None,
         env: Mapping[str, str] | None = None) -> int:
    from scripts.fetch_earnings import EXIT_ERROR, EXIT_LOCKED, LockHeld, _CacheLock, load_api_key
    from scripts.fetch_fundamentals import live_universe
    from shared.data_cache import configured_data_config

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tickers", default=None, help="comma-separated subset (never stamps fetched_at)")
    parser.add_argument("--cache-dir", default=None)
    args = parser.parse_args(argv)

    data_cfg = configured_data_config()
    cache_dir = resolve_cache_dir(args.cache_dir or data_cfg.cache_dir)
    live = [t.strip() for t in args.tickers.split(",")] if args.tickers else live_universe()
    user_agent = load_api_key(env, USER_AGENT_VAR)
    if not user_agent:
        print(f"ERROR: {USER_AGENT_VAR} is not set and the keychain (service algo-poc) has no item "
              f"for it. Import it: deploy/launchd/secrets.sh --import --only {USER_AGENT_VAR}", file=sys.stderr)
        print("FUNDAMENTALS_REFRESH source=edgar status=ERROR reason='no SEC User-Agent'")
        return EXIT_ERROR
    print(f"Fundamentals refresh (SEC EDGAR): {len(live)} tickers, cache {cache_dir}")
    try:
        with _CacheLock(cache_dir):
            summary, code = run_edgar_refresh(
                client=EdgarClient(user_agent, transport=transport),
                cache_dir=cache_dir, live=live, stamp_allowed=not args.tickers,
                # Renamed listings (MMC -> MRSH, FI -> FISV) as the earnings refresh maps them.
                symbol_overrides=data_cfg.earnings.refresh.symbol_overrides,
            )
    except LockHeld as exc:
        print(f"FUNDAMENTALS_REFRESH source=edgar status=SKIPPED reason={str(exc)!r}")
        return EXIT_LOCKED
    except Exception as exc:  # noqa: BLE001 -- the ticker map itself failed
        print(f"FUNDAMENTALS_REFRESH source=edgar status=ERROR reason={type(exc).__name__}: {exc}")
        return EXIT_ERROR
    print(summary)
    return code


if __name__ == "__main__":
    sys.exit(main())
