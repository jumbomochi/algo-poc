"""The fundamentals and earnings caches: where they live, and whether to trust them.

KAN-109. ``quality_value`` ranks on ``data/cache/fundamentals.json`` and
``earnings_drift`` trades on ``data/cache/earnings.json``. Both files are
gitignored and written by ``scripts/fetch_fundamentals.py`` /
``scripts/fetch_earnings.py``. Until this module, a missing file loaded as
``{}`` and nothing said a word, so:

* from 2026-09-24 the deploy clone (KAN-72), which had never been given the
  caches, ran both sleeves with no data at all — no entry was possible, live or
  in the shadow, and the evidence read as a quiet market;
* before that, both sleeves ran on caches fetched once on 2026-03-26.

This module makes the condition loud. It owns three things:

1. **The cache path**, from ``data.cache_dir`` in config, resolved against the
   repository root so every reader and writer agrees on one location.
2. **The cache format.** ``fetched_at`` is recorded *inside* the file, because
   a file's mtime is rewritten by a copy, a checkout or a restore and says
   nothing about when the data was fetched. The legacy format — a bare
   ``{ticker: [rows]}`` mapping — still loads unchanged; it simply has an
   unknown fetch time, which the freshness check treats as stale.
3. **Freshness**, per cache, against the thresholds in ``data.fundamentals`` /
   ``data.earnings``, and the mapping from a stale cache to the sleeve it
   degrades (:data:`SLEEVE_CACHE`).

What a degraded sleeve does is decided by its callers, and is the same
everywhere: no new entries from that sleeve and no exits ranked on the
cache (risk exits — stops, time exits — still run), its rolling
shadow is not graded, and a backtest refuses to run it unless told otherwise.
"""

from __future__ import annotations

import json
import math
import os
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

__all__ = [
    "CACHE_FORMAT_VERSION",
    "EARNINGS",
    "EARNINGS_FILE",
    "EARNINGS_WINDOW_DAYS",
    "FUNDAMENTALS",
    "FUNDAMENTALS_FILE",
    "REPO_ROOT",
    "SLEEVE_CACHE",
    "CacheDocument",
    "CacheVerdict",
    "DataCaches",
    "DataHealth",
    "assess_data_health",
    "assess_earnings",
    "assess_fundamentals",
    "cache_path",
    "configured_data_config",
    "load_data_caches",
    "read_cache_document",
    "resolve_cache_dir",
    "write_cache_document",
]

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The envelope version written by the fetchers. Version 1 is the legacy bare
#: mapping, which carries no version at all.
CACHE_FORMAT_VERSION = 2

FUNDAMENTALS = "fundamentals"
EARNINGS = "earnings"
FUNDAMENTALS_FILE = "fundamentals.json"
EARNINGS_FILE = "earnings.json"
_FILES = {FUNDAMENTALS: FUNDAMENTALS_FILE, EARNINGS: EARNINGS_FILE}

#: Days after an announcement that ``build_earnings_lookup`` still returns it.
#: The paper run, the shadow and the backtest all build the lookup with this.
EARNINGS_WINDOW_DAYS = 2

#: Which sleeve each cache feeds. A sleeve not listed here reads neither cache
#: and is never data-degraded.
SLEEVE_CACHE: dict[str, str] = {
    "quality_value": FUNDAMENTALS,
    "earnings_drift": EARNINGS,
}

#: Fetch scripts to name in a message, so the reader knows what to run.
_FETCHER = {
    FUNDAMENTALS: "scripts/fetch_fundamentals.py",
    EARNINGS: "scripts/fetch_earnings.py",
}


# ---------------------------------------------------------------------------
# Location
# ---------------------------------------------------------------------------


def resolve_cache_dir(
    configured: str | os.PathLike[str] | None = None,
    *,
    repo_root: Path = REPO_ROOT,
) -> Path:
    """The cache directory, absolute.

    ``configured`` is ``data.cache_dir``. ``None`` reads it from
    ``config/default.yaml`` (honouring ``ALGO_DATA_CACHE_DIR``), falling back
    to the model default if the config cannot be loaded — the same fallback
    every script here already takes. A relative path is anchored at the
    repository root rather than the cwd.
    """
    if configured is None:
        configured = _configured_cache_dir(repo_root)
    path = Path(configured).expanduser()
    return path if path.is_absolute() else repo_root / path


def configured_data_config(repo_root: Path = REPO_ROOT) -> Any:
    """The ``data`` section of ``config/default.yaml`` at ``repo_root``.

    Read from the repository, not the cwd, and falling back to the model
    defaults (still honouring ``ALGO_DATA_CACHE_DIR``) when the file cannot be
    loaded — the fallback every script here already takes for its config.
    """
    from shared.config import DataConfig, load_config

    try:
        return load_config(str(repo_root / "config" / "default.yaml")).data
    except Exception:  # noqa: BLE001 — a missing config keeps the defaults
        override = os.environ.get("ALGO_DATA_CACHE_DIR")
        return DataConfig(cache_dir=override) if override else DataConfig()


def _configured_cache_dir(repo_root: Path) -> str:
    return configured_data_config(repo_root).cache_dir


def cache_path(cache: str, cache_dir: str | os.PathLike[str] | None = None) -> Path:
    """Path of one named cache (``fundamentals`` / ``earnings``)."""
    return resolve_cache_dir(cache_dir) / _FILES[cache]


# ---------------------------------------------------------------------------
# Format
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CacheDocument:
    """One cache file as read: its rows, and what is known about the fetch."""

    path: Path
    #: ``{ticker: [rows]}`` — exactly what ``build_*_lookup`` takes, whichever
    #: format the file was written in. Empty when the file is missing.
    data: dict[str, list[dict]]
    #: When the rows were fetched, or ``None`` when that is unknown (a legacy
    #: file, or no file at all).
    fetched_at: datetime | None
    exists: bool
    #: Set when the file exists but could not be read as a cache.
    error: str | None = None
    source: str | None = None


def _parse_instant(value: Any) -> datetime | None:
    if not value:
        return None
    moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def unwrap_cache(raw: Any) -> tuple[dict[str, list[dict]], datetime | None, str | None]:
    """Split a parsed cache file into ``(rows, fetched_at, source)``.

    Accepts both formats. The envelope is recognised by its
    ``format_version`` key, which no ticker symbol can collide with.
    """
    if not isinstance(raw, dict):
        raise ValueError(f"expected a JSON object, got {type(raw).__name__}")
    if "format_version" in raw:
        tickers = raw.get("tickers")
        if not isinstance(tickers, dict):
            raise ValueError("cache envelope has no 'tickers' mapping")
        return tickers, _parse_instant(raw.get("fetched_at")), raw.get("source")
    return raw, None, None


def read_cache_document(path: str | os.PathLike[str]) -> CacheDocument:
    """Read a cache file without ever raising.

    A missing file is ``exists=False``; an unreadable one carries ``error``.
    Both have empty ``data``, so a caller that only wants rows gets the same
    ``{}`` the old loaders returned — but the freshness check can now say why.
    """
    path = Path(path)
    if not path.exists():
        return CacheDocument(path=path, data={}, fetched_at=None, exists=False)
    try:
        with open(path) as f:
            data, fetched_at, source = unwrap_cache(json.load(f))
    except (OSError, ValueError, TypeError) as exc:
        return CacheDocument(
            path=path,
            data={},
            fetched_at=None,
            exists=True,
            error=f"{type(exc).__name__}: {exc}",
        )
    return CacheDocument(
        path=path, data=data, fetched_at=fetched_at, exists=True, source=source
    )


def write_cache_document(
    path: str | os.PathLike[str],
    data: Mapping[str, list[dict]],
    *,
    fetched_at: datetime | None = None,
    source: str | None = None,
) -> None:
    """Write rows in the current envelope, stamped with when they were fetched.

    ``fetched_at`` defaults to now. Callers that fetch for minutes should pass
    the instant the fetch STARTED: rows fetched at the start are the oldest in
    the file, and the stamp has to describe the oldest.
    """
    moment = fetched_at or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    payload = {
        "format_version": CACHE_FORMAT_VERSION,
        "fetched_at": moment.astimezone(timezone.utc).isoformat(),
        "source": source,
        "tickers": dict(data),
    }
    path = Path(path)
    os.makedirs(path.parent, exist_ok=True)
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)


@dataclass(frozen=True)
class DataCaches:
    fundamentals: CacheDocument
    earnings: CacheDocument

    def document(self, cache: str) -> CacheDocument:
        return self.fundamentals if cache == FUNDAMENTALS else self.earnings


def load_data_caches(cache_dir: str | os.PathLike[str] | None = None) -> DataCaches:
    root = resolve_cache_dir(cache_dir)
    return DataCaches(
        fundamentals=read_cache_document(root / FUNDAMENTALS_FILE),
        earnings=read_cache_document(root / EARNINGS_FILE),
    )


# ---------------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CacheVerdict:
    """Whether one cache may be traded on, and every reason it may not."""

    cache: str
    path: Path
    problems: list[str] = field(default_factory=list)
    fetched_at: datetime | None = None
    #: Fetch age in days at the instant assessed; ``None`` when unknown.
    age_days: float | None = None

    @property
    def fresh(self) -> bool:
        return not self.problems

    def describe(self) -> str:
        if self.fresh:
            fetched = (
                f"fetched {self.fetched_at:%Y-%m-%d %H:%M} UTC"
                if self.fetched_at
                else "fetch time unknown"
            )
            return f"{self.cache} cache ok ({fetched})"
        return f"{self.cache} cache {'; '.join(self.problems)}"


def _as_utc(moment: datetime) -> datetime:
    """``moment`` in UTC. Naive is taken to BE UTC; aware is converted.

    Converting matters: a check's "today" is ``_as_utc(as_of).date()``, and an
    SGT instant at 05:52 is still the previous UTC day. Attaching rather than
    converting judged the daily report one day ahead of the paper run.
    """
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _presence_problems(doc: CacheDocument, cache: str) -> list[str]:
    if not doc.exists:
        return [
            f"MISSING: {doc.path} does not exist (run {_FETCHER[cache]})"
        ]
    if doc.error:
        return [f"UNREADABLE: {doc.path}: {doc.error}"]
    if not doc.data:
        return [f"EMPTY: {doc.path} holds no tickers"]
    return []


def _fetch_age(
    doc: CacheDocument, *, as_of: datetime, limit_days: float
) -> tuple[float | None, list[str]]:
    if doc.fetched_at is None:
        return None, [
            "STALE: fetch time unknown (no fetched_at — written before "
            "KAN-109), so its age cannot be shown to be within "
            f"{limit_days:g} days"
        ]
    age = (_as_utc(as_of) - doc.fetched_at).total_seconds() / 86400.0
    if age > limit_days:
        return age, [
            f"STALE: fetched {age:.1f} days ago "
            f"({doc.fetched_at:%Y-%m-%d %H:%M} UTC), limit {limit_days:g}"
        ]
    return age, []


def _as_date(value: Any) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def assess_fundamentals(
    doc: CacheDocument,
    *,
    as_of: datetime,
    max_fetch_age_days: float = 14.0,
    max_period_age_days: int = 167,
    max_stale_ticker_fraction: float = 0.20,
) -> CacheVerdict:
    """Judge the fundamentals cache as of ``as_of`` (an aware instant).

    Stale when the fetch is too old or unknown, **or** when more than
    ``max_stale_ticker_fraction`` of tickers have a latest period older than
    ``max_period_age_days`` on ``as_of``'s date. The period rule is judged on
    the rows themselves, so it holds for a legacy cache too: the 2026-03-26
    cache's dominant period (2025-12-31) passes it until 2026-06-16.
    """
    problems = _presence_problems(doc, FUNDAMENTALS)
    if problems:
        return CacheVerdict(FUNDAMENTALS, doc.path, problems)

    age, age_problems = _fetch_age(
        doc, as_of=as_of, limit_days=max_fetch_age_days
    )
    problems.extend(age_problems)

    today = _as_utc(as_of).date()
    latest: dict[str, date] = {}
    for ticker, rows in doc.data.items():
        periods = [_as_date(r["report_date"]) for r in rows if r.get("report_date")]
        if periods:
            latest[ticker] = max(periods)
    if not latest:
        problems.append("EMPTY: no ticker carries a report_date")
    else:
        stale = sorted(
            t for t, period in latest.items()
            if (today - period).days > max_period_age_days
        )
        share = len(stale) / len(latest)
        if share > max_stale_ticker_fraction:
            newest = max(latest.values())
            typical = statistics.median_low(sorted(latest.values()))
            problems.append(
                f"STALE: {len(stale)} of {len(latest)} tickers' latest period "
                f"is over {max_period_age_days} days old on {today} "
                f"(typical latest period {typical}, newest {newest}); a "
                "fresh fetch would hold a newer quarter"
            )

    return CacheVerdict(FUNDAMENTALS, doc.path, problems, doc.fetched_at, age)


def assess_earnings(
    doc: CacheDocument,
    *,
    as_of: datetime,
    window_days: int = EARNINGS_WINDOW_DAYS,
    max_fetch_age_days: float = 2.0,
) -> CacheVerdict:
    """Judge the earnings cache as of ``as_of`` (an aware instant).

    The lookup answers "was there an announcement in the last
    ``window_days`` days?", so the cache has to cover that window: its
    history must reach back to the window's start, and it must have been
    fetched recently enough to hold the announcements inside it — a cache
    covers nothing announced after its own fetch. ``max_fetch_age_days``
    bounds the second.

    An event with a non-finite surprise (a scheduled announcement with no
    reported EPS) is not coverage: it is the shape every announcement after
    the 2026-03-26 fetch took in the stale cache.
    """
    problems = _presence_problems(doc, EARNINGS)
    if problems:
        return CacheVerdict(EARNINGS, doc.path, problems)

    age, age_problems = _fetch_age(doc, as_of=as_of, limit_days=max_fetch_age_days)
    window_start = _as_utc(as_of).date() - timedelta(days=window_days)
    if age_problems and doc.fetched_at is not None:
        age_problems = [
            age_problems[0]
            + f"; it holds no announcement after {doc.fetched_at.date()}, and "
            f"today's lookup window opens {window_start}"
        ]
    problems.extend(age_problems)

    reported = [
        _as_date(e["earnings_date"])
        for events in doc.data.values()
        for e in events
        if e.get("earnings_date") and _finite(e.get("surprise_pct"))
    ]
    if not reported:
        problems.append("EMPTY: no event carries a reported surprise")
    elif min(reported) > window_start:
        problems.append(
            f"STALE: history starts {min(reported)}, after today's lookup "
            f"window opens ({window_start})"
        )

    return CacheVerdict(EARNINGS, doc.path, problems, doc.fetched_at, age)


def _finite(value: Any) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


@dataclass(frozen=True)
class DataHealth:
    """Every cache's verdict, and the sleeves they degrade."""

    verdicts: dict[str, CacheVerdict]

    def degraded_sleeves(self, sleeves: Iterable[str] | None = None) -> dict[str, str]:
        """``{sleeve: reason}`` for each listed sleeve whose cache is not fresh.

        ``sleeves`` defaults to every sleeve that reads a cache. A sleeve that
        reads neither is never degraded.
        """
        names = SLEEVE_CACHE if sleeves is None else sleeves
        out: dict[str, str] = {}
        for sleeve in names:
            cache = SLEEVE_CACHE.get(sleeve)
            verdict = self.verdicts.get(cache) if cache else None
            if verdict is not None and not verdict.fresh:
                out[sleeve] = verdict.describe()
        return out

    @property
    def healthy(self) -> bool:
        return all(v.fresh for v in self.verdicts.values())

    def alert_message(self, sleeves: Iterable[str] | None = None) -> str | None:
        """One page for the run, naming each sleeve, its cache and why."""
        degraded = self.degraded_sleeves(sleeves)
        if not degraded:
            return None
        lines = "; ".join(
            f"{sleeve} — {reason}" for sleeve, reason in sorted(degraded.items())
        )
        return (
            f"DATA-DEGRADED: {', '.join(sorted(degraded))} will place NO new "
            f"entries this run (risk exits still run, rank replacements are held; "
            f"the shadow is not graded): "
            f"{lines}. Refresh the cache(s) in data.cache_dir; see KAN-109."
        )


def assess_data_health(
    caches: DataCaches,
    *,
    as_of: datetime,
    config: Any | None = None,
) -> DataHealth:
    """Assess both caches with the thresholds in ``config`` (a ``DataConfig``).

    ``as_of`` is an aware instant. The paper run passes the start of its bar
    fetch; a backtest passes the close of its last bar, so a cache fetched
    after a historical window ends is fresh for that window.
    """
    from shared.config import DataConfig

    cfg = config if config is not None else DataConfig()
    return DataHealth(
        verdicts={
            FUNDAMENTALS: assess_fundamentals(
                caches.fundamentals,
                as_of=as_of,
                max_fetch_age_days=cfg.fundamentals.max_fetch_age_days,
                max_period_age_days=cfg.fundamentals.max_period_age_days,
                max_stale_ticker_fraction=cfg.fundamentals.max_stale_ticker_fraction,
            ),
            EARNINGS: assess_earnings(
                caches.earnings,
                as_of=as_of,
                max_fetch_age_days=cfg.earnings.max_fetch_age_days,
            ),
        }
    )
