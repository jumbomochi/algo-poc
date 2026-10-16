#!/usr/bin/env python3
"""Refresh the fundamentals cache from Alpha Vantage, keeping each quarter as first seen.

KAN-110. ``quality_value`` ranks on ``fundamentals.json`` in ``data.cache_dir``
(ROE, debt/equity and margin from the newest quarter available on a date).
This script keeps that file current. It runs daily as the second step of the
04:45 earnings full run (``deploy/launchd/run_earnings_refresh.sh``), after
the earnings fetch and under the same lock, on the same free-tier quota.

Usage::

    python scripts/fetch_fundamentals.py                  # live universe, AV
    python scripts/fetch_fundamentals.py --dry-run        # the plan, no calls
    python scripts/fetch_fundamentals.py --tickers AAPL,MSFT --cache-dir /tmp/x
    python scripts/fetch_fundamentals.py --source yfinance --output PATH  # legacy

The API key is read as ``scripts/fetch_earnings.py`` reads it: the environment,
else the login keychain (service ``algo-poc``). It is never printed.

As first seen, never overwritten
--------------------------------
Alpha Vantage serves each quarter's LATEST figures. When a company recasts a
period — a spin-off, a discontinued operation — the old quarter changes
(probe of 2026-10-16 against SEC companyfacts: GE's 2024-Q1 revenue is 8.955B
in Alpha Vantage, 16.053B as first filed; its 2022-Q3 net income +161M, first
filed as -165M). A backtest or shadow fed that history ranks on numbers nobody
had at the time. So a quarter is stored only when it is first seen within
``capture_window_days`` (135) of its period end — before the next quarter's
filing could have restated it — and a stored quarter is never replaced. The
earlier quarters each response carries are discarded. Every stored row carries
``first_seen_at``.

The consequence is a shallow history that deepens by one quarter per quarter.
That is honest for live trading, which reads only the newest quarter. A
backtest or the shadow whose start predates the history is marked
``SHALLOW`` (:func:`history_coverage_problem`) and degraded, as it was when
there was no cache at all.

One run
-------
At most ``calls_per_run`` requests (two per ticker: INCOME_STATEMENT, then
BALANCE_SHEET), in this order:

a. live tickers **never fetched** from Alpha Vantage, newest reported quarter
   (per ``earnings.json``) first, so an early reporter's first fetch already
   holds its newest quarter;
b. live tickers **behind**: ``earnings.json`` shows a quarter reported at least
   ``statement_lag_days`` ago that is newer than the newest stored one, and the
   ticker was not asked in the last ``retry_days`` days; oldest report first;
c. live tickers **aging**, asked at most every ``aging_retry_days`` days:
   one the earnings cache knows nothing about whose newest stored quarter is
   missing or over ``aging_days`` old, or one whose newer quarter Alpha
   Vantage has already offered and could not be stored.

Failures are handled as in the earnings refresh: a quota notice stops the run,
a transport or malformed response is retried once and then skips the ticker
(``max_consecutive_errors`` in a row stop the run), an ``Error Message``
refuses that symbol, and a live ticker refused on ``refusal_exclude_after_days``
consecutive days is excluded from the stamp gate (``LIVE_REFUSED``).

What ``fetched_at`` means
-------------------------
The START of the most recent run over the whole live universe (never a
``--tickers`` subset) that was not stopped or terminated, after which every
live ticker (bar the excluded) had been fetched from Alpha Vantage at least
once, and at most ``max_stale_ticker_fraction`` (20%) of them had no stored
quarter — the freshness rule only judges tickers that have rows, so a cache
missing names entirely would otherwise read fresh (THIN, exit 2). Whether each ticker's quarter is current is the period rule's
job (``data.fundamentals.max_period_age_days``), judged on the rows. A run
short of that bar saves its rows and leaves ``fetched_at`` where it was —
``null`` until the first complete pass — so the sleeve stays degraded rather
than ranking a partial universe.

Exit codes: as ``scripts/fetch_earnings.py`` — 0 current, 1 could not run,
2 Alpha Vantage refused or failed before the live universe was current,
3 the budget ran out first (the initial fill), 75 another refresh holds the
lock, 124 terminated by SIGTERM (progress saved).
"""
from __future__ import annotations

import argparse
import json
import signal
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

# Run by path (``python scripts/fetch_*.py``), which puts scripts/ rather than
# the repo root on sys.path; pin the root so ``shared`` is THIS checkout's.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


from shared.data_cache import (  # noqa: E402
    EARNINGS_FILE,
    FUNDAMENTALS_FILE,
    FUNDAMENTALS_STATE_FILE,
    read_cache_document,
    resolve_cache_dir,
    write_cache_document,
    write_json_atomic,
)


# The equity sector map moved to shared/universe.py so services (risk
# management, fill projector) can resolve sectors too. Re-exported here
# for existing importers (run_paper.py, validate_replacement_policy.py).
from shared.universe import SECTOR_MAP  # noqa: F401, E402

# Calendar days after a fiscal period-end before its figures are assumed to be
# public. Neither Alpha Vantage's nor yfinance's quarterly statements carry a
# filing date, so this stands in for one. 45 days clears the SEC's 40-day 10-Q
# deadline for large accelerated filers with a small margin; the annual 10-K
# deadline is 60 days, so pass a larger lag if the cache mixes in
# fiscal-year-end periods and you want to be strict about those too. Rows that
# do carry an explicit ``filing_date`` use it instead of this lag.
DEFAULT_FILING_LAG_DAYS = 45

SOURCE = "alphavantage"
STATE_FORMAT_VERSION = 1


def save_fundamentals_cache(
    data: dict[str, list[dict]],
    path: str,
    *,
    fetched_at: datetime | None = None,
    source: str | None = None,
) -> None:
    """Save fundamentals rows, stamped with when they were fetched (KAN-109).

    ``fetched_at`` defaults to now; pass the instant the fetch STARTED.
    """
    write_cache_document(path, data, fetched_at=fetched_at, source=source)


def load_fundamentals_cache(path: str) -> dict[str, list[dict]]:
    """Load fundamentals rows. Returns empty dict if file missing.

    Reads both the KAN-109 envelope and the legacy bare ``{ticker: rows}``
    mapping, and returns only the rows, so the lookup builders never see the
    metadata. Use :func:`shared.data_cache.read_cache_document` for the fetch
    time and whether the file was there at all — this function cannot tell a
    missing cache from an empty one, which is how KAN-109 went unnoticed.
    """
    return read_cache_document(path).data


def _available_on(row: Mapping[str, Any], lag: timedelta) -> date:
    filed = row.get("filing_date")
    available = _as_date(filed) if filed else _as_date(row["report_date"]) + lag
    # A quarter this cache first saw later than that was not in it before: a
    # replay must not use it earlier than the cache could have (KAN-110).
    seen = row.get("first_seen_at")
    if seen:
        available = max(available, _as_date(str(seen)[:10]))
    return available


def build_fundamentals_lookup(
    cache: dict[str, list[dict]],
    filing_lag_days: int = DEFAULT_FILING_LAG_DAYS,
) -> Callable[[str, date], dict | None]:
    """Build a point-in-time fundamentals lookup function.

    Returns a function(ticker, as_of_date) -> dict | None giving the most
    recent report **available** on ``as_of_date``.

    Availability is the filing date, not the fiscal period-end: a quarter
    ending 31 March is not public knowledge on 31 March. Each row's
    availability is:

    - its explicit ``filing_date``, when the data source provides one; else
    - ``report_date + filing_lag_days`` (see ``DEFAULT_FILING_LAG_DAYS``).

    Keying off ``report_date`` — as this function used to — leaks figures weeks
    early into both the backtest and live paper trading (finding 4.4 of the
    2026-08-06 implementation review). Reports are ordered by availability, so
    a late-filed or restated earlier period cannot shadow one that was already
    public.
    """
    if filing_lag_days < 0:
        raise ValueError(f"filing_lag_days must be >= 0, got {filing_lag_days}")

    lag = timedelta(days=filing_lag_days)
    sorted_cache: dict[str, list[tuple[date, dict]]] = {}
    for ticker, reports in cache.items():
        entries = [(_available_on(r, lag), r) for r in reports]
        entries.sort(key=lambda x: x[0])
        sorted_cache[ticker] = entries

    def lookup(ticker: str, as_of_date: date) -> dict | None:
        entries = sorted_cache.get(ticker)
        if not entries:
            return None
        result = None
        for available_date, report in entries:
            if available_date <= as_of_date:
                result = report
            else:
                break
        return result

    return lookup


def history_coverage_problem(
    cache: Mapping[str, list[dict]],
    *,
    start: date,
    tickers: Iterable[str],
    max_missing_fraction: float,
    filing_lag_days: int = DEFAULT_FILING_LAG_DAYS,
) -> str | None:
    """Why ``cache`` cannot serve a replay that starts on ``start``, or None.

    The freshness rules (``shared.data_cache.assess_fundamentals``) judge a
    cache at one instant: the newest bar, for a backtest. A cache kept as
    first seen (KAN-110) is fresh there and empty years earlier, so a 10-year
    backtest would rank quality_value on nothing for most of its span and
    still be called healthy. This is the same 20% bound turned the other way:
    more than ``max_missing_fraction`` of ``tickers`` with no quarter
    available on ``start`` is ``SHALLOW``.
    """
    names = sorted(set(tickers))
    if not names:
        return None
    lag = timedelta(days=filing_lag_days)
    missing = [
        t for t in names
        if not any(_available_on(r, lag) <= start for r in cache.get(t, ()) if r.get("report_date"))
    ]
    share = len(missing) / len(names)
    if share <= max_missing_fraction:
        return None
    earliest = min(
        (_available_on(r, lag) for t in names for r in cache.get(t, ()) if r.get("report_date")),
        default=None,
    )
    return (
        f"SHALLOW: {len(missing)} of {len(names)} tickers have no quarter available on "
        f"{start}, the replay's start (history begins "
        f"{earliest.isoformat() if earliest else 'nowhere'}); the cache keeps each "
        "quarter as first seen, so its history only grows forward (KAN-110)"
    )


def _as_date(value: date | str) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))


# ---------------------------------------------------------------------------
# Alpha Vantage statements
# ---------------------------------------------------------------------------


def _quarters(payload: Mapping[str, Any] | None) -> dict[str, Mapping[str, Any]]:
    """``fiscalDateEnding`` -> quarterly report, for one statement payload."""
    from scripts.fetch_earnings import _iso_date

    out: dict[str, Mapping[str, Any]] = {}
    for quarter in (payload or {}).get("quarterlyReports") or []:
        if not isinstance(quarter, Mapping):
            continue
        period = _iso_date(quarter.get("fiscalDateEnding"))
        if period:
            out[period] = quarter
    return out


def _text(value: Any) -> str | None:
    """A string field, or None for Alpha Vantage's literal "None" (JPM, 2026-10-17)."""
    text = str(value or "").strip()
    return None if text in ("", "None") else text


def _nearest_quarter(
    quarters: Mapping[str, Mapping[str, Any]], period: str
) -> Mapping[str, Any] | None:
    """The quarter dated within ``_SAME_QUARTER`` of ``period``, if exactly one.

    The two endpoints can date a 52/53-week filer's quarter a few days apart;
    the planner already treats such dates as one quarter, so the parser must.
    """
    target = _as_date(period)
    close = [q for p, q in quarters.items() if abs(_as_date(p) - target) <= _SAME_QUARTER]
    return close[0] if len(close) == 1 else None


def _total_debt(balance: Mapping[str, Any]) -> float:
    """Short plus long-term debt. Alpha Vantage's own total when present."""
    from scripts.fetch_earnings import _number

    total = _number(balance.get("shortLongTermDebtTotal"))
    if total is not None:
        return total
    short = _number(balance.get("shortTermDebt"))
    if short is None:
        short = _number(balance.get("currentDebt"))
    long = _number(balance.get("longTermDebt"))
    # Every field "None" reads as no debt, as yfinance's missing "Total Debt"
    # always did: a missing figure would otherwise drop a debt-free company.
    return (short or 0.0) + (long or 0.0)


def parse_statements(
    ticker: str,
    income: Mapping[str, Any] | None,
    balance: Mapping[str, Any] | None,
) -> list[dict]:
    """Quarterly rows in the cache's shape, oldest first.

    A quarter needs both statements, net income, revenue and equity. The ratios
    are the quarterly ones the yfinance fetcher computed, so the scores keep
    their basis: ROE and margin from one quarter's net income.
    """
    from scripts.fetch_earnings import _number

    balances = _quarters(balance)
    rows = []
    for period, inc in _quarters(income).items():
        bal = balances.get(period) or _nearest_quarter(balances, period)
        if bal is None:
            continue
        net_income = _number(inc.get("netIncome"))
        total_revenue = _number(inc.get("totalRevenue"))
        total_equity = _number(bal.get("totalShareholderEquity"))
        if net_income is None or total_revenue is None or total_equity is None:
            continue
        total_debt = _total_debt(bal)
        rows.append({
            "report_date": period,
            "roe": round(net_income / total_equity, 4) if total_equity > 0 else 0.0,
            "debt_equity": round(total_debt / total_equity, 4) if total_equity > 0 else 0.0,
            "profit_margin": round(net_income / total_revenue, 4) if total_revenue > 0 else 0.0,
            "net_income": net_income,
            "total_revenue": total_revenue,
            "total_equity": total_equity,
            "total_debt": total_debt,
            "sector": SECTOR_MAP.get(ticker, "Unknown"),
            "source": SOURCE,
            "reported_currency": _text(inc.get("reportedCurrency")),
        })
    return sorted(rows, key=lambda r: r["report_date"])


def _is_av_row(row: Mapping[str, Any]) -> bool:
    return row.get("source") == SOURCE


def merge_first_seen(
    old: list[dict] | None,
    new: list[dict],
    *,
    seen_at: datetime,
    capture_window_days: int,
) -> tuple[list[dict], list[str]]:
    """Add the quarters of ``new`` that are first seen in time; replace nothing.

    Returns ``(rows, added_periods)``. A stored quarter is never touched, so
    a later restatement cannot reach the cache. A quarter not yet stored is
    added only if ``seen_at`` is within ``capture_window_days`` of its period
    end. Rows from another source (the legacy yfinance cache, restated and
    undated) are dropped once an Alpha Vantage quarter is stored for the
    ticker, so one ticker never mixes the two.
    """
    kept = [dict(r) for r in (old or []) if _is_av_row(r)]
    legacy = [r for r in (old or []) if not _is_av_row(r)]
    stored = {r["report_date"] for r in kept}
    window = timedelta(days=capture_window_days)
    seen_day = seen_at.astimezone(timezone.utc).date()
    added = []
    for row in new:
        period = row["report_date"]
        if period in stored or seen_day > _as_date(period) + window:
            continue
        kept.append({**row, "first_seen_at": seen_at.astimezone(timezone.utc).isoformat()})
        stored.add(period)
        added.append(period)
    if not kept:
        # Nothing of Alpha Vantage's is storable yet: keep what was there
        # rather than leave the ticker with no rows at all.
        return list(old or []), added
    return sorted(kept, key=lambda r: r["report_date"]), added


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


def latest_reported_quarter(rows: Iterable[Mapping[str, Any]]) -> tuple[date, date] | None:
    """``(fiscal period, report date)`` of a ticker's newest earnings actual."""
    best: tuple[date, date] | None = None
    for row in rows:
        period, reported = row.get("fiscal_period"), row.get("earnings_date")
        if not period or not reported:
            continue
        candidate = (_as_date(period), _as_date(reported))
        if best is None or candidate > best:
            best = candidate
    return best


@dataclass
class FundamentalsPlan:
    never: list[str]
    behind: list[str]
    aging: list[str]
    selected: list[str]
    excluded: list[str] = field(default_factory=list)


# Fiscal period ends from the two Alpha Vantage endpoints may differ by a few
# days for a 52/53-week filer; a gap this small is the same quarter.
_SAME_QUARTER = timedelta(days=7)


def plan_fundamentals(
    *,
    live: list[str],
    cache: Mapping[str, list[dict]],
    state: Mapping[str, dict],
    earnings: Mapping[str, list[dict]],
    now: datetime,
    budget: int,
    statement_lag_days: int,
    retry_days: int,
    aging_days: int,
    aging_retry_days: int,
    refusal_limit: int,
) -> FundamentalsPlan:
    """Which live tickers to fetch this run, in priority order (module docstring)."""
    from scripts.fetch_earnings import _parse_instant, is_excluded

    today = now.astimezone(timezone.utc).date()

    def asked_within(ticker: str, days: int) -> bool:
        last = _parse_instant((state.get(ticker) or {}).get("last_attempt_at"))
        return last is not None and now - last < timedelta(days=days)

    def newest_stored(ticker: str) -> date | None:
        periods = [_as_date(r["report_date"]) for r in cache.get(ticker, ()) if _is_av_row(r)]
        return max(periods) if periods else None

    excluded = [t for t in live if is_excluded(state.get(t), refusal_limit)]
    never: list[tuple[Any, ...]] = []
    behind: list[tuple[Any, ...]] = []
    aging: list[str] = []
    for index, ticker in enumerate(live):
        if ticker in excluded:
            continue
        reported = latest_reported_quarter(earnings.get(ticker, ()))
        if not (state.get(ticker) or {}).get("last_success_at"):
            newest_period = reported[0] if reported else date.min
            never.append((-newest_period.toordinal(), index, ticker))
            continue
        stored = newest_stored(ticker)
        offered = (state.get(ticker) or {}).get("newest_offered")
        if reported is not None and (stored is None or reported[0] > stored + _SAME_QUARTER):
            if offered and _as_date(offered) >= reported[0] - _SAME_QUARTER:
                # Alpha Vantage already offered that quarter and it could not
                # be stored (outside the capture window, or a figure missing).
                # Asking every few days would not change that; weekly may.
                if not asked_within(ticker, aging_retry_days):
                    aging.append(ticker)
                continue
            # A newer quarter is known. Ask once its statements can exist, not
            # before: the aging fallback would only fetch the old one again.
            filed = today - reported[1] >= timedelta(days=statement_lag_days)
            if filed and not asked_within(ticker, retry_days):
                behind.append((reported[1], index, ticker))
            continue
        # The fallback for a ticker the earnings cache knows nothing about: with
        # earnings rows and no newer quarter, the next one simply is not out.
        if (
            reported is None
            and (stored is None or (today - stored).days > aging_days)
            and not asked_within(ticker, aging_retry_days)
        ):
            aging.append(ticker)

    never_order = [t for *_, t in sorted(never)]
    behind_order = [t for *_, t in sorted(behind)]
    per_run = budget // 2
    # Excluded tickers are still asked, last, so a fixed override is noticed.
    retry_excluded = [t for t in excluded if not asked_within(t, 1)]
    selected = (never_order + behind_order + aging + retry_excluded)[:per_run]
    return FundamentalsPlan(
        never=never_order,
        behind=behind_order,
        aging=aging,
        selected=selected,
        excluded=excluded,
    )


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


@dataclass
class FundamentalsResult:
    status: str  # CURRENT | FAILED | THIN | INCOMPLETE | SUBSET | TERMINATED
    exit_code: int
    calls: int
    fetched: list[str]
    captured: dict[str, list[str]]
    no_data: list[str]
    refused: list[str]
    errored: list[str]
    stopped: str | None
    fetched_at: datetime | None
    plan: FundamentalsPlan | None
    live_without_fetch: list[str]
    messages: list[str] = field(default_factory=list)

    def summary_line(self) -> str:
        from scripts.fetch_earnings import _iso

        parts = [
            f"status={self.status}",
            f"calls={self.calls}",
            f"fetched={len(self.fetched)}",
            f"quarters_added={sum(len(v) for v in self.captured.values())}",
            f"behind={len(self.plan.behind) if self.plan else 0}",
            f"live_unfetched={len(self.live_without_fetch)}",
            f"errors={len(self.errored)}",
            f"fetched_at={_iso(self.fetched_at) if self.fetched_at else 'unknown'}",
        ]
        if self.stopped:
            parts.append(f"stopped={self.stopped!r}")
        return "FUNDAMENTALS_REFRESH " + " ".join(parts)


def _load_state(path: Path) -> dict:
    try:
        with open(path) as f:
            raw = json.load(f)
        if isinstance(raw.get("tickers"), dict):
            return raw
    except (OSError, ValueError, AttributeError):
        pass
    return {"format_version": STATE_FORMAT_VERSION, "tickers": {}}


def run_fundamentals_refresh(
    *,
    client: Any,
    cache_dir: Path,
    live: list[str],
    budget: int,
    symbol_overrides: Mapping[str, str] | None = None,
    capture_window_days: int = 135,
    statement_lag_days: int = 3,
    retry_days: int = 3,
    aging_days: int = 100,
    aging_retry_days: int = 7,
    refusal_limit: int = 3,
    max_consecutive_errors: int = 3,
    max_missing_fraction: float = 0.20,
    stamp_allowed: bool = True,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    log: Callable[[str], None] = print,
    termination: Any = None,
) -> FundamentalsResult:
    """One budgeted refresh of ``fundamentals.json`` (see the module docstring).

    ``stamp_allowed`` is False for a hand-run over a subset (``--tickers``):
    it saves rows but can never vouch for the whole universe.
    ``max_missing_fraction``: a pass after which more than this share of live
    names has no stored quarter is THIN, not current — the freshness rule
    only judges the tickers that have rows, so it cannot see the gap.
    """
    from scripts.fetch_earnings import (
        EXIT_CURRENT,
        EXIT_FAILED,
        EXIT_INCOMPLETE,
        EXIT_TERMINATED,
        TERMINATION,
        AlphaVantageError,
        Terminated,
        _iso,
        _note_refusal,
        av_symbol,
        is_excluded,
        us_session_date,
    )

    term = termination or TERMINATION
    started = now()
    today = us_session_date(started)
    path = cache_dir / FUNDAMENTALS_FILE
    state_path = cache_dir / FUNDAMENTALS_STATE_FILE

    previous = read_cache_document(path)
    if previous.error:
        raise RuntimeError(
            f"{path} exists but is unreadable ({previous.error}); refusing to overwrite "
            "it. Move it aside to rebuild from Alpha Vantage."
        )
    cache: dict[str, list[dict]] = {t: list(r) for t, r in previous.data.items()}
    state = _load_state(state_path)
    ticker_state: dict[str, dict] = state["tickers"]
    earnings = read_cache_document(cache_dir / EARNINGS_FILE).data

    plan = plan_fundamentals(
        live=live, cache=cache, state=ticker_state, earnings=earnings, now=started,
        budget=budget, statement_lag_days=statement_lag_days, retry_days=retry_days,
        aging_days=aging_days, aging_retry_days=aging_retry_days,
        refusal_limit=refusal_limit,
    )
    log(
        f"plan: {len(plan.never)} live never fetched, {len(plan.behind)} behind their "
        f"earnings, {len(plan.aging)} aging, {len(plan.excluded)} excluded; "
        f"{len(plan.selected)} ticker(s), budget {budget} request(s)"
    )

    class OutOfBudget(Exception):
        pass

    def call_with_retry(fn: Callable[[], Any]) -> Any:
        """One retry for a transport/malformed failure, every request inside
        the budget: the free tier counts retries too."""
        if client.calls - calls_before >= budget:
            raise OutOfBudget()
        try:
            return fn()
        except AlphaVantageError as exc:
            if exc.kind not in ("transport", "malformed"):
                raise
            if client.calls - calls_before >= budget:
                raise
            log(f"  retrying once after {exc.kind}: {exc}")
            term.checkpoint()
            return fn()

    fetched: list[str] = []
    captured: dict[str, list[str]] = {}
    no_data: list[str] = []
    refused: list[str] = []
    errored: list[str] = []
    stopped: str | None = None
    terminated = False
    dirty = False
    consecutive_errors = 0
    calls_before = client.calls
    try:
        for i, ticker in enumerate(plan.selected, 1):
            term.checkpoint()
            # Retries spend quota too, and a ticker needs two requests.
            if client.calls - calls_before + 2 > budget:
                log(f"  budget of {budget} request(s) spent; {len(plan.selected) - i + 1} left")
                break
            symbol = av_symbol(ticker, symbol_overrides)
            entry = ticker_state.setdefault(ticker, {})
            entry["av_symbol"] = symbol
            entry["last_attempt_at"] = _iso(now())
            dirty = True
            tag = f"  [{i}/{len(plan.selected)}] {ticker} ({symbol})"
            try:
                income = call_with_retry(lambda: client.statement("INCOME_STATEMENT", symbol))
                balance = (
                    call_with_retry(lambda: client.statement("BALANCE_SHEET", symbol))
                    if income else None
                )
            except OutOfBudget:
                log(f"{tag}: budget of {budget} request(s) spent mid-ticker; nothing merged")
                break
            except AlphaVantageError as exc:
                entry["status"] = f"error:{exc.kind}"
                if exc.kind == "limit":
                    stopped = f"{ticker} ({symbol}) limit: {exc}"
                    log(f"{tag}: STOPPED (limit): {exc}")
                    break
                if exc.kind == "refused":
                    _note_refusal(entry, today)
                    refused.append(ticker)
                    log(f"{tag}: refused (day {entry['refusal_streak']} of "
                        f"{refusal_limit} before exclusion): {exc}")
                    continue
                errored.append(ticker)
                consecutive_errors += 1
                log(f"{tag}: skipped after a retry ({exc.kind}): {exc}")
                if consecutive_errors >= max_consecutive_errors:
                    stopped = (f"{consecutive_errors} consecutive {exc.kind} failures, "
                               f"last {ticker}: {exc}")
                    log(f"  STOPPED: {stopped}")
                    break
                continue
            consecutive_errors = 0
            entry["last_success_at"] = _iso(now())
            entry["refusal_streak"] = 0
            entry.pop("last_refusal_day", None)
            rows = parse_statements(ticker, income, balance)
            if not rows:
                entry["status"] = "no_data"
                no_data.append(ticker)
                log(f"{tag}: no usable quarters")
            else:
                cache[ticker], added = merge_first_seen(
                    cache.get(ticker), rows, seen_at=started,
                    capture_window_days=capture_window_days,
                )
                entry["status"] = "ok" if cache[ticker] else "none_in_window"
                entry["newest_offered"] = rows[-1]["report_date"]
                if added:
                    captured[ticker] = added
                kept = cache[ticker][-1]["report_date"] if cache[ticker] else "none"
                log(f"{tag}: {len(rows)} quarters offered, newest {rows[-1]['report_date']}; "
                    f"added {', '.join(added) or 'nothing'}; newest stored {kept}")
                if not cache[ticker]:
                    del cache[ticker]
            fetched.append(ticker)
    except Terminated:
        terminated = True
        stopped = "terminated (timeout) mid-run"
        log("terminated mid-run; saving progress")

    excluded = {t for t in live if is_excluded(ticker_state.get(t), refusal_limit)}
    live_without_fetch = [
        t for t in live
        if not (ticker_state.get(t) or {}).get("last_success_at") and t not in excluded
    ]
    with_rows = sum(1 for t in live if cache.get(t))
    thin = bool(live) and (len(live) - with_rows) / len(live) > max_missing_fraction
    current = (
        stamp_allowed
        and not terminated
        and stopped is None
        and not live_without_fetch
        and not thin
    )
    with term.shielded():
        if current:
            fetched_at: datetime | None = started
            write_cache_document(path, cache, fetched_at=started, source=SOURCE)
        else:
            fetched_at = previous.fetched_at
            if dirty and (cache or previous.exists):
                write_cache_document(
                    path, cache, fetched_at=previous.fetched_at,
                    fetch_time_unknown=previous.fetched_at is None, source=SOURCE,
                )
        if dirty or current:
            state["format_version"] = STATE_FORMAT_VERSION
            state["updated_at"] = _iso(now())
            if current:
                state["last_current_at"] = _iso(started)
            write_json_atomic(state_path, state)
    terminated = terminated or term.requested

    gating = set(live) - excluded
    if terminated:
        status, code = "TERMINATED", EXIT_TERMINATED
        stopped = stopped or "terminated while saving"
    elif current:
        status, code = "CURRENT", EXIT_CURRENT
    elif stopped or any(t in gating for t in refused + errored):
        status, code = "FAILED", EXIT_FAILED
    elif live_without_fetch:
        status, code = "INCOMPLETE", EXIT_INCOMPLETE
    elif not stamp_allowed:
        status, code = "SUBSET", EXIT_CURRENT
    else:
        # Every live name asked, and too many still have no quarter.
        status, code = "THIN", EXIT_FAILED

    result = FundamentalsResult(
        status=status, exit_code=code, calls=client.calls - calls_before,
        fetched=fetched, captured=captured, no_data=no_data, refused=refused,
        errored=errored, stopped=stopped, fetched_at=fetched_at, plan=plan,
        live_without_fetch=live_without_fetch,
    )
    live_no_data = sorted(
        t for t in live if (ticker_state.get(t) or {}).get("status") == "no_data"
    )
    if live_no_data:
        result.messages.append(
            "LIVE_NO_DATA: Alpha Vantage has no usable statements for "
            f"{', '.join(live_no_data)} — quality_value cannot rank them; add a symbol "
            "override in data.earnings.refresh.symbol_overrides"
        )
    if excluded:
        result.messages.append(
            "LIVE_REFUSED: Alpha Vantage has refused the statements of "
            f"{', '.join(sorted(excluded))} on {refusal_limit}+ consecutive days; excluded "
            "from the freshness gate, but quality_value cannot rank them. Map each in "
            "data.earnings.refresh.symbol_overrides"
        )
    if thin and not live_without_fetch:
        result.messages.append(
            f"THIN: only {with_rows} of {len(live)} live tickers have a stored quarter "
            f"(more than {max_missing_fraction:.0%} missing); fetched_at is not advanced "
            "and quality_value stays degraded"
        )
    if not stamp_allowed:
        result.messages.append(
            "SUBSET: --tickers run; rows saved, fetched_at left where it was"
        )
    if live_without_fetch:
        per_run = max(budget // 2, 1)
        result.messages.append(
            f"{len(live_without_fetch)} live ticker(s) not yet fetched from Alpha Vantage "
            f"(at {per_run}/run, ~{-(-len(live_without_fetch) // per_run)} more run(s)); "
            "the cache stays unstamped until they are"
        )
    return result


# ---------------------------------------------------------------------------
# Legacy: yfinance
# ---------------------------------------------------------------------------


def fetch_fundamentals_from_yfinance(
    tickers: list[str],
    output_path: str | None = None,
) -> dict[str, list[dict]]:
    """Fetch quarterly fundamentals from yfinance and save to cache.

    Hand-run only (``--source yfinance``). yfinance serves restated figures with
    no filing date, so a cache written by it is neither point-in-time nor the
    production source (KAN-110 chose Alpha Vantage).
    """
    import yfinance as yf

    if output_path is None:
        output_path = str(resolve_cache_dir() / FUNDAMENTALS_FILE)
    fetched_at = datetime.now(timezone.utc)

    cache: dict[str, list[dict]] = {}

    for i, ticker in enumerate(tickers):
        print(f"  [{i+1}/{len(tickers)}] Fetching {ticker}...", end=" ", flush=True)
        try:
            yf_ticker = yf.Ticker(ticker.replace(" ", "-"))
            income = yf_ticker.quarterly_income_stmt
            balance = yf_ticker.quarterly_balance_sheet

            if income.empty or balance.empty:
                print("no data")
                continue

            reports = []
            for col_date in income.columns:
                report_date = col_date.date() if hasattr(col_date, 'date') else col_date
                try:
                    net_income = float(income.loc["Net Income", col_date]) if "Net Income" in income.index else 0.0
                    total_revenue = float(income.loc["Total Revenue", col_date]) if "Total Revenue" in income.index else 0.0

                    total_equity = 0.0
                    total_debt = 0.0
                    if col_date in balance.columns:
                        if "Stockholders Equity" in balance.index:
                            total_equity = float(balance.loc["Stockholders Equity", col_date])
                        elif "Total Equity Gross Minority Interest" in balance.index:
                            total_equity = float(balance.loc["Total Equity Gross Minority Interest", col_date])
                        if "Total Debt" in balance.index:
                            total_debt = float(balance.loc["Total Debt", col_date])

                    roe = net_income / total_equity if total_equity > 0 else 0.0
                    debt_equity = total_debt / total_equity if total_equity > 0 else 0.0
                    profit_margin = net_income / total_revenue if total_revenue > 0 else 0.0

                    reports.append({
                        "report_date": str(report_date),
                        "roe": round(roe, 4),
                        "debt_equity": round(debt_equity, 4),
                        "profit_margin": round(profit_margin, 4),
                        "net_income": net_income,
                        "total_revenue": total_revenue,
                        "total_equity": total_equity,
                        "total_debt": total_debt,
                        "sector": SECTOR_MAP.get(ticker, "Unknown"),
                    })
                except (KeyError, TypeError, ValueError):
                    continue

            if reports:
                cache[ticker] = sorted(reports, key=lambda r: r["report_date"])
                print(f"{len(reports)} reports")
            else:
                print("no usable data")

        except Exception as e:
            print(f"FAILED: {e}")

        time.sleep(0.3)

    save_fundamentals_cache(cache, output_path, fetched_at=fetched_at, source="yfinance")
    print(f"\nSaved {len(cache)} tickers to {output_path}")
    return cache


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def live_universe() -> list[str]:
    """The tickers quality_value trades (shared.universe is the single source)."""
    from shared.universe import UNIVERSE_REGISTRY

    return list(UNIVERSE_REGISTRY["quality_value"])


def main(
    argv: list[str] | None = None,
    *,
    transport: Any = None,
    env: Mapping[str, str] | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        description="Refresh fundamentals.json from Alpha Vantage (KAN-110)"
    )
    parser.add_argument("--source", choices=("alphavantage", "yfinance"), default="alphavantage",
                        help="alphavantage: the incremental, as-first-seen refresh (default). "
                             "yfinance: the legacy full re-fetch, restated figures, hand-run only")
    parser.add_argument("--tickers", default=None,
                        help="Comma-separated tickers (default: the quality_value universe)")
    parser.add_argument("--cache-dir", default=None,
                        help="Directory for fundamentals.json (default: data.cache_dir)")
    parser.add_argument("--output", default=None,
                        help="--source yfinance only: output JSON path")
    parser.add_argument("--budget", type=int, default=None,
                        help="Requests this run (default: data.fundamentals.refresh.calls_per_run)")
    parser.add_argument("--min-interval", type=float, default=None,
                        help="Seconds between requests (default: min_interval_seconds)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the plan; make no requests")
    args = parser.parse_args(argv)

    if args.tickers:
        live = [t.strip() for t in args.tickers.split(",") if t.strip()]
    else:
        live = live_universe()

    if args.source == "yfinance":
        print(f"Fetching fundamentals for {len(live)} tickers from yfinance...")
        fetch_fundamentals_from_yfinance(live, args.output)
        return 0

    from scripts.fetch_earnings import (
        API_KEY_VAR,
        EXIT_CURRENT,
        EXIT_ERROR,
        EXIT_LOCKED,
        EXIT_TERMINATED,
        TERMINATION,
        AlphaVantageClient,
        LockHeld,
        Terminated,
        _CacheLock,
        load_api_key,
    )
    from shared.data_cache import configured_data_config

    data_cfg = configured_data_config()
    cfg = data_cfg.fundamentals.refresh
    overrides = data_cfg.earnings.refresh.symbol_overrides
    cache_dir = resolve_cache_dir(args.cache_dir or data_cfg.cache_dir)
    budget = cfg.calls_per_run if args.budget is None else args.budget
    interval = cfg.min_interval_seconds if args.min_interval is None else args.min_interval
    print(f"Fundamentals refresh: {len(live)} live tickers, budget {budget} request(s) "
          f"(2 per ticker), {interval:g}s apart, cache {cache_dir}")

    if args.dry_run:
        state = _load_state(cache_dir / FUNDAMENTALS_STATE_FILE)["tickers"]
        plan = plan_fundamentals(
            live=live, cache=read_cache_document(cache_dir / FUNDAMENTALS_FILE).data,
            state=state, earnings=read_cache_document(cache_dir / EARNINGS_FILE).data,
            now=datetime.now(timezone.utc), budget=budget,
            statement_lag_days=cfg.statement_lag_days, retry_days=cfg.retry_days,
            aging_days=cfg.aging_days, aging_retry_days=cfg.aging_retry_days,
            refusal_limit=cfg.refusal_exclude_after_days,
        )
        print(f"never fetched: {len(plan.never)}; behind: {plan.behind}; aging: "
              f"{plan.aging}; excluded: {plan.excluded}")
        print(f"would fetch: {plan.selected}")
        return EXIT_CURRENT

    key = load_api_key(env)
    if not key:
        print(f"ERROR: no Alpha Vantage key: {API_KEY_VAR} is not set and the keychain "
              "(service algo-poc) has no item for it. Import it: "
              "deploy/launchd/secrets.sh --import", file=sys.stderr)
        print("FUNDAMENTALS_REFRESH status=ERROR reason='no API key'")
        return EXIT_ERROR

    client = AlphaVantageClient(
        key, transport=transport, min_interval_seconds=interval,
        timeout_seconds=cfg.request_timeout_seconds,
    )
    TERMINATION.reset()
    previous_handler = signal.signal(signal.SIGTERM, TERMINATION.handle)
    try:
        with _CacheLock(cache_dir):
            result = run_fundamentals_refresh(
                client=client,
                cache_dir=cache_dir,
                live=live,
                budget=budget,
                symbol_overrides=overrides,
                capture_window_days=cfg.capture_window_days,
                statement_lag_days=cfg.statement_lag_days,
                retry_days=cfg.retry_days,
                aging_days=cfg.aging_days,
                aging_retry_days=cfg.aging_retry_days,
                refusal_limit=cfg.refusal_exclude_after_days,
                max_consecutive_errors=cfg.max_consecutive_errors,
                max_missing_fraction=data_cfg.fundamentals.max_stale_ticker_fraction,
                stamp_allowed=args.tickers is None,
            )
    except LockHeld as exc:
        print(f"SKIPPED: {exc}")
        print("FUNDAMENTALS_REFRESH status=SKIPPED reason='lock held'")
        return EXIT_LOCKED
    except Terminated:
        print("FUNDAMENTALS_REFRESH status=TERMINATED reason='outside the fetch loop'")
        return EXIT_TERMINATED
    except Exception as exc:  # noqa: BLE001 — scrubbed, then reported
        print(client.scrub(traceback.format_exc()), file=sys.stderr)
        print(f"ERROR: {client.scrub(f'{type(exc).__name__}: {exc}')}", file=sys.stderr)
        print(f"FUNDAMENTALS_REFRESH status=ERROR reason={client.scrub(type(exc).__name__)!r}")
        return EXIT_ERROR
    finally:
        signal.signal(signal.SIGTERM, previous_handler)

    for message in result.messages:
        print(message)
    print(result.summary_line())
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
