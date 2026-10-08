"""KAN-110 — the Alpha Vantage earnings refresh.

Every response here is a recorded Alpha Vantage body (tests/scripts/fixtures/
alphavantage, trimmed from the 2026-10-08 probe) or a synthetic one built in the
same shape. No test makes a network call: the client takes a transport, and
these tests hand it a fake one.

The properties pinned, in the order the ticket asks for them:

* parsing: only reported quarters become rows, in the shape the lookup reads;
* the surprise is recomputed from reportedEPS/estimatedEPS of the SAME row
  (Alpha Vantage's adjusted basis on both sides), never taken from
  ``surprisePercentage``, and is ``None`` without a usable estimate;
* the calendar file, and the ticker mapping both ways (``BRK B`` <-> ``BRK-B`` /
  ``BRK.B``, ``MMC`` -> ``MRSH``);
* merging never drops history, and writes are atomic;
* the budget, the rate-limit stop, and what each does to ``fetched_at`` — the
  contract with KAN-109's freshness check;
* the key never escapes, not even through an exception that carries the URL.
"""

from __future__ import annotations

import json
import threading
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts import fetch_earnings as fe
from shared.data_cache import (
    EARNINGS_CALENDAR_FILE,
    EARNINGS_FILE,
    EARNINGS_STATE_FILE,
    assess_earnings,
    read_cache_document,
    write_cache_document,
    write_json_atomic,
)

FIXTURES = Path(__file__).parent / "fixtures" / "alphavantage"
KEY = "SECRETAVKEY0042"
# 2026-10-08 13:00 UTC = 09:00 EDT: the US session date is 2026-10-08.
NOW = datetime(2026, 10, 8, 13, 0, tzinfo=timezone.utc)


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _quarter(reported, actual, estimate, *, fiscal=None, timing="pre-market", pct="999"):
    return {
        "fiscalDateEnding": fiscal or reported,
        "reportedDate": reported,
        "reportedEPS": actual,
        "estimatedEPS": estimate,
        "surprise": "0",
        "surprisePercentage": pct,
        "reportTime": timing,
    }


def _earnings_body(symbol: str, quarters: list[dict]) -> bytes:
    return json.dumps({"symbol": symbol, "quarterlyEarnings": quarters}).encode()


def _calendar_body(rows: list[tuple[str, str, str]]) -> bytes:
    lines = ["symbol,name,reportDate,fiscalDateEnding,estimate,currency,timeOfTheDay"]
    for symbol, report, timing in rows:
        lines.append(f"{symbol},X,{report},2026-09-30,1.0,USD,{timing}")
    return ("\n".join(lines) + "\n").encode()


def _default_history(symbol: str) -> list[dict]:
    return [
        _quarter("2026-07-20", "2.00", "1.90", fiscal="2026-06-30"),
        _quarter("2026-04-20", "1.80", "1.75", fiscal="2026-03-31"),
    ]


class FakeAV:
    """A transport that answers like Alpha Vantage and records every request."""

    def __init__(self, *, calendar: bytes | None = None, earnings: dict | None = None,
                 limit_after: int | None = None, raise_on: dict | None = None):
        self.calendar = calendar if calendar is not None else _calendar_body([])
        self.earnings = earnings or {}
        self.limit_after = limit_after
        self.raise_on = raise_on or {}
        self.requests: list[dict] = []

    def __call__(self, url: str, timeout: float) -> bytes:
        params = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        assert params.get("apikey") == KEY
        self.requests.append(params)
        if self.limit_after is not None and len(self.requests) > self.limit_after:
            return json.dumps({"Note": "Thank you for using Alpha Vantage! Our standard API "
                               "rate limit is 25 requests per day."}).encode()
        if params["function"] == "EARNINGS_CALENDAR":
            return self.calendar
        symbol = params["symbol"]
        if symbol in self.raise_on:
            raise self.raise_on[symbol](url)
        if symbol in self.earnings:
            body = self.earnings[symbol]
            return body if isinstance(body, bytes) else _earnings_body(symbol, body)
        return _earnings_body(symbol, _default_history(symbol))

    def symbols(self) -> list[str]:
        return [r["symbol"] for r in self.requests if r["function"] == "EARNINGS"]


def _client(transport, sleep=None) -> fe.AlphaVantageClient:
    return fe.AlphaVantageClient(
        KEY, transport=transport, min_interval_seconds=0, sleep=sleep or (lambda s: None)
    )


def _run(tmp_path, av, *, live, universe=None, budget=20, now=NOW, **kw):
    return fe.run_refresh(
        client=_client(av),
        cache_dir=tmp_path,
        live=live,
        universe=universe or list(live),
        budget=budget,
        symbol_overrides={"MMC": "MRSH", "FI": "FISV"},
        now=lambda: now,
        log=lambda line: None,
        **kw,
    )


def _doc(tmp_path):
    return read_cache_document(tmp_path / EARNINGS_FILE)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_recorded_brk_b_payload_parses_to_the_lookup_row_shape():
    payload = json.loads(_fixture("earnings_BRK-B.json"))
    rows = fe.parse_quarterly_earnings(payload)
    latest = rows[-1]
    assert latest == {
        "earnings_date": "2026-08-08",
        "actual_eps": 6.0255,
        "estimate_eps": 5.05,
        "surprise_pct": 19.32,
        "timing": "amc",
        "fiscal_period": "2026-06-30",
    }
    assert [r["earnings_date"] for r in rows] == sorted(r["earnings_date"] for r in rows)
    lookup = fe.build_earnings_lookup({"BRK B": rows}, window_days=2)
    assert lookup("BRK B", date(2026, 8, 10))["surprise_pct"] == 19.32
    assert lookup("BRK B", date(2026, 8, 11)) is None


def test_surprise_is_recomputed_from_both_eps_fields_not_taken_from_av():
    """surprisePercentage is ignored: the rule is ours, applied to the two
    numbers Alpha Vantage reports on the same (adjusted) basis."""
    rows = fe.parse_quarterly_earnings({"quarterlyEarnings": [
        _quarter("2026-07-20", "1.10", "1.00", pct="55.5"),
        _quarter("2026-04-20", "-0.50", "-1.00", pct="0"),
    ]})
    by_date = {r["earnings_date"]: r for r in rows}
    assert by_date["2026-07-20"]["surprise_pct"] == 10.0
    # Negative estimate: divided by |estimate|, so beating a loss is positive.
    assert by_date["2026-04-20"]["surprise_pct"] == 50.0
    assert by_date["2026-07-20"]["actual_eps"] == 1.1
    assert by_date["2026-07-20"]["estimate_eps"] == 1.0


@pytest.mark.parametrize("estimate", ["None", "0", "0.0", "", "-"])
def test_no_usable_estimate_means_no_surprise_not_zero(estimate):
    """Zero is a real in-line result; the yfinance fetcher wrote 0.0 for
    "unknown", which read as one."""
    rows = fe.parse_quarterly_earnings({"quarterlyEarnings": [
        _quarter("2026-07-20", "1.10", estimate)]})
    assert rows[0]["surprise_pct"] is None


def test_the_recorded_no_estimate_quarter_has_no_surprise():
    rows = fe.parse_quarterly_earnings(json.loads(_fixture("earnings_BRK-B.json")))
    old = [r for r in rows if r["estimate_eps"] is None]
    assert old and all(r["surprise_pct"] is None for r in old)


def test_only_reported_quarters_become_rows():
    rows = fe.parse_quarterly_earnings({"quarterlyEarnings": [
        _quarter("2026-11-06", "None", "5.66", fiscal="2026-09-30"),  # scheduled
        _quarter("2026-08-08", "6.02", "5.05", fiscal="2026-06-30"),
        _quarter("not-a-date", "1.0", "1.0", fiscal="2026-03-31"),
    ]})
    assert [r["fiscal_period"] for r in rows] == ["2026-06-30"]


def test_report_time_maps_to_bmo_amc():
    rows = fe.parse_quarterly_earnings({"quarterlyEarnings": [
        _quarter("2026-07-20", "1", "1", timing="pre-market"),
        _quarter("2026-04-20", "1", "1", timing="post-market"),
        _quarter("2026-01-20", "1", "1", timing=""),
    ]})
    assert [r["timing"] for r in rows] == [None, "amc", "bmo"]


def test_recorded_calendar_parses_and_maps_back_to_universe_tickers():
    entries = fe.parse_calendar_csv(_fixture("earnings_calendar_3month.csv").decode())
    reverse = fe.reverse_symbols(["BRK B", "MMC", "FI", "AAPL"], {"MMC": "MRSH", "FI": "FISV"})
    by_ticker = {fe.calendar_ticker(e.symbol, reverse): e for e in entries}
    assert by_ticker["BRK B"].report_date == "2026-11-06"
    assert by_ticker["BRK B"].estimate_eps == 5.66
    assert by_ticker["MMC"].timing == "bmo"            # MRSH, pre-market
    assert by_ticker["FI"].report_date == "2026-11-04"  # FISV
    assert by_ticker["UA"].estimate_eps is None         # blank estimate
    assert by_ticker["BRK A"].symbol == "BRK.A"         # not in the universe


def test_a_calendar_without_the_documented_header_is_malformed():
    with pytest.raises(fe.AlphaVantageError) as info:
        fe.parse_calendar_csv("foo,bar\n1,2\n")
    assert info.value.kind == "malformed"


# ---------------------------------------------------------------------------
# Ticker mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ticker, expected",
    [("BRK B", "BRK-B"), ("BF B", "BF-B"), ("AAPL", "AAPL"), ("MMC", "MRSH"), ("FI", "FISV")],
)
def test_universe_tickers_map_to_alpha_vantage_symbols(ticker, expected):
    assert fe.av_symbol(ticker, {"MMC": "MRSH", "FI": "FISV"}) == expected


def test_the_shipped_config_carries_the_verified_renames():
    from shared.config import load_config

    cfg = load_config("config/default.yaml").data.earnings.refresh
    assert cfg.symbol_overrides == {"MMC": "MRSH", "FI": "FISV"}
    # Free tier: one calendar call + the budget must leave headroom under 25/day.
    assert cfg.calls_per_run + 1 <= 22
    assert cfg.min_interval_seconds >= 12


# ---------------------------------------------------------------------------
# Merging and atomic writes
# ---------------------------------------------------------------------------


def test_merge_replaces_a_fiscal_period_and_never_drops_older_rows():
    old = [
        {"earnings_date": "2010-01-20", "actual_eps": 1.0, "estimate_eps": 1.0,
         "surprise_pct": 0.0, "timing": None, "fiscal_period": "2009-12-31"},
        {"earnings_date": "2026-07-19", "actual_eps": 2.0, "estimate_eps": 1.9,
         "surprise_pct": 5.26, "timing": None, "fiscal_period": "2026-06-30"},
    ]
    new = [{"earnings_date": "2026-07-20", "actual_eps": 2.1, "estimate_eps": 1.9,
            "surprise_pct": 10.53, "timing": "bmo", "fiscal_period": "2026-06-30"}]
    merged = fe.merge_ticker_rows(old, new)
    assert [r["fiscal_period"] for r in merged] == ["2009-12-31", "2026-06-30"]
    assert merged[-1]["actual_eps"] == 2.1


def test_legacy_rows_are_replaced_on_first_fetch_so_bases_never_mix():
    legacy = [{"earnings_date": "2025-10-29", "actual_eps": -7.0, "estimate_eps": 6.7,
               "surprise_pct": -204.0}]  # yfinance: GAAP actual vs adjusted estimate
    new = fe.parse_quarterly_earnings({"quarterlyEarnings": [
        _quarter("2025-10-29", "7.25", "6.70", fiscal="2025-09-30")]})
    merged = fe.merge_ticker_rows(legacy, new)
    assert merged == new
    # ...but a ticker Alpha Vantage returned nothing for keeps what it had.
    assert fe.merge_ticker_rows(legacy, []) == legacy


def test_atomic_write_leaves_the_old_file_on_failure(tmp_path):
    path = tmp_path / "earnings.json"
    write_json_atomic(path, {"good": True})
    with pytest.raises(TypeError):
        write_json_atomic(path, {"bad": object()})
    assert json.loads(path.read_text()) == {"good": True}
    assert [p.name for p in tmp_path.iterdir()] == ["earnings.json"], "temp file left behind"


def test_write_cache_document_can_record_an_unknown_fetch_time(tmp_path):
    path = tmp_path / "e.json"
    write_cache_document(path, {"A": []}, fetch_time_unknown=True)
    doc = read_cache_document(path)
    assert doc.exists and doc.fetched_at is None
    assert json.loads(path.read_text())["fetched_at"] is None


# ---------------------------------------------------------------------------
# The client: errors, spacing, the key
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field", ["Note", "Information"])
def test_quota_notices_stop_the_run(field):
    body = json.dumps({field: f"limit reached for apikey={KEY}"}).encode()
    client = _client(lambda url, t: body)
    with pytest.raises(fe.AlphaVantageError) as info:
        client.earnings("AAPL")
    assert info.value.kind == "limit" and info.value.stops_run
    assert KEY not in str(info.value)


def test_error_message_is_a_refusal_of_that_request_only():
    body = json.dumps({"Error Message": "Invalid API call."}).encode()
    with pytest.raises(fe.AlphaVantageError) as info:
        _client(lambda url, t: body).earnings("ZZZZ")
    assert info.value.kind == "refused" and not info.value.stops_run


def test_an_unknown_symbol_is_no_data_not_an_error():
    """Recorded 2026-10-08: EARNINGS for MMC (renamed MRSH) returns {}."""
    assert _client(lambda url, t: b"{}").earnings("MMC") is None


def test_the_calendar_rejects_a_json_notice_in_place_of_csv():
    body = json.dumps({"Information": "rate limit"}).encode()
    with pytest.raises(fe.AlphaVantageError) as info:
        _client(lambda url, t: body).calendar()
    assert info.value.kind == "limit"


def test_requests_are_spaced_by_the_minimum_interval():
    slept: list[float] = []
    # monotonic(): after request 1 -> 0.0; before request 2 -> 1.0; after it -> 1.0
    clock = iter([0.0, 1.0, 1.0])
    client = fe.AlphaVantageClient(
        KEY, transport=lambda url, t: b"{}", min_interval_seconds=13.0,
        sleep=slept.append, monotonic=lambda: next(clock),
    )
    client.earnings("A")
    client.earnings("B")
    assert slept and abs(slept[0] - 12.0) < 1e-9
    assert client.calls == 2


def test_a_transport_error_carrying_the_url_is_scrubbed():
    """urllib's HTTPError and friends carry the full URL — and so the key."""

    def boom(url, timeout):
        raise OSError(f"HTTP Error 500 for {url}")

    with pytest.raises(fe.AlphaVantageError) as info:
        _client(boom).earnings("AAPL")
    err = info.value
    assert err.kind == "transport"
    assert KEY not in str(err) and "apikey=***" in str(err)
    # Not chained: a traceback would otherwise print the original message.
    assert err.__cause__ is None and err.__suppress_context__


def test_scrub_masks_the_literal_key_and_any_apikey_parameter():
    text = f"https://x/query?function=EARNINGS&apikey={KEY}&symbol=A and {KEY}"
    out = fe.scrub(text, KEY)
    assert KEY not in out
    assert fe.scrub("apikey=OTHERKEY&x=1") == "apikey=***&x=1"


# ---------------------------------------------------------------------------
# fetched_at vs KAN-109's freshness check
# ---------------------------------------------------------------------------


def test_a_full_successful_run_is_current_and_reads_fresh(tmp_path):
    av = FakeAV()
    result = _run(tmp_path, av, live=["AAPL", "BRK B", "MMC"])
    assert result.status == "CURRENT" and result.exit_code == 0
    doc = _doc(tmp_path)
    assert doc.fetched_at == NOW and doc.source == "alphavantage"
    assert set(doc.data) == {"AAPL", "BRK B", "MMC"}
    assert av.symbols() == ["AAPL", "BRK-B", "MRSH"]
    verdict = assess_earnings(doc, as_of=NOW + timedelta(hours=1))
    assert verdict.fresh, verdict.describe()
    state = json.loads((tmp_path / EARNINGS_STATE_FILE).read_text())
    assert state["last_current_at"] == NOW.isoformat()


def test_an_empty_cache_backfilled_over_budget_stays_unstamped_until_complete(tmp_path):
    """Days one to four of the first backfill: rows saved, cache NOT fresh."""
    live = ["A", "B", "C", "D", "E"]
    result = _run(tmp_path, FakeAV(), live=live, budget=2)
    assert result.status == "INCOMPLETE" and result.exit_code == 3
    doc = _doc(tmp_path)
    assert set(doc.data) == {"A", "B"}
    assert doc.fetched_at is None
    verdict = assess_earnings(doc, as_of=NOW)
    assert not verdict.fresh and "fetch time unknown" in verdict.describe()

    later = NOW + timedelta(days=1)
    result = _run(tmp_path, FakeAV(), live=live, budget=2, now=later)
    assert result.status == "INCOMPLETE" and _doc(tmp_path).fetched_at is None
    final = NOW + timedelta(days=2)
    av = FakeAV()
    result = _run(tmp_path, av, live=live, budget=2, now=final)
    assert result.status == "CURRENT"
    assert av.symbols()[0] == "E", "the never-fetched ticker must come first"
    assert _doc(tmp_path).fetched_at == final


def test_a_partial_run_leaves_a_good_stamp_where_it_was(tmp_path):
    """A rate limit mid-run saves progress but never advances fetched_at."""
    live = ["A", "B", "C"]
    _run(tmp_path, FakeAV(), live=live)
    stamped = _doc(tmp_path).fetched_at
    assert stamped == NOW

    # Next day C reported; the quota is gone after the calendar call.
    later = NOW + timedelta(days=1)
    cal = _calendar_body([("C", "2026-10-08", "pre-market")])
    av = FakeAV(calendar=cal, limit_after=1)
    result = _run(tmp_path, av, live=live, now=later)
    assert result.status == "FAILED" and result.exit_code == 2
    assert "limit" in result.stopped
    doc = _doc(tmp_path)
    assert doc.fetched_at == stamped
    assert set(doc.data) == {"A", "B", "C"}, "a failed run must not truncate the cache"
    assert assess_earnings(doc, as_of=later).fresh  # still inside 2 days
    assert not assess_earnings(doc, as_of=NOW + timedelta(days=2, hours=1)).fresh


def test_a_failed_calendar_call_changes_nothing(tmp_path):
    _run(tmp_path, FakeAV(), live=["A"])
    before = (tmp_path / EARNINGS_FILE).read_bytes()
    av = FakeAV(limit_after=0)
    result = _run(tmp_path, av, live=["A"], now=NOW + timedelta(days=1))
    assert result.exit_code == 2 and av.symbols() == []
    assert (tmp_path / EARNINGS_FILE).read_bytes() == before


def test_recent_reporters_come_first_and_gate_the_stamp(tmp_path):
    live = ["A", "B", "C"]
    _run(tmp_path, FakeAV(), live=live)  # all fetched on day 0

    # Day 2: B reported yesterday (dropped off the forward calendar since, but
    # retained from the day-1 calendar), C reports today pre-market.
    day1 = NOW + timedelta(days=1)
    _run(tmp_path, FakeAV(calendar=_calendar_body([("B", "2026-10-09", "post-market"),
                                                    ("C", "2026-10-10", "pre-market")])),
         live=live, budget=0, now=day1)
    day2 = NOW + timedelta(days=2)
    fresh_rows = {
        "B": [_quarter("2026-10-09", "3.0", "2.5", fiscal="2026-09-30", timing="post-market")],
        "C": [_quarter("2026-10-10", "1.0", "1.2", fiscal="2026-09-30")],
    }
    cal_day2 = _calendar_body([("C", "2026-10-10", "pre-market")])

    starved = _run(tmp_path, FakeAV(calendar=cal_day2, earnings=fresh_rows),
                   live=live, budget=1, now=day2)
    assert starved.status == "INCOMPLETE"
    assert starved.plan.due_live == ["B", "C"]
    assert starved.critical_missed == ["C"]
    assert _doc(tmp_path).fetched_at == NOW

    av = FakeAV(calendar=cal_day2, earnings=fresh_rows)
    result = _run(tmp_path, av, live=live, budget=1, now=day2 + timedelta(hours=1))
    assert result.status == "CURRENT"
    assert av.symbols() == ["C"], "B was captured by the starved run and is not due again"
    doc = _doc(tmp_path)
    assert doc.fetched_at == day2 + timedelta(hours=1)
    assert doc.data["C"][-1]["earnings_date"] == "2026-10-10"
    assert doc.data["A"], "untouched tickers keep their rows"


def test_a_reporter_asked_after_its_report_but_without_an_actual_is_current(tmp_path):
    """Alpha Vantage had not published yet: asked, so the stamp may advance;
    asked again on the next daily run, which is past refetch_after_hours."""
    live = ["A"]
    _run(tmp_path, FakeAV(), live=live)
    cal = _calendar_body([("A", "2026-10-09", "pre-market")])
    day1 = NOW + timedelta(days=1)
    result = _run(tmp_path, FakeAV(calendar=cal), live=live, now=day1)
    assert result.status == "CURRENT" and result.fetched == ["A"]
    # Same day, an hour later: asked since the report, so not due again.
    av = FakeAV(calendar=cal)
    again = _run(tmp_path, av, live=live, budget=0, now=day1 + timedelta(hours=1))
    assert again.status == "CURRENT" and av.symbols() == []
    # Next day: due again, because the actual is still missing.
    av = FakeAV(calendar=cal)
    _run(tmp_path, av, live=live, budget=5, now=day1 + timedelta(days=1))
    assert av.symbols() == ["A"]


def test_pit_backfill_uses_spare_budget_and_never_blocks_the_stamp(tmp_path):
    live = ["A", "B"]
    universe = live + [f"P{i}" for i in range(10)]
    av = FakeAV()
    result = _run(tmp_path, av, live=live, universe=universe, budget=4)
    assert result.status == "CURRENT"
    assert av.symbols() == ["A", "B", "P0", "P1"]
    assert result.backfill_remaining == 8
    assert _doc(tmp_path).fetched_at == NOW


def test_a_rate_limit_during_backfill_still_stamps_a_current_live_universe(tmp_path):
    live = ["A", "B"]
    universe = live + ["P0", "P1", "P2"]
    av = FakeAV(limit_after=4)  # calendar + A + B + P0, then the quota
    result = _run(tmp_path, av, live=live, universe=universe, budget=10)
    assert result.status == "CURRENT" and result.exit_code == 0
    assert result.stopped and "limit" in result.stopped
    assert _doc(tmp_path).fetched_at == NOW


def test_a_live_ticker_with_no_data_is_reported(tmp_path):
    result = _run(tmp_path, FakeAV(earnings={"ZZZ": b"{}"}), live=["A", "ZZZ"])
    assert result.status == "CURRENT"
    assert any(m.startswith("LIVE_NO_DATA: ") and "ZZZ" in m for m in result.messages)


def test_a_refused_live_symbol_fails_the_run(tmp_path):
    body = json.dumps({"Error Message": "Invalid API call."}).encode()
    result = _run(tmp_path, FakeAV(earnings={"BAD": body}), live=["A", "BAD"])
    assert result.status == "FAILED" and result.refused == ["BAD"]
    assert _doc(tmp_path).fetched_at is None


def test_termination_mid_run_saves_progress(tmp_path):
    def terminate(url):
        raise fe.Terminated()

    av = FakeAV(raise_on={"B": terminate})
    result = _run(tmp_path, av, live=["A", "B", "C"])
    assert result.exit_code == 2 and "terminated" in result.stopped
    assert set(_doc(tmp_path).data) == {"A"}
    assert _doc(tmp_path).fetched_at is None


def test_an_unreadable_cache_is_never_overwritten(tmp_path):
    path = tmp_path / EARNINGS_FILE
    path.write_text("{ not json")
    with pytest.raises(RuntimeError, match="refusing to overwrite"):
        _run(tmp_path, FakeAV(), live=["A"])
    assert path.read_text() == "{ not json"


def test_a_legacy_bare_cache_is_upgraded_ticker_by_ticker(tmp_path):
    legacy = {"A": [{"earnings_date": "2025-01-01", "actual_eps": 1.0,
                     "estimate_eps": 0.0, "surprise_pct": 0.0}],
              "Z": [{"earnings_date": "2025-01-01", "actual_eps": 1.0,
                     "estimate_eps": 1.0, "surprise_pct": 0.0}]}
    (tmp_path / EARNINGS_FILE).write_text(json.dumps(legacy))
    result = _run(tmp_path, FakeAV(), live=["A"])
    assert result.status == "CURRENT"
    doc = _doc(tmp_path)
    assert all("fiscal_period" in r for r in doc.data["A"])
    assert doc.data["Z"] == legacy["Z"], "rows not yet re-fetched are kept"


def test_the_calendar_file_keeps_recent_past_entries(tmp_path):
    cal = _calendar_body([("A", "2026-10-09", "pre-market"), ("BRK.B", "2026-11-06", "")])
    _run(tmp_path, FakeAV(calendar=cal), live=["A", "BRK B"])
    doc = read_cache_document(tmp_path / EARNINGS_CALENDAR_FILE)
    assert doc.fetched_at is not None and doc.source == "alphavantage"
    assert doc.data["BRK B"][0]["symbol"] == "BRK.B"

    # 20 days later the forward calendar no longer lists either; the A entry
    # is past the 14-day retention and goes, nothing else is invented.
    later = NOW + timedelta(days=20)
    _run(tmp_path, FakeAV(calendar=_calendar_body([])), live=["A", "BRK B"], now=later)
    doc = read_cache_document(tmp_path / EARNINGS_CALENDAR_FILE)
    assert "A" not in doc.data
    assert doc.data["BRK B"][0]["report_date"] == "2026-11-06"


# ---------------------------------------------------------------------------
# The CLI
# ---------------------------------------------------------------------------


def test_main_without_a_key_exits_1_and_makes_no_request(tmp_path, capsys):
    calls: list[str] = []
    missing = tmp_path / "security"
    missing.write_text("#!/bin/bash\nexit 44\n")
    missing.chmod(0o755)
    rc = fe.main(["--tickers", "A", "--cache-dir", str(tmp_path)],
                 transport=lambda url, t: calls.append(url) or b"",
                 env={"ALGO_SECURITY_BIN": str(missing)})
    assert rc == 1 and calls == []
    assert "status=ERROR" in capsys.readouterr().out


def test_main_reads_the_key_from_the_keychain_and_never_prints_it(tmp_path, capsys):
    (tmp_path / "bin").mkdir()
    security = tmp_path / "bin" / "security"
    security.write_text(f"#!/bin/bash\necho '{KEY}'\n")
    security.chmod(0o755)
    cache = tmp_path / "cache"

    def flaky(url, timeout):
        if "EARNINGS_CALENDAR" in url:
            return _calendar_body([])
        raise OSError(f"connection reset fetching {url}")

    rc = fe.main(["--tickers", "A", "--cache-dir", str(cache), "--min-interval", "0"],
                 transport=flaky, env={"ALGO_SECURITY_BIN": str(security)})
    out = capsys.readouterr()
    assert rc == 2
    assert KEY not in out.out and KEY not in out.err
    assert "apikey=***" in out.out
    for path in cache.iterdir():
        if path.is_file():
            assert KEY not in path.read_text(errors="replace"), path.name


def test_main_crash_output_is_scrubbed(tmp_path, capsys, monkeypatch):
    def explode(**kwargs):
        raise ValueError(f"bad response from https://x/query?apikey={KEY}")

    monkeypatch.setattr(fe, "run_refresh", explode)
    rc = fe.main(["--tickers", "A", "--cache-dir", str(tmp_path)],
                 transport=lambda url, t: b"", env={fe.API_KEY_VAR: KEY})
    out = capsys.readouterr()
    assert rc == 1
    assert KEY not in out.out + out.err


def test_main_dry_run_makes_no_request(tmp_path, capsys):
    rc = fe.main(["--tickers", "A,B", "--cache-dir", str(tmp_path), "--dry-run"],
                 transport=lambda url, t: pytest.fail("dry run made a request"),
                 env={fe.API_KEY_VAR: KEY})
    assert rc == 0
    assert "would fetch: ['A', 'B']" in capsys.readouterr().out


def test_a_second_concurrent_refresh_is_refused(tmp_path):
    with fe._CacheLock(tmp_path):
        with pytest.raises(RuntimeError, match="another earnings refresh"):
            with fe._CacheLock(tmp_path):
                pass


def test_the_live_universe_is_the_earnings_drift_sleeve():
    from shared.universe import SP500_TOP100, UNIVERSE_REGISTRY

    assert fe.live_universe() == list(UNIVERSE_REGISTRY["earnings_drift"]) == SP500_TOP100


def test_the_pit_universe_puts_live_first_and_covers_every_member():
    from shared.universe import MEMBERSHIP_SNAPSHOT_PATH, MembershipCalendar

    live = fe.live_universe()
    pit = fe.pit_universe(live)
    assert pit[: len(live)] == live
    assert len(pit) == len(set(pit))
    members = set(MembershipCalendar.from_json_file(str(MEMBERSHIP_SNAPSHOT_PATH)).all_tickers())
    assert members <= set(pit)


def test_a_live_ticker_the_calendar_never_flagged_is_re_asked_weekly_when_overdue(tmp_path):
    """Its newest actual is from July; the calendar never listed its October
    report. The PIT backfill must not starve it."""
    live = ["A"]
    universe = live + [f"P{i}" for i in range(5)]
    _run(tmp_path, FakeAV(), live=live, universe=universe, budget=1)  # A only
    # Day 13 (10-21): A's newest actual (07-20) is 93 days old, not overdue,
    # so the spare call goes to the backfill.
    av = FakeAV()
    _run(tmp_path, av, live=live, universe=universe, budget=1, now=NOW + timedelta(days=13))
    assert av.symbols() == ["P0"]
    # Day 21 (10-29): 101 days old and last asked 3 weeks ago: A jumps the backfill.
    av = FakeAV()
    _run(tmp_path, av, live=live, universe=universe, budget=1, now=NOW + timedelta(days=21))
    assert av.symbols() == ["A"]
    # Day 24: still no newer actual, but asked 3 days ago: back to the backfill.
    av = FakeAV()
    _run(tmp_path, av, live=live, universe=universe, budget=1, now=NOW + timedelta(days=24))
    assert av.symbols() == ["P1"]

def test_the_recorded_mrsh_payload_serves_mmc():
    """MMC was renamed MRSH; Alpha Vantage keeps the history under MRSH."""
    rows = fe.parse_quarterly_earnings(json.loads(_fixture("earnings_MRSH.json")))
    assert rows[-1]["earnings_date"] == "2026-07-21"
    assert rows[-1]["timing"] == "bmo"
    assert rows[-1]["surprise_pct"] == 2.78
