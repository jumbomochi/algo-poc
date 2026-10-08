#!/usr/bin/env python3
"""Refresh the earnings cache from Alpha Vantage, incrementally and within budget.

KAN-110. ``earnings_drift`` trades on ``earnings.json`` in ``data.cache_dir``,
and KAN-109 marks the sleeve DATA-DEGRADED when that file was fetched more than
``data.earnings.max_fetch_age_days`` (2) days ago. This script is what keeps it
inside that bound, run daily by ``deploy/launchd/run_earnings_refresh.sh``.

Usage::

    python scripts/fetch_earnings.py                    # live sleeve universe
    python scripts/fetch_earnings.py --universe pit     # + point-in-time backfill
    python scripts/fetch_earnings.py --tickers AAPL,MSFT --cache-dir /tmp/x
    python scripts/fetch_earnings.py --dry-run          # print the plan, no calls

The API key is read from ``$ALPHAVANTAGE_API_KEY``, else from the macOS login
keychain (service ``algo-poc``, account ``ALPHAVANTAGE_API_KEY``). It travels in
the request URL, so no URL and no unscrubbed exception text is ever printed.

Why Alpha Vantage
-----------------
Its EARNINGS endpoint reports ``reportedEPS`` and ``estimatedEPS`` on the SAME
(adjusted) basis, back to the 1990s. yfinance mixed a GAAP actual with an
adjusted consensus for some names, which manufactured surprises (META 2025-10).
The surprise is recomputed here from those two numbers rather than taken from
``surprisePercentage``, so the rule is ours and is the same for every row.

One run
-------
1. One ``EARNINGS_CALENDAR`` call, merged into ``earnings_calendar.json``. Past
   entries are kept for ``calendar_retain_days``, because the calendar only
   looks forward and a report from yesterday has already dropped off it.
2. ``EARNINGS`` calls, at most ``calls_per_run``, in this order:

   a. **recent reporters** in the live universe: a scheduled report date in the
      last ``recent_days`` days (US/Eastern) whose actual is not in the cache
      yet, oldest first;
   b. live tickers **never fetched** from Alpha Vantage, then live tickers
      whose newest actual is over 100 days old (re-asked weekly — the case of a
      report the calendar never carried);
   c. ``--universe pit`` only: other point-in-time tickers that are recent
      reporters, then those never fetched (current index members first);
   d. the **stalest** remaining tickers, so estimates and late restatements are
      picked up over time.

   Each response is merged into ``earnings.json``: rows for a fiscal period are
   replaced, older rows are never dropped, and a ticker's pre-KAN-110 rows
   (yfinance, no ``fiscal_period``) are replaced wholesale on its first fetch so
   the two EPS bases are never mixed in one ticker.
3. Both files are written atomically (temp file + ``os.replace``). On a rate
   limit or error the run stops, keeps what it fetched, and writes that.

What ``fetched_at`` means
-------------------------
``earnings.json``'s ``fetched_at`` is the START of the most recent run in which

* the calendar call succeeded, **and**
* every live ticker had been fetched from Alpha Vantage at least once, **and**
* every live recent reporter (step 2a) was fetched successfully — in this run,
  or within ``refetch_after_hours`` on or after its report date.

That is exactly the claim KAN-109's 2-day freshness check needs: as of that
instant, every live name that has reported inside the lookup window had been
asked for its actual. A run that does not meet it (budget exhausted, rate
limit, error) still saves the rows it fetched but leaves ``fetched_at`` where
it was — or ``null`` when no run has ever met it — so the cache ages honestly
and the sleeve degrades loudly instead of trading on a partial picture.

Exit codes: 0 current (stamped); 1 could not run (no key, lock held, crash,
unreadable cache); 2 Alpha Vantage refused or failed before the live universe
was current; 3 the budget ran out before it was current (initial backfill, or
an earnings-season day with more reporters than budget).
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
import re
import signal
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from zoneinfo import ZoneInfo

# Run by path (``python scripts/fetch_*.py``), which puts scripts/ rather than
# the repo root on sys.path; pin the root so ``shared`` is THIS checkout's.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


from shared.data_cache import (  # noqa: E402
    EARNINGS_CALENDAR_FILE,
    EARNINGS_FILE,
    EARNINGS_STATE_FILE,
    read_cache_document,
    resolve_cache_dir,
    write_cache_document,
    write_json_atomic,
)

SOURCE = "alphavantage"
AV_URL = "https://www.alphavantage.co/query"
API_KEY_VAR = "ALPHAVANTAGE_API_KEY"
US_EASTERN = ZoneInfo("America/New_York")
STATE_FORMAT_VERSION = 1

EXIT_CURRENT = 0
EXIT_ERROR = 1
EXIT_FAILED = 2
EXIT_INCOMPLETE = 3

_TIMING = {"pre-market": "bmo", "post-market": "amc"}

#: An Alpha Vantage reportedDate and the calendar's reportDate for the same
#: announcement can differ by a day; a row this close to a scheduled date
#: counts as that report's actual.
_CAPTURE_TOLERANCE = timedelta(days=3)

#: A live ticker whose newest Alpha Vantage actual is older than this has
#: probably reported without the calendar saying so; it is re-asked at most
#: every ``_OVERDUE_RETRY``.
_OVERDUE_AFTER = timedelta(days=100)
_OVERDUE_RETRY = timedelta(days=7)


# ---------------------------------------------------------------------------
# The cache API the paper run and the backtest use
# ---------------------------------------------------------------------------


def save_earnings_cache(
    data: dict[str, list[dict]],
    path: str,
    *,
    fetched_at: datetime | None = None,
    source: str | None = None,
) -> None:
    """Save earnings rows, stamped with when they were fetched (KAN-109).

    ``fetched_at`` defaults to now; pass the instant the fetch STARTED.
    """
    write_cache_document(path, data, fetched_at=fetched_at, source=source)


def load_earnings_cache(path: str) -> dict[str, list[dict]]:
    """Load earnings rows. Returns empty dict if file missing.

    Reads both the KAN-109 envelope and the legacy bare ``{ticker: rows}``
    mapping, and returns only the rows, so the lookup builders never see the
    metadata. Use :func:`shared.data_cache.read_cache_document` for the fetch
    time and whether the file was there at all — this function cannot tell a
    missing cache from an empty one, which is how KAN-109 went unnoticed.
    """
    return read_cache_document(path).data


def build_earnings_lookup(
    cache: dict[str, list[dict]],
    window_days: int = 2,
) -> Callable[[str, date], dict | None]:
    """Build an earnings event lookup function.

    Returns a function(ticker, as_of_date) -> dict | None that returns
    the earnings event if one occurred within window_days of as_of_date.
    Only returns events on or after the earnings date (not before).
    """
    events_by_ticker: dict[str, dict[date, dict]] = {}
    for ticker, events in cache.items():
        date_map: dict[date, dict] = {}
        for e in events:
            ed = date.fromisoformat(e["earnings_date"]) if isinstance(e["earnings_date"], str) else e["earnings_date"]
            for offset in range(window_days + 1):
                d = ed + timedelta(days=offset)
                if d not in date_map:
                    date_map[d] = e
        events_by_ticker[ticker] = date_map

    def lookup(ticker: str, as_of_date: date) -> dict | None:
        date_map = events_by_ticker.get(ticker)
        if not date_map:
            return None
        return date_map.get(as_of_date)

    return lookup


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------


def scrub(text: Any, secret: str | None = None) -> str:
    """``text`` with the API key removed, however it got there.

    The key is a query parameter, so any exception that carries the URL — an
    ``HTTPError``, a proxy error, a TLS failure — carries the key. Both the
    literal value and any ``apikey=`` parameter are masked.
    """
    out = str(text)
    if secret:
        out = out.replace(secret, "***")
    return re.sub(r"(?i)(apikey=)[^&\s'\"]+", r"\1***", out)


def load_api_key(env: Mapping[str, str] | None = None) -> str | None:
    """The key from the environment, else the login keychain; never printed.

    The launchd wrapper exports it through ``secrets.sh``. The keychain
    fallback serves a hand-run, read the same way ``secrets.sh`` does, with
    the value on stdout of a child process — never in argv.
    """
    env = os.environ if env is None else env
    value = (env.get(API_KEY_VAR) or "").strip()
    if value:
        return value
    security = env.get("ALGO_SECURITY_BIN", "/usr/bin/security")
    service = env.get("ALGO_KEYCHAIN_SERVICE", "algo-poc")
    try:
        result = subprocess.run(
            [security, "find-generic-password", "-s", service, "-a", API_KEY_VAR, "-w"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip() if result.returncode == 0 else ""
    return value or None


# ---------------------------------------------------------------------------
# Alpha Vantage
# ---------------------------------------------------------------------------


class AlphaVantageError(Exception):
    """A request Alpha Vantage did not answer with data.

    ``kind``:

    * ``limit`` — a ``Note``/``Information`` body: the daily or per-minute
      quota, or a plan restriction. Every later call would get the same, so the
      run stops.
    * ``refused`` — an ``Error Message`` body: this request was invalid. On the
      calendar call that means the key or the call is wrong (stop); on one
      symbol it is that symbol (record it, carry on).
    * ``transport`` — no usable HTTP response (network, TLS, timeout, 5xx).
    * ``malformed`` — a response that is not the shape documented.

    The message is always scrubbed of the key.
    """

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind

    @property
    def stops_run(self) -> bool:
        return self.kind != "refused"


Transport = Callable[[str, float], bytes]


def _urllib_transport(url: str, timeout: float) -> bytes:
    """GET ``url`` with certifi's CA bundle.

    The production venv's Python has no default CA file, which is why a bare
    ``urlopen`` failed every https dead-man ping in KAN-100
    (scripts/ops/evidence_digest.py ``_ping_deadman``). certifi is installed
    via httpx.
    """
    import ssl
    import urllib.request

    import certifi

    context = ssl.create_default_context(cafile=certifi.where())
    request = urllib.request.Request(url, headers={"User-Agent": "algo-poc/earnings-refresh"})
    with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
        return response.read()


class AlphaVantageClient:
    """A rate-spaced Alpha Vantage client that can never leak its key."""

    def __init__(
        self,
        api_key: str,
        *,
        transport: Transport | None = None,
        min_interval_seconds: float = 13.0,
        timeout_seconds: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        if not api_key:
            raise ValueError("an Alpha Vantage API key is required")
        self._key = api_key
        self._transport = transport or _urllib_transport
        self._interval = min_interval_seconds
        self._timeout = timeout_seconds
        self._sleep = sleep
        self._monotonic = monotonic
        self._last: float | None = None
        #: Requests sent, successful or not — what the quota counts.
        self.calls = 0

    def scrub(self, text: Any) -> str:
        return scrub(text, self._key)

    def _get(self, params: Mapping[str, str]) -> str:
        from urllib.parse import urlencode

        if self._last is not None:
            wait = self._interval - (self._monotonic() - self._last)
            if wait > 0:
                self._sleep(wait)
        url = f"{AV_URL}?{urlencode({**params, 'apikey': self._key})}"
        self.calls += 1
        try:
            body = self._transport(url, self._timeout)
        except Exception as exc:  # noqa: BLE001 — every failure is reported, scrubbed
            # ``from None``: the original exception's repr can carry the URL,
            # and a chained traceback would print it.
            raise AlphaVantageError(
                "transport", self.scrub(f"{type(exc).__name__}: {exc}")
            ) from None
        finally:
            self._last = self._monotonic()
        return body.decode("utf-8", errors="replace")

    def _notice(self, payload: Any) -> None:
        """Raise for the HTTP-200 JSON bodies that are errors, not data."""
        if not isinstance(payload, dict):
            return
        for key in ("Note", "Information"):
            if key in payload:
                raise AlphaVantageError("limit", self.scrub(f"{key}: {payload[key]}"))
        if "Error Message" in payload:
            raise AlphaVantageError(
                "refused", self.scrub(f"Error Message: {payload['Error Message']}")
            )

    def earnings(self, symbol: str) -> dict | None:
        """The EARNINGS payload, or ``None`` when Alpha Vantage knows no such symbol."""
        text = self._get({"function": "EARNINGS", "symbol": symbol})
        try:
            payload = json.loads(text)
        except ValueError:
            raise AlphaVantageError(
                "malformed", self.scrub(f"EARNINGS {symbol}: not JSON: {text[:120]!r}")
            ) from None
        self._notice(payload)
        if not isinstance(payload, dict):
            raise AlphaVantageError("malformed", f"EARNINGS {symbol}: not an object")
        # An unknown symbol is answered with ``{}`` (MMC, 2026-10-08).
        if not payload:
            return None
        return payload

    def calendar(self, horizon: str = "3month") -> str:
        """The EARNINGS_CALENDAR CSV, verbatim."""
        text = self._get({"function": "EARNINGS_CALENDAR", "horizon": horizon})
        if text.lstrip().startswith("{"):
            try:
                payload = json.loads(text)
            except ValueError:
                payload = None
            self._notice(payload)
            raise AlphaVantageError(
                "malformed", self.scrub(f"EARNINGS_CALENDAR: JSON instead of CSV: {text[:120]!r}")
            )
        return text


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _number(value: Any) -> float | None:
    """A finite float, or ``None`` for Alpha Vantage's "None", "", "-" and junk."""
    if value is None:
        return None
    try:
        out = float(str(value).strip())
    except ValueError:
        return None
    return out if math.isfinite(out) else None


def _iso_date(value: Any) -> str | None:
    try:
        return date.fromisoformat(str(value).strip()[:10]).isoformat()
    except ValueError:
        return None


def surprise_pct(actual: float | None, estimate: float | None) -> float | None:
    """``(actual - estimate) / |estimate| * 100``; ``None`` without a usable estimate.

    A zero estimate has no percentage surprise. ``None`` rather than the old
    ``0.0``: zero is a real (in-line) result and would be read as one.
    """
    if actual is None or estimate is None or estimate == 0:
        return None
    return round((actual - estimate) / abs(estimate) * 100.0, 2)


def parse_quarterly_earnings(payload: Mapping[str, Any]) -> list[dict]:
    """Reported quarters from an EARNINGS payload, in the cache's row shape.

    Only quarters with a reported EPS are rows: a scheduled quarter has no
    actual, and a row without one is not an event (KAN-109 found 36 such rows
    trading as entries). Both EPS figures come from the same Alpha Vantage
    row, so they are on the same (adjusted) basis.
    """
    rows: dict[str, dict] = {}
    for quarter in payload.get("quarterlyEarnings") or []:
        if not isinstance(quarter, Mapping):
            continue
        actual = _number(quarter.get("reportedEPS"))
        reported = _iso_date(quarter.get("reportedDate"))
        if actual is None or reported is None:
            continue
        estimate = _number(quarter.get("estimatedEPS"))
        fiscal = _iso_date(quarter.get("fiscalDateEnding"))
        row = {
            "earnings_date": reported,
            "actual_eps": round(actual, 4),
            "estimate_eps": None if estimate is None else round(estimate, 4),
            "surprise_pct": surprise_pct(actual, estimate),
            "timing": _TIMING.get(str(quarter.get("reportTime") or "").strip()),
            "fiscal_period": fiscal,
        }
        rows[_row_key(row)] = row
    return sorted(rows.values(), key=lambda r: r["earnings_date"])


@dataclass(frozen=True)
class CalendarEntry:
    symbol: str
    report_date: str
    fiscal_period: str | None
    estimate_eps: float | None
    currency: str | None
    timing: str | None

    def as_row(self) -> dict:
        return {
            "symbol": self.symbol,
            "report_date": self.report_date,
            "fiscal_period": self.fiscal_period,
            "estimate_eps": self.estimate_eps,
            "currency": self.currency,
            "timing": self.timing,
        }


def parse_calendar_csv(text: str) -> list[CalendarEntry]:
    reader = csv.DictReader(io.StringIO(text))
    header = set(reader.fieldnames or [])
    if not {"symbol", "reportDate"} <= header:
        raise AlphaVantageError(
            "malformed", f"EARNINGS_CALENDAR: unexpected header {sorted(header)}"
        )
    out = []
    for row in reader:
        symbol = (row.get("symbol") or "").strip()
        reported = _iso_date(row.get("reportDate"))
        if not symbol or reported is None:
            continue
        out.append(
            CalendarEntry(
                symbol=symbol,
                report_date=reported,
                fiscal_period=_iso_date(row.get("fiscalDateEnding")),
                estimate_eps=_number(row.get("estimate")),
                currency=(row.get("currency") or "").strip() or None,
                timing=_TIMING.get((row.get("timeOfTheDay") or "").strip()),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Ticker mapping
# ---------------------------------------------------------------------------


def av_symbol(ticker: str, overrides: Mapping[str, str] | None = None) -> str:
    """The EARNINGS-endpoint symbol for a universe ticker (``BRK B`` -> ``BRK-B``)."""
    if overrides and ticker in overrides:
        return overrides[ticker]
    return ticker.strip().replace(" ", "-").replace(".", "-")


def calendar_ticker(
    symbol: str, reverse: Mapping[str, str] | None = None
) -> str:
    """A calendar symbol as a universe ticker.

    The calendar writes share classes with a dot (``BRK.B``) where EARNINGS
    takes a dash, so both are folded to the dash form before ``reverse`` (AV
    symbol -> universe ticker) is consulted; anything not in the universe is
    written universe-style, with a space.
    """
    dashed = symbol.strip().replace(".", "-")
    if reverse and dashed in reverse:
        return reverse[dashed]
    return dashed.replace("-", " ")


def reverse_symbols(
    tickers: Iterable[str], overrides: Mapping[str, str] | None = None
) -> dict[str, str]:
    out: dict[str, str] = {}
    for ticker in tickers:
        out.setdefault(av_symbol(ticker, overrides), ticker)
    return out


# ---------------------------------------------------------------------------
# Merging
# ---------------------------------------------------------------------------


def _row_key(row: Mapping[str, Any]) -> str:
    return f"fp:{row['fiscal_period']}" if row.get("fiscal_period") else f"d:{row['earnings_date']}"


def _is_av_row(row: Mapping[str, Any]) -> bool:
    """Rows written by this fetcher carry ``fiscal_period``; yfinance rows never did."""
    return "fiscal_period" in row


def merge_ticker_rows(old: list[dict] | None, new: list[dict]) -> list[dict]:
    """``new`` over ``old``: same fiscal period replaced, nothing older dropped.

    Pre-KAN-110 rows (no ``fiscal_period``) are dropped once Alpha Vantage has
    returned rows for the ticker: they were fetched on a different EPS basis,
    and AV's history is longer, so keeping them would only mix bases.
    """
    kept = [r for r in (old or []) if _is_av_row(r)] if new else list(old or [])
    merged = {_row_key(r): r for r in kept}
    for row in new:
        merged[_row_key(row)] = row
    return sorted(merged.values(), key=lambda r: (r["earnings_date"], r.get("fiscal_period") or ""))


def merge_calendar(
    old: Mapping[str, list[dict]],
    new: Mapping[str, list[dict]],
    *,
    keep_from: date,
) -> dict[str, list[dict]]:
    """The new calendar, plus old entries whose report date is ``>= keep_from``.

    The calendar only looks forward. Without the retained past, a ticker that
    reported yesterday would vanish from it and could no longer be recognised
    as a recent reporter.
    """
    out: dict[str, dict[str, dict]] = {}
    for ticker, rows in old.items():
        for row in rows:
            if _iso_date(row.get("report_date")) and row["report_date"] >= keep_from.isoformat():
                out.setdefault(ticker, {})[_calendar_key(row)] = row
    for ticker, rows in new.items():
        for row in rows:
            out.setdefault(ticker, {})[_calendar_key(row)] = row
    return {
        ticker: sorted(rows.values(), key=lambda r: r["report_date"])
        for ticker, rows in sorted(out.items())
    }


def _calendar_key(row: Mapping[str, Any]) -> str:
    return f"fp:{row['fiscal_period']}" if row.get("fiscal_period") else f"d:{row['report_date']}"


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def _parse_instant(value: Any) -> datetime | None:
    if not value:
        return None
    moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


@dataclass
class RefreshPlan:
    #: Live recent reporters, oldest report first — the calls fetched_at needs.
    due_live: list[str]
    #: Live tickers with no successful Alpha Vantage fetch yet.
    never_live: list[str]
    #: Live tickers whose newest actual is over a quarter old (not gating).
    overdue_live: list[str]
    due_other: list[str]
    never_other: list[str]
    stalest: list[str]
    selected: list[str]
    #: Due dates per ticker, for the report.
    due_dates: dict[str, list[str]] = field(default_factory=dict)


def us_session_date(moment: datetime) -> date:
    return moment.astimezone(US_EASTERN).date()


def plan_refresh(
    *,
    live: list[str],
    universe: list[str],
    cache: Mapping[str, list[dict]],
    state: Mapping[str, dict],
    calendar: Mapping[str, list[dict]],
    now: datetime,
    budget: int,
    recent_days: int,
    refetch_after_hours: float,
) -> RefreshPlan:
    """Which tickers to fetch this run, in priority order, capped at ``budget``."""
    today = us_session_date(now)
    window_start = today - timedelta(days=recent_days)
    refetch = timedelta(hours=refetch_after_hours)
    live_set = set(live)

    def last_success(ticker: str) -> datetime | None:
        return _parse_instant((state.get(ticker) or {}).get("last_success_at"))

    def due_dates(ticker: str) -> list[tuple[str, str | None]]:
        """Scheduled report dates in the window that are not yet satisfied."""
        asked = last_success(ticker)
        av_dates = [
            date.fromisoformat(r["earnings_date"])
            for r in cache.get(ticker, [])
            if _is_av_row(r) and r.get("earnings_date")
        ]
        out = []
        for entry in calendar.get(ticker, []):
            when = _iso_date(entry.get("report_date"))
            if when is None:
                continue
            scheduled = date.fromisoformat(when)
            if not window_start <= scheduled <= today:
                continue
            captured = any(d >= scheduled - _CAPTURE_TOLERANCE for d in av_dates)
            # Asked on or after the report day, and recently: Alpha Vantage
            # did not have the actual yet. It is asked again tomorrow.
            asked_since = (
                asked is not None
                and us_session_date(asked) >= scheduled
                and now - asked < refetch
            )
            if not captured and not asked_since:
                out.append((when, entry.get("timing")))
        return out

    def due_order(ticker: str, dates: list[tuple[str, str | None]]) -> tuple:
        first = min(d for d, _ in dates)
        # A post-market report from today may not be published yet: last.
        same_day_amc = first == today.isoformat() and any(
            d == first and timing == "amc" for d, timing in dates
        )
        return (same_day_amc, first, ticker)

    due: dict[str, list[tuple[str, str | None]]] = {}
    for ticker in universe:
        dates = due_dates(ticker)
        if dates:
            due[ticker] = dates

    due_live = sorted((t for t in live if t in due), key=lambda t: due_order(t, due[t]))
    never_live = [t for t in live if last_success(t) is None and t not in due]
    # A live name the calendar never flagged (a symbol it lists differently, a
    # date it never carried) would otherwise wait for the stalest-first
    # rotation, which the point-in-time backfill can starve for weeks. One
    # whose newest actual is over a quarter old is asked again, weekly.
    overdue_before = today - _OVERDUE_AFTER

    def overdue(ticker: str) -> bool:
        asked = last_success(ticker)
        if asked is None or now - asked < _OVERDUE_RETRY or ticker in due:
            return False
        dates = [r["earnings_date"] for r in cache.get(ticker, []) if _is_av_row(r)]
        return not dates or max(dates) < overdue_before.isoformat()

    overdue_live = [t for t in live if overdue(t)]
    others = [t for t in universe if t not in live_set]
    due_other = sorted((t for t in others if t in due), key=lambda t: due_order(t, due[t]))
    never_other = [t for t in others if last_success(t) is None and t not in due]
    stale_cutoff = now - refetch
    stalest = sorted(
        (t for t in universe if (last_success(t) or now) < stale_cutoff),
        key=lambda t: (last_success(t), t),
    )

    selected: list[str] = []
    seen: set[str] = set()
    for ticker in due_live + never_live + overdue_live + due_other + never_other + stalest:
        if len(selected) >= budget:
            break
        if ticker not in seen:
            seen.add(ticker)
            selected.append(ticker)
    return RefreshPlan(
        due_live=due_live,
        never_live=never_live,
        overdue_live=overdue_live,
        due_other=due_other,
        never_other=never_other,
        stalest=stalest,
        selected=selected,
        due_dates={t: [d for d, _ in v] for t, v in due.items()},
    )


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


class Terminated(BaseException):
    """SIGTERM (the wrapper's timeout) arrived mid-run; save progress and stop.

    A ``BaseException`` so the client's catch-all for transport failures does
    not turn it into an ordinary error and carry on.
    """


@dataclass
class RefreshResult:
    status: str  # CURRENT | FAILED | INCOMPLETE
    exit_code: int
    calls: int
    fetched: list[str]
    no_data: list[str]
    refused: list[str]
    stopped: str | None
    stamped: bool
    fetched_at: datetime | None
    plan: RefreshPlan | None
    live_without_fetch: list[str]
    critical_missed: list[str]
    backfill_remaining: int
    messages: list[str] = field(default_factory=list)

    def summary_line(self) -> str:
        parts = [
            f"status={self.status}",
            f"calls={self.calls}",
            f"fetched={len(self.fetched)}",
        ]
        if self.plan is not None:
            parts.append(f"recent_reporters={len(self.plan.due_live)}")
        parts += [
            f"live_unfetched={len(self.live_without_fetch)}",
            f"recent_missed={len(self.critical_missed)}",
            f"backfill_remaining={self.backfill_remaining}",
            f"fetched_at={_iso(self.fetched_at) if self.fetched_at else 'unknown'}",
        ]
        if self.stopped:
            parts.append(f"stopped={self.stopped!r}")
        return "EARNINGS_REFRESH " + " ".join(parts)


def _load_state(path: Path) -> dict:
    try:
        with open(path) as f:
            raw = json.load(f)
        tickers = raw.get("tickers")
        if isinstance(tickers, dict):
            return raw
    except (OSError, ValueError, AttributeError):
        pass
    return {"format_version": STATE_FORMAT_VERSION, "tickers": {}}


def run_refresh(
    *,
    client: AlphaVantageClient,
    cache_dir: Path,
    live: list[str],
    universe: list[str],
    budget: int,
    recent_days: int = 4,
    refetch_after_hours: float = 20.0,
    calendar_horizon: str = "3month",
    calendar_retain_days: int = 14,
    symbol_overrides: Mapping[str, str] | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    log: Callable[[str], None] = print,
) -> RefreshResult:
    """One budgeted refresh of ``earnings.json`` (see the module docstring)."""
    started = now()
    overrides = dict(symbol_overrides or {})
    earnings_path = cache_dir / EARNINGS_FILE
    calendar_path = cache_dir / EARNINGS_CALENDAR_FILE
    state_path = cache_dir / EARNINGS_STATE_FILE

    previous = read_cache_document(earnings_path)
    if previous.error:
        # A file that exists and cannot be read might be a good cache behind a
        # transient fault. Never overwrite what we cannot see.
        raise RuntimeError(
            f"{earnings_path} exists but is unreadable ({previous.error}); refusing to "
            "overwrite it. Move it aside to rebuild from Alpha Vantage."
        )
    cache: dict[str, list[dict]] = {t: list(r) for t, r in previous.data.items()}
    state = _load_state(state_path)
    ticker_state: dict[str, dict] = state["tickers"]
    reverse = reverse_symbols(universe, overrides)

    fetched: list[str] = []
    no_data: list[str] = []
    refused: list[str] = []
    stopped: str | None = None
    calendar_ok = False
    plan: RefreshPlan | None = None
    dirty = False

    # 1. The calendar.
    old_calendar = read_cache_document(calendar_path)
    calendar_rows: dict[str, list[dict]] = dict(old_calendar.data)
    try:
        entries = parse_calendar_csv(client.calendar(calendar_horizon))
        fresh: dict[str, list[dict]] = {}
        for entry in entries:
            fresh.setdefault(calendar_ticker(entry.symbol, reverse), []).append(entry.as_row())
        keep_from = us_session_date(started) - timedelta(days=calendar_retain_days)
        calendar_rows = merge_calendar(old_calendar.data, fresh, keep_from=keep_from)
        write_cache_document(calendar_path, calendar_rows, fetched_at=now(), source=SOURCE)
        calendar_ok = True
        log(f"calendar: {len(entries)} scheduled reports ({calendar_horizon}), "
            f"{sum(len(v) for v in calendar_rows.values())} kept with the last "
            f"{calendar_retain_days} days")
    except AlphaVantageError as exc:
        stopped = f"calendar {exc.kind}: {exc}"
        log(f"calendar FAILED ({exc.kind}): {exc}")
    except Terminated:
        stopped = "terminated before the calendar call completed"

    # 2. EARNINGS calls.
    if calendar_ok:
        plan = plan_refresh(
            live=live,
            universe=universe,
            cache=cache,
            state=ticker_state,
            calendar=calendar_rows,
            now=started,
            budget=budget,
            recent_days=recent_days,
            refetch_after_hours=refetch_after_hours,
        )
        log(
            f"plan: {len(plan.due_live)} live recent reporter(s), "
            f"{len(plan.never_live)} live never fetched, {len(plan.due_other)} other "
            f"recent, {len(plan.never_other)} other never fetched; "
            f"{len(plan.selected)} call(s) of budget {budget}"
        )
        try:
            for i, ticker in enumerate(plan.selected, 1):
                symbol = av_symbol(ticker, overrides)
                entry = ticker_state.setdefault(ticker, {})
                entry["av_symbol"] = symbol
                entry["last_attempt_at"] = _iso(now())
                dirty = True
                try:
                    payload = client.earnings(symbol)
                except AlphaVantageError as exc:
                    entry["status"] = f"error:{exc.kind}"
                    if exc.stops_run:
                        stopped = f"{ticker} ({symbol}) {exc.kind}: {exc}"
                        log(f"  [{i}/{len(plan.selected)}] {ticker}: STOPPED ({exc.kind}): {exc}")
                        break
                    refused.append(ticker)
                    log(f"  [{i}/{len(plan.selected)}] {ticker}: refused: {exc}")
                    continue
                entry["last_success_at"] = _iso(now())
                rows = parse_quarterly_earnings(payload) if payload else []
                if not rows:
                    entry["status"] = "no_data"
                    entry["reported_rows"] = 0
                    no_data.append(ticker)
                    log(f"  [{i}/{len(plan.selected)}] {ticker} ({symbol}): no reported quarters")
                else:
                    before = {_row_key(r) for r in cache.get(ticker, []) if _is_av_row(r)}
                    cache[ticker] = merge_ticker_rows(cache.get(ticker), rows)
                    added = len({_row_key(r) for r in rows} - before)
                    entry["status"] = "ok"
                    entry["reported_rows"] = len(cache[ticker])
                    log(f"  [{i}/{len(plan.selected)}] {ticker} ({symbol}): "
                        f"{len(rows)} reported quarters, {added} new, latest {rows[-1]['earnings_date']}")
                fetched.append(ticker)
        except Terminated:
            stopped = "terminated (timeout) mid-run"
            log("terminated mid-run; saving progress")

    # 3. Verdict, then persist — atomically, and never dropping a row.
    live_without_fetch = [
        t for t in live if not (ticker_state.get(t) or {}).get("last_success_at")
    ]
    critical_missed = [t for t in (plan.due_live if plan else []) if t not in fetched]
    current = calendar_ok and not live_without_fetch and not critical_missed
    if current:
        fetched_at: datetime | None = started
        write_cache_document(earnings_path, cache, fetched_at=started, source=SOURCE)
    else:
        fetched_at = previous.fetched_at
        if dirty or not previous.exists:
            if cache or previous.exists:
                write_cache_document(
                    earnings_path,
                    cache,
                    fetched_at=previous.fetched_at,
                    fetch_time_unknown=previous.fetched_at is None,
                    source=SOURCE if fetched else previous.source,
                )
    if dirty:
        state["format_version"] = STATE_FORMAT_VERSION
        state["updated_at"] = _iso(now())
        if current:
            state["last_current_at"] = _iso(started)
        write_json_atomic(state_path, state)
    elif current:
        state["last_current_at"] = _iso(started)
        write_json_atomic(state_path, state)

    backfill_remaining = sum(
        1 for t in universe if not (ticker_state.get(t) or {}).get("last_success_at")
    )
    if current:
        status, code = "CURRENT", EXIT_CURRENT
    elif stopped or any(t in refused for t in live):
        status, code = "FAILED", EXIT_FAILED
    else:
        status, code = "INCOMPLETE", EXIT_INCOMPLETE

    result = RefreshResult(
        status=status,
        exit_code=code,
        calls=client.calls,
        fetched=fetched,
        no_data=no_data,
        refused=refused,
        stopped=stopped,
        stamped=current,
        fetched_at=fetched_at,
        plan=plan,
        live_without_fetch=live_without_fetch,
        critical_missed=critical_missed,
        backfill_remaining=backfill_remaining,
    )
    live_no_data = sorted(
        t for t in live if (ticker_state.get(t) or {}).get("status") == "no_data"
    )
    if live_no_data:
        result.messages.append(
            "LIVE_NO_DATA: Alpha Vantage has no reported quarters for "
            f"{', '.join(live_no_data)} — earnings_drift cannot trade them; add a "
            "symbol override in data.earnings.refresh.symbol_overrides"
        )
    if live_without_fetch:
        result.messages.append(
            f"{len(live_without_fetch)} live ticker(s) not yet fetched from Alpha "
            f"Vantage (at {budget}/run, ~{-(-len(live_without_fetch) // max(budget, 1))} "
            "more run(s)); the cache stays unstamped until they are"
        )
    if critical_missed:
        result.messages.append(
            f"recent reporters not fetched this run: {', '.join(critical_missed)}"
        )
    return result



# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def live_universe() -> list[str]:
    """The tickers earnings_drift trades (shared.universe is the single source)."""
    from shared.universe import UNIVERSE_REGISTRY

    return list(UNIVERSE_REGISTRY["earnings_drift"])


def pit_universe(live: list[str]) -> list[str]:
    """Live first, then current index members, then every former member."""
    from shared.universe import (
        MEMBERSHIP_SNAPSHOT_PATH,
        MembershipCalendar,
        current_index_members,
    )

    ordered = list(live)
    seen = set(ordered)
    calendar = MembershipCalendar.from_json_file(str(MEMBERSHIP_SNAPSHOT_PATH))
    for ticker in list(current_index_members()) + calendar.all_tickers():
        if ticker not in seen:
            seen.add(ticker)
            ordered.append(ticker)
    return ordered


class _CacheLock:
    """One refresh at a time per cache directory (flock, released on exit)."""

    def __init__(self, cache_dir: Path):
        self.path = cache_dir / ".earnings_refresh.lock"
        self._fh = None

    def __enter__(self) -> "_CacheLock":
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "w")
        try:
            fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._fh.close()
            raise RuntimeError(f"another earnings refresh holds {self.path}") from None
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._fh is not None:
            self._fh.close()


def _raise_terminated(signum: int, frame: Any) -> None:
    raise Terminated()


def main(
    argv: list[str] | None = None,
    *,
    transport: Transport | None = None,
    env: Mapping[str, str] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="Refresh earnings.json from Alpha Vantage (KAN-110)")
    parser.add_argument("--universe", choices=("live", "pit"), default="live",
                        help="live: the earnings_drift universe (default). pit: also "
                             "trickle-backfill the point-in-time universe with any budget "
                             "the live universe does not need")
    parser.add_argument("--tickers", default=None,
                        help="Comma-separated tickers, treated as the live universe")
    parser.add_argument("--cache-dir", default=None,
                        help="Directory for earnings.json and its companions "
                             "(default: data.cache_dir)")
    parser.add_argument("--budget", type=int, default=None,
                        help="EARNINGS calls this run (default: data.earnings.refresh.calls_per_run)")
    parser.add_argument("--min-interval", type=float, default=None,
                        help="Seconds between requests (default: data.earnings.refresh.min_interval_seconds)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the plan from the cached calendar; make no requests")
    args = parser.parse_args(argv)

    from shared.data_cache import configured_data_config

    data_cfg = configured_data_config()
    cfg = data_cfg.earnings.refresh
    cache_dir = resolve_cache_dir(args.cache_dir) if args.cache_dir else resolve_cache_dir(data_cfg.cache_dir)
    budget = cfg.calls_per_run if args.budget is None else args.budget
    interval = cfg.min_interval_seconds if args.min_interval is None else args.min_interval

    if args.tickers:
        live = [t.strip() for t in args.tickers.split(",") if t.strip()]
        universe = list(live)
    else:
        live = live_universe()
        universe = pit_universe(live) if args.universe == "pit" else list(live)

    print(f"Earnings refresh: {len(live)} live / {len(universe)} total tickers, "
          f"budget {budget} EARNINGS call(s) + 1 calendar, {interval:g}s apart, "
          f"cache {cache_dir}")

    if args.dry_run:
        doc = read_cache_document(cache_dir / EARNINGS_FILE)
        calendar = read_cache_document(cache_dir / EARNINGS_CALENDAR_FILE).data
        plan = plan_refresh(
            live=live, universe=universe, cache=doc.data,
            state=_load_state(cache_dir / EARNINGS_STATE_FILE)["tickers"],
            calendar=calendar, now=datetime.now(timezone.utc), budget=budget,
            recent_days=cfg.recent_days, refetch_after_hours=cfg.refetch_after_hours,
        )
        print(f"recent reporters (live): {plan.due_live}")
        print(f"never fetched (live): {len(plan.never_live)}; other recent: "
              f"{len(plan.due_other)}; other never fetched: {len(plan.never_other)}")
        print(f"would fetch: {plan.selected}")
        return EXIT_CURRENT

    key = load_api_key(env)
    if not key:
        print(f"ERROR: no Alpha Vantage key: {API_KEY_VAR} is not set and the keychain "
              "(service algo-poc) has no item for it. Import it: "
              "deploy/launchd/secrets.sh --import", file=sys.stderr)
        print("EARNINGS_REFRESH status=ERROR reason='no API key'")
        return EXIT_ERROR

    client = AlphaVantageClient(
        key,
        transport=transport,
        min_interval_seconds=interval,
        timeout_seconds=cfg.request_timeout_seconds,
    )
    previous_handler = signal.signal(signal.SIGTERM, _raise_terminated)
    try:
        with _CacheLock(cache_dir):
            result = run_refresh(
                client=client,
                cache_dir=cache_dir,
                live=live,
                universe=universe,
                budget=budget,
                recent_days=cfg.recent_days,
                refetch_after_hours=cfg.refetch_after_hours,
                calendar_horizon=cfg.calendar_horizon,
                calendar_retain_days=cfg.calendar_retain_days,
                symbol_overrides=cfg.symbol_overrides,
            )
    except Exception as exc:  # noqa: BLE001 — scrubbed, then reported
        print(client.scrub(traceback.format_exc()), file=sys.stderr)
        print(f"ERROR: {client.scrub(f'{type(exc).__name__}: {exc}')}", file=sys.stderr)
        print(f"EARNINGS_REFRESH status=ERROR reason={client.scrub(type(exc).__name__)!r}")
        return EXIT_ERROR
    finally:
        signal.signal(signal.SIGTERM, previous_handler)

    for message in result.messages:
        print(message)
    print(result.summary_line())
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
