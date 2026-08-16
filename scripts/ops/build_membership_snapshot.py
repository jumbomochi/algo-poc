"""Build ``data/universe/sp500_membership.json`` from Wikipedia revision history.

Why this exists
---------------
The point-in-time membership file is what removes survivorship bias from the
headline backtest, and without it ``scripts/divergence_monitor.py`` exits 3 —
the monitor is BLIND and the paper track record accruing toward the go-live
gate has no implementation-fidelity check behind it (KAN-23).

The history itself is not something the repo can invent. This script derives it
from the *revision history* of the Wikipedia article "List of S&P 500
companies": for each quarterly date in the window it asks the MediaWiki API for
the last revision at or before that date, renders it, and reads the constituents
table. That table also carries the GICS sector of every name, which is how the
sectors of long-delisted members (the ones ``SECTOR_MAP`` has never covered)
come along for free.

Provenance and its limits
-------------------------
Every snapshot records the exact ``revid`` and revision timestamp it came from,
so the file is reproducible and auditable against Wikipedia. Two honest caveats,
both documented in ``docs/operations/backtest-baseline.md``:

* Wikipedia lags real index changes by days, so a constituent's entry/exit date
  is accurate to roughly a week, not to the session. Fine for a divergence
  baseline; not fine for attribution or for an index-replication claim.
* An editor can leave the article briefly inconsistent. The parser therefore
  refuses a table with implausibly few rows rather than writing a snapshot that
  makes most of the index untradable on that date.

Usage
-----
    python scripts/ops/build_membership_snapshot.py \\
        --start 2015-01-01 --output data/universe/sp500_membership.json

``--end`` defaults to today. Roughly one API call per quarter, rate-limited.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

WIKI_API = "https://en.wikipedia.org/w/api.php"
WIKI_PAGE = "List of S&P 500 companies"
USER_AGENT = "algo-poc-membership-snapshot/1.0 (https://github.com/jumbomochi/algo-poc)"

DEFAULT_OUTPUT = "data/universe/sp500_membership.json"
DEFAULT_SECTOR_MODULE = "shared/historical_sectors.py"
DEFAULT_START = date(2015, 1, 1)

#: The S&P 500 has had 500-ish members throughout the window. A parse that
#: yields far fewer has read the wrong table or a mid-edit revision.
MINIMUM_CONSTITUENTS = 400

#: GICS sector names as Wikipedia writes them -> the labels already in use in
#: ``shared.universe.SECTOR_MAP``. Deliberately exhaustive and strict: a GICS
#: label this map does not know breaks the build, because the alternative is a
#: name quietly resolving to "Unknown" and joining the pseudo-sector that
#: froze all new entries on 2026-08-07.
GICS_TO_REPO_SECTOR: dict[str, str] = {
    "Communication Services": "Communication Services",
    # Wikipedia spelled the new sector plural for its first ~year (observed on
    # the 2019-10-01 revision).
    "Communications Services": "Communication Services",
    "Consumer Discretionary": "Consumer Discretionary",
    "Consumer Staples": "Consumer Staples",
    "Energy": "Energy",
    "Financials": "Financials",
    "Health Care": "Healthcare",
    "Healthcare": "Healthcare",
    "Industrials": "Industrials",
    "Information Technology": "Technology",
    "Materials": "Materials",
    "Real Estate": "Real Estate",
    "Utilities": "Utilities",
    # Pre-2018 GICS, before the Communication Services reshuffle. Both of the
    # old buckets fold into what the repo calls Communication Services, which
    # is where their surviving members sit today.
    "Telecommunications Services": "Communication Services",
    "Telecommunication Services": "Communication Services",
}


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def normalize_symbol(symbol: str) -> str:
    """Wikipedia's ticker spelling -> this repo's.

    Class shares are the only real difference: Wikipedia writes ``BRK.B`` and
    every universe list here writes ``BRK B`` (IB's own convention).
    """
    cleaned = symbol.replace(" ", " ").replace("­", "").strip()
    return cleaned.replace(".", " ").upper()


def repo_sector(gics: str) -> str:
    """Translate a GICS sector label into the repo's own vocabulary."""
    key = gics.replace(" ", " ").strip()
    try:
        return GICS_TO_REPO_SECTOR[key]
    except KeyError:
        raise KeyError(
            f"unrecognised GICS sector {key!r}; add it to GICS_TO_REPO_SECTOR "
            "rather than letting the name fall through to 'Unknown'"
        ) from None


_TABLE_RE = re.compile(r"<table\b.*?</table>", re.S | re.I)
_ROW_RE = re.compile(r"<tr\b.*?</tr>", re.S | re.I)
_CELL_RE = re.compile(r"<t[hd]\b.*?</t[hd]>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")

#: Header wording has drifted across the window: "Ticker symbol" (2015),
#: "Symbol" (2018+). Anchor on the sector column, which never moved.
_SYMBOL_HEADERS = {"symbol", "ticker symbol", "ticker"}
_SECTOR_HEADERS = {"gics sector", "gics  sector", "sector"}


def _cell_text(cell: str) -> str:
    return unescape(_TAG_RE.sub("", cell)).replace(" ", " ").strip()


def parse_constituents(html: str, minimum_rows: int = 0) -> dict[str, str]:
    """Read ``{ticker: gics_sector}`` out of a rendered revision of the article.

    Selects the table by its header cells rather than by position or id, because
    both have changed over the window and the page also renders a
    component-changes table with a ``Ticker`` column of its own.
    """
    for table in _TABLE_RE.findall(html):
        rows = _ROW_RE.findall(table)
        if not rows:
            continue
        header = [_cell_text(c).lower() for c in _CELL_RE.findall(rows[0])]
        try:
            symbol_col = next(
                i for i, h in enumerate(header) if h in _SYMBOL_HEADERS
            )
            sector_col = next(
                i for i, h in enumerate(header) if h in _SECTOR_HEADERS
            )
        except StopIteration:
            continue

        parsed: dict[str, str] = {}
        for row in rows[1:]:
            cells = _CELL_RE.findall(row)
            if len(cells) <= max(symbol_col, sector_col):
                continue
            symbol = normalize_symbol(_cell_text(cells[symbol_col]))
            sector = _cell_text(cells[sector_col])
            if not symbol or not sector:
                continue
            parsed[symbol] = sector
        if len(parsed) < minimum_rows:
            raise ValueError(
                f"only {len(parsed)} constituents parsed (expected at least "
                f"{minimum_rows}); the revision is probably mid-edit — pick a "
                "different date or widen the search"
            )
        return parsed

    raise ValueError(
        "no constituents table found: no table has both a symbol column and a "
        "GICS Sector column"
    )


def snapshot_dates(start: date, end: date, months: int = 3) -> list[date]:
    """Quarterly dates across ``[start, end]``, with ``end`` always included.

    Quarterly is the resolution the backtest needs: it bounds how long a name
    can be traded after leaving the index (or missed after joining) to one
    quarter, while keeping the file to ~40 API calls and a couple of MB.
    """
    if end < start:
        raise ValueError(f"end {end} is before start {start}")
    days: list[date] = []
    cursor = start
    while cursor <= end:
        days.append(cursor)
        month = cursor.month - 1 + months
        cursor = date(cursor.year + month // 12, month % 12 + 1, cursor.day)
    if days[-1] != end:
        days.append(end)
    return days


def build_envelope(
    observations: Sequence[Mapping[str, Any]],
    generated_at: date,
) -> dict[str, Any]:
    """Assemble the on-disk envelope from per-date observations.

    Consecutive snapshots with identical membership are collapsed: a snapshot is
    "effective from this date until the next", so repeating an unchanged list
    carries no information and multiplies the file size.
    """
    if not observations:
        raise ValueError("need at least one observation to build an envelope")

    snapshots: dict[str, list[str]] = {}
    revisions: dict[str, dict[str, Any]] = {}
    sectors: dict[str, str] = {}
    previous: frozenset[str] | None = None

    for obs in observations:
        members = frozenset(obs["members"])
        sectors.update(obs["members"])
        if members == previous:
            continue
        previous = members
        key = obs["requested"].isoformat()
        snapshots[key] = sorted(members)
        revisions[key] = {
            "revid": obs["revid"],
            "timestamp": obs["timestamp"],
        }

    return {
        "source": (
            "Wikipedia revision history of 'List of S&P 500 companies', read "
            "quarterly via the MediaWiki API. Regenerate with "
            "scripts/ops/build_membership_snapshot.py. Constituent dates are "
            "accurate to roughly a week (Wikipedia lags real index changes), "
            "which is adequate for a divergence baseline and not for "
            "attribution."
        ),
        "generated_at": generated_at.isoformat(),
        "generator": "scripts/ops/build_membership_snapshot.py",
        "snapshots": snapshots,
        "revisions": revisions,
        "sectors": dict(sorted(sectors.items())),
    }


_SECTOR_MODULE_HEADER = '''"""Sector labels for every historical S&P 500 member in the window.

AUTOGENERATED by ``scripts/ops/build_membership_snapshot.py`` from the same
Wikipedia revisions as ``data/universe/sp500_membership.json`` — do not edit by
hand; regenerate both together or ``tests/shared/test_universe.py`` will fail.

This exists because ``SECTOR_MAP`` covers only the present-day top 100. Every
name that has left the index since {start} — the delistings and acquisitions a
point-in-time backtest exists to include — would otherwise resolve to
"Unknown", and the risk engine lumps all Unknowns into one pseudo-sector that
freezes new entries once it crosses ``sector_concentration_pct`` (the
2026-08-07 incident). Curated ``SECTOR_MAP`` entries take precedence over this
map, so nothing here can change how a currently-traded name is bucketed.

Generated {generated_at} from {snapshots} snapshots; {count} tickers.
"""
from __future__ import annotations

HISTORICAL_SECTOR_MAP: dict[str, str] = {{
'''


def render_sector_module(
    sectors: Mapping[str, str],
    curated: Mapping[str, str],
    *,
    generated_at: date,
    start: date,
    snapshots: int,
) -> str:
    """Render the historical-sector module for every member not already curated."""
    extra = {t: s for t, s in sorted(sectors.items()) if t not in curated}
    body = "".join(f'    {t!r}: {s!r},\n' for t, s in extra.items())
    header = _SECTOR_MODULE_HEADER.format(
        start=start.isoformat(),
        generated_at=generated_at.isoformat(),
        snapshots=snapshots,
        count=len(extra),
    )
    return header + body + "}\n"


def sector_conflicts(
    sectors: Mapping[str, str], curated: Mapping[str, str]
) -> dict[str, tuple[str, str]]:
    """Names the curated map and the index disagree about — ``{t: (repo, index)}``.

    Reported, never auto-applied: a curated label is what the live risk engine
    buckets a *currently held* name by, so changing one is a trading-behaviour
    change that belongs in its own reviewed commit.
    """
    return {
        t: (curated[t], s)
        for t, s in sorted(sectors.items())
        if t in curated and curated[t] != s
    }


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------

def _api(params: Mapping[str, Any]) -> dict[str, Any]:
    query = urllib.parse.urlencode({**params, "format": "json"})
    request = urllib.request.Request(
        f"{WIKI_API}?{query}", headers={"User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def revision_at(day: date, page: str = WIKI_PAGE) -> tuple[int, str]:
    """The last revision of ``page`` at or before ``day`` — ``(revid, ts)``."""
    payload = _api(
        {
            "action": "query",
            "prop": "revisions",
            "titles": page,
            "rvlimit": 1,
            "rvdir": "older",
            "rvstart": f"{day.isoformat()}T23:59:59Z",
            "rvprop": "ids|timestamp",
            "formatversion": 2,
        }
    )
    pages = payload["query"]["pages"]
    revisions = pages[0].get("revisions") if pages else None
    if not revisions:
        raise ValueError(f"no revision of {page!r} exists at or before {day}")
    return revisions[0]["revid"], revisions[0]["timestamp"]


def revision_html(revid: int) -> str:
    payload = _api(
        {"action": "parse", "oldid": revid, "prop": "text", "formatversion": 2}
    )
    return payload["parse"]["text"]


def collect_observations(
    days: Iterable[date],
    *,
    minimum_rows: int = MINIMUM_CONSTITUENTS,
    pause_seconds: float = 0.5,
    log=print,
) -> list[dict[str, Any]]:
    """Fetch and parse one revision per date. Fails loudly on a bad parse."""
    observations: list[dict[str, Any]] = []
    for day in days:
        revid, timestamp = revision_at(day)
        members = parse_constituents(revision_html(revid), minimum_rows=minimum_rows)
        observations.append(
            {
                "requested": day,
                "revid": revid,
                "timestamp": timestamp,
                "members": {t: repo_sector(s) for t, s in members.items()},
            }
        )
        log(f"  {day}  rev {revid} ({timestamp[:10]})  {len(members)} members")
        time.sleep(pause_seconds)
    return observations


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--start", default=DEFAULT_START.isoformat())
    parser.add_argument(
        "--end",
        default=None,
        help="Defaults to today (UTC), so the file covers the whole window.",
    )
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--sector-module",
        default=DEFAULT_SECTOR_MODULE,
        help="Where to write the generated HISTORICAL_SECTOR_MAP module.",
    )
    parser.add_argument("--months", type=int, default=3, help="Snapshot cadence.")
    parser.add_argument(
        "--minimum-rows",
        type=int,
        default=MINIMUM_CONSTITUENTS,
        help="Refuse a revision that parses to fewer constituents than this.",
    )
    args = parser.parse_args(argv)

    start = date.fromisoformat(args.start)
    end = (
        date.fromisoformat(args.end)
        if args.end
        else datetime.now(timezone.utc).date()
    )
    days = snapshot_dates(start, end, months=args.months)
    print(f"Reading {len(days)} revisions of {WIKI_PAGE!r} ({start} .. {end})")

    observations = collect_observations(days, minimum_rows=args.minimum_rows)
    envelope = build_envelope(
        observations, generated_at=datetime.now(timezone.utc).date()
    )

    def _resolve(path: str) -> Path:
        candidate = Path(path)
        return candidate if candidate.is_absolute() else REPO_ROOT / candidate

    out = _resolve(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(envelope, indent=1, sort_keys=False) + "\n")

    snaps = envelope["snapshots"]
    print(
        f"\nWrote {out} — {len(snaps)} distinct snapshots "
        f"({list(snaps)[0]} .. {list(snaps)[-1]}), "
        f"{len(envelope['sectors'])} tickers ever a member, "
        f"{out.stat().st_size / 1e6:.1f} MB"
    )

    from shared.universe import SECTOR_MAP

    module = _resolve(args.sector_module)
    module.write_text(
        render_sector_module(
            envelope["sectors"],
            SECTOR_MAP,
            generated_at=envelope["generated_at"]
            and date.fromisoformat(envelope["generated_at"]),
            start=start,
            snapshots=len(snaps),
        )
    )
    print(f"Wrote {module}")

    conflicts = sector_conflicts(envelope["sectors"], SECTOR_MAP)
    if conflicts:
        print(
            f"\n{len(conflicts)} curated SECTOR_MAP label(s) disagree with the "
            "index. NOT auto-applied — a curated label buckets a currently held "
            "name, so changing one is a trading-behaviour change:"
        )
        for ticker, (repo, index) in conflicts.items():
            print(f"  {ticker}: SECTOR_MAP={repo!r} index={index!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
