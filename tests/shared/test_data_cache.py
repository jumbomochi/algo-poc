"""KAN-109: the fundamentals/earnings caches — location, format, freshness.

The failure this guards against was silence: a missing cache loaded as ``{}``
and both data-driven sleeves ran with nothing to decide on for two weeks. So
the tests pin three things: the path comes from config and is anchored at the
repo root, ``fetched_at`` is recorded in a format the lookups never see (old
bare caches still load), and every way a cache can be unfit — missing, unknown
age, old fetch, superseded periods, a window it does not cover — is reported
by name.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts.fetch_earnings import (
    build_earnings_lookup,
    load_earnings_cache,
    save_earnings_cache,
)
from scripts.fetch_fundamentals import (
    build_fundamentals_lookup,
    load_fundamentals_cache,
    save_fundamentals_cache,
)
from shared.config import DataConfig, load_config
from shared.data_cache import (
    EARNINGS,
    FUNDAMENTALS,
    REPO_ROOT,
    SLEEVE_CACHE,
    assess_data_health,
    assess_earnings,
    assess_fundamentals,
    load_data_caches,
    read_cache_document,
    resolve_cache_dir,
    write_cache_document,
)

NOW = datetime(2026, 10, 6, 21, 15, tzinfo=timezone.utc)


def _fund_rows(period: str) -> list[dict]:
    return [{"report_date": period, "roe": 0.2, "debt_equity": 0.5,
             "profit_margin": 0.2, "sector": "Technology"}]


def _fundamentals(periods: dict[str, str]) -> dict[str, list[dict]]:
    return {t: _fund_rows(p) for t, p in periods.items()}


def _earnings(dates: list[str], surprise: float = 8.0) -> dict[str, list[dict]]:
    return {"AAA": [{"earnings_date": d, "actual_eps": 1.1,
                     "estimate_eps": 1.0, "surprise_pct": surprise}
                    for d in dates]}


# ---------------------------------------------------------------------------
# Location
# ---------------------------------------------------------------------------

def test_the_default_config_names_data_cache():
    assert load_config(str(REPO_ROOT / "config/default.yaml")).data.cache_dir == "data/cache"
    assert DataConfig().cache_dir == "data/cache"


def test_a_relative_cache_dir_is_anchored_at_the_repo_not_the_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert resolve_cache_dir("data/cache") == REPO_ROOT / "data" / "cache"


def test_an_absolute_cache_dir_is_used_as_given(tmp_path):
    assert resolve_cache_dir(str(tmp_path)) == tmp_path


def test_the_env_override_reaches_the_resolver(tmp_path, monkeypatch):
    monkeypatch.setenv("ALGO_DATA_CACHE_DIR", str(tmp_path))
    assert resolve_cache_dir() == tmp_path


# ---------------------------------------------------------------------------
# Format: envelope written, both formats read, lookups unchanged
# ---------------------------------------------------------------------------

def test_the_fetchers_write_fetched_at_inside_an_envelope(tmp_path):
    path = tmp_path / "fundamentals.json"
    save_fundamentals_cache(_fundamentals({"AAA": "2026-06-30"}), str(path),
                            fetched_at=NOW, source="test")
    raw = json.loads(path.read_text())
    assert raw["format_version"] == 2
    assert raw["fetched_at"] == NOW.isoformat()
    assert raw["source"] == "test"
    assert set(raw["tickers"]) == {"AAA"}


def test_the_loaders_strip_the_envelope(tmp_path):
    """A top-level fetched_at handed to the lookup builder would be iterated
    as a ticker and crash it; the loaders return rows only."""
    rows = _fundamentals({"AAA": "2026-06-30"})
    path = tmp_path / "f.json"
    save_fundamentals_cache(rows, str(path), fetched_at=NOW)
    assert load_fundamentals_cache(str(path)) == rows

    events = _earnings(["2026-10-05"])
    epath = tmp_path / "e.json"
    save_earnings_cache(events, str(epath), fetched_at=NOW)
    assert load_earnings_cache(str(epath)) == events


def test_a_legacy_bare_cache_still_loads_with_an_unknown_fetch_time(tmp_path):
    rows = _fundamentals({"AAA": "2026-06-30"})
    path = tmp_path / "fundamentals.json"
    path.write_text(json.dumps(rows))

    assert load_fundamentals_cache(str(path)) == rows
    doc = read_cache_document(path)
    assert doc.exists and doc.data == rows and doc.fetched_at is None


@pytest.mark.parametrize("as_of", [date(2026, 8, 13), date(2026, 8, 14), date(2026, 10, 1)])
def test_lookups_are_identical_for_legacy_and_enveloped_caches(tmp_path, as_of):
    """The regression half of AC3: same rows, same answers, whichever format."""
    rows = {
        "AAA": _fund_rows("2026-03-31") + _fund_rows("2026-06-30"),
        "BBB": _fund_rows("2026-06-30"),
    }
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps(rows))
    enveloped = tmp_path / "new.json"
    write_cache_document(enveloped, rows, fetched_at=NOW)

    a = build_fundamentals_lookup(load_fundamentals_cache(str(legacy)))
    b = build_fundamentals_lookup(load_fundamentals_cache(str(enveloped)))
    for ticker in ("AAA", "BBB", "CCC"):
        assert a(ticker, as_of) == b(ticker, as_of)

    events = _earnings(["2026-09-30", "2026-10-02"])
    legacy_e = tmp_path / "legacy_e.json"
    legacy_e.write_text(json.dumps(events))
    new_e = tmp_path / "new_e.json"
    write_cache_document(new_e, events, fetched_at=NOW)
    ea = build_earnings_lookup(load_earnings_cache(str(legacy_e)))
    eb = build_earnings_lookup(load_earnings_cache(str(new_e)))
    for day in (date(2026, 9, 30), date(2026, 10, 2), date(2026, 10, 3), date(2026, 10, 5)):
        assert ea("AAA", day) == eb("AAA", day)


def test_a_missing_file_still_loads_as_empty_but_is_known_missing(tmp_path):
    assert load_fundamentals_cache(str(tmp_path / "nope.json")) == {}
    doc = read_cache_document(tmp_path / "nope.json")
    assert doc.exists is False


def test_a_corrupt_file_is_reported_not_raised(tmp_path):
    path = tmp_path / "earnings.json"
    path.write_text("{not json")
    doc = read_cache_document(path)
    assert doc.exists and doc.error and doc.data == {}
    verdict = assess_earnings(doc, as_of=NOW)
    assert not verdict.fresh
    assert "UNREADABLE" in verdict.describe()


# ---------------------------------------------------------------------------
# Fundamentals freshness
# ---------------------------------------------------------------------------

def _fund_doc(tmp_path, periods, fetched_at=NOW):
    path = tmp_path / "fundamentals.json"
    if fetched_at is None:
        path.write_text(json.dumps(_fundamentals(periods)))
    else:
        write_cache_document(path, _fundamentals(periods), fetched_at=fetched_at)
    return read_cache_document(path)


def test_a_fresh_fundamentals_cache_passes(tmp_path):
    doc = _fund_doc(tmp_path, {"AAA": "2026-06-30", "BBB": "2026-06-30"},
                    fetched_at=NOW - timedelta(days=3))
    verdict = assess_fundamentals(doc, as_of=NOW)
    assert verdict.fresh, verdict.problems
    assert verdict.age_days == pytest.approx(3.0)


def test_missing_fundamentals_name_the_path_and_the_fetcher(tmp_path):
    verdict = assess_fundamentals(read_cache_document(tmp_path / "fundamentals.json"), as_of=NOW)
    assert not verdict.fresh
    text = verdict.describe()
    assert "MISSING" in text and str(tmp_path) in text
    assert "fetch_fundamentals.py" in text


def test_an_unknown_fetch_time_is_stale(tmp_path):
    doc = _fund_doc(tmp_path, {"AAA": "2026-06-30"}, fetched_at=None)
    verdict = assess_fundamentals(doc, as_of=NOW)
    assert not verdict.fresh
    assert "fetch time unknown" in verdict.describe()


def test_an_old_fetch_is_stale(tmp_path):
    doc = _fund_doc(tmp_path, {"AAA": "2026-06-30"}, fetched_at=NOW - timedelta(days=15))
    verdict = assess_fundamentals(doc, as_of=NOW, max_fetch_age_days=14)
    assert not verdict.fresh
    assert "fetched 15.0 days ago" in verdict.describe()


def test_superseded_periods_are_stale_even_when_freshly_fetched(tmp_path):
    """The substantive check: the rows, not the stamp. 2026-03-31 is 189 days
    old on 2026-10-06, past the 167-day bound for every ticker."""
    doc = _fund_doc(tmp_path, {"AAA": "2026-03-31", "BBB": "2026-03-31"})
    verdict = assess_fundamentals(doc, as_of=NOW)
    assert not verdict.fresh
    assert "2 of 2 tickers" in verdict.describe()


def test_a_minority_of_late_filers_does_not_stale_the_cache(tmp_path):
    periods = {f"T{i}": "2026-06-30" for i in range(9)}
    periods["LATE"] = "2026-03-31"
    doc = _fund_doc(tmp_path, periods)
    assert assess_fundamentals(doc, as_of=NOW).fresh  # 10% <= 20%


def test_the_period_bound_never_fires_before_the_next_quarter_exists(tmp_path):
    """167 = the longest quarter (92d) + 75 days. Q2 (ends 06-30) is
    superseded once Q3 (09-30) is filed; the bound gives it until 12-14."""
    doc = _fund_doc(tmp_path, {"AAA": "2026-06-30"})
    day_167 = datetime(2026, 12, 14, 12, tzinfo=timezone.utc)  # 06-30 + 167
    assert assess_fundamentals(doc, as_of=day_167, max_fetch_age_days=999).fresh
    day_168 = datetime(2026, 12, 15, 12, tzinfo=timezone.utc)
    assert not assess_fundamentals(doc, as_of=day_168, max_fetch_age_days=999).fresh


def test_a_fresh_mid_february_cache_is_not_stale(tmp_path):
    """PR #235 review: Q4 has no 10-Q — it comes with the 10-K, due 60-75 days
    after year-end. On 2026-02-20 a fresh fetch still shows 2025-09-30 for most
    calendar-FY companies, and the old 137-day bound called that STALE every
    February. Reproduced with the reviewer's shape: 80 of 100 at 09-30."""
    periods = {f"Q3_{i}": "2025-09-30" for i in range(80)}
    periods.update({f"Q4_{i}": "2025-12-31" for i in range(20)})
    feb_20 = datetime(2026, 2, 20, 12, tzinfo=timezone.utc)
    doc = _fund_doc(tmp_path, periods, fetched_at=feb_20 - timedelta(days=1))
    verdict = assess_fundamentals(doc, as_of=feb_20)
    assert verdict.fresh, verdict.problems
    # The old bound is what made it fail.
    assert not assess_fundamentals(doc, as_of=feb_20, max_period_age_days=137).fresh


def test_a_genuinely_stale_cache_is_still_caught_with_the_wider_bound(tmp_path):
    """The 2026-03-26 cache's shape, judged on 2026-07-01: freshly re-stamped,
    its 2025-12-31 periods are 182 days old — a 10-K and a 10-Q have both been
    filed since — and the check still fires."""
    periods = {f"T{i}": "2025-12-31" for i in range(82)}
    periods.update({f"J{i}": "2026-01-31" for i in range(12)})
    periods.update({"F0": "2026-02-28", "F1": "2026-02-28", "N0": "2025-11-30"})
    july_1 = datetime(2026, 7, 1, 12, tzinfo=timezone.utc)
    doc = _fund_doc(tmp_path, periods, fetched_at=july_1)
    verdict = assess_fundamentals(doc, as_of=july_1)
    assert not verdict.fresh
    assert "83 of 97 tickers" in verdict.describe()


def test_an_aware_instant_is_converted_to_utc_not_relabelled(tmp_path):
    """PR #235 review: the daily report passes local (SGT) time. 05:52 SGT on
    2026-11-14 is 21:52 UTC on 11-13, so "today" for the period rule is 11-13
    — the same day the paper run, which passes UTC, judges."""
    from zoneinfo import ZoneInfo

    doc = _fund_doc(tmp_path, {"AAA": "2026-05-30"})  # 167 days -> 11-13
    sgt = datetime(2026, 11, 14, 5, 52, tzinfo=ZoneInfo("Asia/Singapore"))
    utc = sgt.astimezone(timezone.utc)
    assert utc.date() == date(2026, 11, 13)
    a = assess_fundamentals(doc, as_of=sgt, max_fetch_age_days=999)
    b = assess_fundamentals(doc, as_of=utc, max_fetch_age_days=999)
    assert a.fresh and b.fresh  # 11-13 is day 167, still inside the bound
    assert a.problems == b.problems


# ---------------------------------------------------------------------------
# Earnings freshness
# ---------------------------------------------------------------------------

def _earn_doc(tmp_path, dates, fetched_at=NOW, surprise=8.0):
    path = tmp_path / "earnings.json"
    write_cache_document(path, _earnings(dates, surprise), fetched_at=fetched_at)
    return read_cache_document(path)


def test_a_fresh_earnings_cache_passes(tmp_path):
    doc = _earn_doc(tmp_path, ["2026-07-20", "2026-10-02"], fetched_at=NOW - timedelta(hours=6))
    assert assess_earnings(doc, as_of=NOW).fresh


def test_an_earnings_cache_older_than_the_lookup_window_is_stale(tmp_path):
    """A cache covers nothing announced after its own fetch, and the sleeve
    enters within 2 days of an announcement — so a 3-day-old cache has missed
    announcements still tradeable today."""
    doc = _earn_doc(tmp_path, ["2026-07-20"], fetched_at=NOW - timedelta(days=3))
    verdict = assess_earnings(doc, as_of=NOW)
    assert not verdict.fresh
    text = verdict.describe()
    assert "fetched 3.0 days ago" in text
    assert "lookup window opens 2026-10-04" in text


def test_an_earnings_history_that_starts_inside_the_window_does_not_cover_it(tmp_path):
    doc = _earn_doc(tmp_path, ["2026-10-05"])
    verdict = assess_earnings(doc, as_of=NOW)
    assert not verdict.fresh
    assert "history starts 2026-10-05" in verdict.describe()


def test_events_with_no_reported_surprise_are_not_coverage(tmp_path):
    doc = _earn_doc(tmp_path, ["2026-07-20"], surprise=float("nan"))
    verdict = assess_earnings(doc, as_of=NOW)
    assert not verdict.fresh
    assert "no event carries a reported surprise" in verdict.describe()


# ---------------------------------------------------------------------------
# Sleeve mapping and the one alert
# ---------------------------------------------------------------------------

def test_each_cache_degrades_exactly_its_own_sleeve():
    assert SLEEVE_CACHE == {"quality_value": FUNDAMENTALS, "earnings_drift": EARNINGS}


def test_missing_caches_degrade_both_sleeves_and_nothing_else(tmp_path):
    health = assess_data_health(load_data_caches(tmp_path), as_of=NOW)
    degraded = health.degraded_sleeves(
        ["momentum", "quality_value", "earnings_drift", "tail_risk_hedge"]
    )
    assert set(degraded) == {"quality_value", "earnings_drift"}
    assert "MISSING" in degraded["quality_value"]

    message = health.alert_message()
    assert message.startswith("DATA-DEGRADED: earnings_drift, quality_value")
    assert "NO new entries" in message and "exits still run" in message


def test_fresh_caches_degrade_nothing_and_send_no_alert(tmp_path):
    write_cache_document(tmp_path / "fundamentals.json",
                         _fundamentals({"AAA": "2026-06-30"}), fetched_at=NOW)
    write_cache_document(tmp_path / "earnings.json",
                         _earnings(["2026-07-20"]), fetched_at=NOW)
    health = assess_data_health(load_data_caches(tmp_path), as_of=NOW)
    assert health.healthy
    assert health.degraded_sleeves() == {}
    assert health.alert_message() is None


def test_one_stale_cache_degrades_only_its_sleeve(tmp_path):
    write_cache_document(tmp_path / "fundamentals.json",
                         _fundamentals({"AAA": "2026-06-30"}), fetched_at=NOW)
    health = assess_data_health(load_data_caches(tmp_path), as_of=NOW)
    assert set(health.degraded_sleeves()) == {"earnings_drift"}


def test_thresholds_come_from_config(tmp_path):
    write_cache_document(tmp_path / "fundamentals.json",
                         _fundamentals({"AAA": "2026-06-30"}),
                         fetched_at=NOW - timedelta(days=20))
    write_cache_document(tmp_path / "earnings.json", _earnings(["2026-07-20"]),
                         fetched_at=NOW - timedelta(days=5))
    caches = load_data_caches(tmp_path)
    assert not assess_data_health(caches, as_of=NOW).healthy
    lenient = DataConfig(
        fundamentals={"max_fetch_age_days": 30},
        earnings={"max_fetch_age_days": 7},
    )
    assert assess_data_health(caches, as_of=NOW, config=lenient).healthy


def test_the_shipped_thresholds_are_the_documented_ones():
    cfg = load_config(str(Path(REPO_ROOT) / "config/default.yaml")).data
    assert cfg.fundamentals.max_fetch_age_days == 14
    assert cfg.fundamentals.max_period_age_days == 167
    assert cfg.fundamentals.max_stale_ticker_fraction == pytest.approx(0.2)
    assert cfg.earnings.max_fetch_age_days == 2
