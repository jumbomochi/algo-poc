"""KAN-110 — the Alpha Vantage fundamentals refresh.

The GE and ATVI bodies are recorded Alpha Vantage responses
(tests/scripts/fixtures/alphavantage, trimmed from the 2026-10-16 probe); the
rest are synthetic, in the same shape. No test makes a network call.

Pinned:

* parsing: both statements per quarter, the ratios on the quarterly basis the
  yfinance fetcher used, so quality_value's scores keep their meaning;
* as first seen: a quarter is stored only inside the capture window and is
  never overwritten, so a later restatement cannot reach the cache;
* the plan's order and the budget (two requests a ticker);
* what each outcome does to ``fetched_at`` — the contract with KAN-109;
* the shared lock with the earnings refresh, and the key never escaping;
* SHALLOW: a backtest or shadow that starts before the history does is
  degraded, not graded on empty lookups.
"""

from __future__ import annotations

import json
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts import fetch_earnings as fe
from scripts import fetch_fundamentals as ff
from shared.data_cache import (
    EARNINGS_FILE,
    FUNDAMENTALS_FILE,
    FUNDAMENTALS_STATE_FILE,
    assess_fundamentals,
    read_cache_document,
    write_cache_document,
)
from shared.universe import SECTOR_MAP, UNIVERSE_REGISTRY

FIXTURES = Path(__file__).parent / "fixtures" / "alphavantage"
KEY = "SECRETAVKEY0042"
NOW = datetime(2026, 10, 17, 1, 0, tzinfo=timezone.utc)


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _statements(symbol: str, quarters: dict[str, dict]) -> tuple[bytes, bytes]:
    """Synthetic INCOME_STATEMENT / BALANCE_SHEET bodies, one quarter per key."""
    income, balance = [], []
    for period, f in sorted(quarters.items(), reverse=True):
        income.append({"fiscalDateEnding": period, "reportedCurrency": "USD",
                       "netIncome": str(f.get("ni", 100)), "totalRevenue": str(f.get("rev", 1000))})
        balance.append({"fiscalDateEnding": period, "reportedCurrency": "USD",
                        "totalShareholderEquity": str(f.get("eq", 500)),
                        "shortLongTermDebtTotal": str(f.get("debt", 250))})
    return (
        json.dumps({"symbol": symbol, "quarterlyReports": income}).encode(),
        json.dumps({"symbol": symbol, "quarterlyReports": balance}).encode(),
    )


class FakeAV:
    """A transport answering (function, symbol) from a table; records each URL."""

    def __init__(self, table: dict | None = None, default=None):
        self.table = dict(table or {})
        self.default = default
        self.urls: list[str] = []

    def add(self, symbol: str, quarters: dict[str, dict]) -> None:
        income, balance = _statements(symbol, quarters)
        self.table[("INCOME_STATEMENT", symbol)] = income
        self.table[("BALANCE_SHEET", symbol)] = balance

    def requested(self) -> list[tuple[str, str]]:
        out = []
        for url in self.urls:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            out.append((q["function"][0], q["symbol"][0]))
        return out

    def __call__(self, url: str, timeout: float) -> bytes:
        self.urls.append(url)
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        answer = self.table.get((q["function"][0], q["symbol"][0]), self.default)
        if answer is None:
            return b"{}"
        if isinstance(answer, Exception):
            raise answer
        return answer(url) if callable(answer) else answer


def _client(transport) -> fe.AlphaVantageClient:
    return fe.AlphaVantageClient(KEY, transport=transport, min_interval_seconds=0,
                                 sleep=lambda s: None)


def _run(tmp_path, transport, live, *, budget=6, now=NOW, **kw):
    return ff.run_fundamentals_refresh(
        client=_client(transport), cache_dir=tmp_path, live=live, budget=budget,
        now=lambda: now, log=lambda m: None, termination=fe._Termination(), **kw,
    )


def _cache(tmp_path):
    return read_cache_document(tmp_path / FUNDAMENTALS_FILE)


# --- parsing ---------------------------------------------------------------


def test_the_recorded_ge_statements_parse_into_the_lookup_shape():
    rows = ff.parse_statements(
        "GE",
        json.loads(_fixture("income_statement_GE.json")),
        json.loads(_fixture("balance_sheet_GE.json")),
    )
    assert [r["report_date"] for r in rows] == [
        "2024-03-31", "2025-12-31", "2026-03-31", "2026-06-30"
    ]
    newest = rows[-1]
    assert newest["net_income"] == 2_370_000_000
    assert newest["total_equity"] == 17_640_000_000
    assert newest["total_debt"] == 19_157_000_000
    # The quarterly basis the yfinance fetcher used: one quarter's net income.
    assert newest["roe"] == round(2.37 / 17.64, 4)
    assert newest["debt_equity"] == round(19.157 / 17.64, 4)
    assert newest["profit_margin"] == round(2.37 / 13.349, 4)
    assert newest["sector"] == SECTOR_MAP.get("GE", "Unknown")
    assert newest["source"] == "alphavantage"
    assert newest["reported_currency"] == "USD"


def test_alpha_vantages_literal_none_is_not_a_currency():
    assert ff._text("None") is None and ff._text(" USD ") == "USD" and ff._text(None) is None


def test_a_quarter_needs_both_statements_and_its_core_figures():
    income, balance = _statements("X", {"2026-06-30": {}, "2026-03-31": {}})
    inc, bal = json.loads(income), json.loads(balance)
    bal["quarterlyReports"] = bal["quarterlyReports"][:1]  # 2026-03-31 has no balance sheet
    inc["quarterlyReports"][0]["netIncome"] = "None"       # 2026-06-30 has no net income
    assert ff.parse_statements("X", inc, bal) == []


def test_debt_falls_back_to_short_plus_long_and_none_reads_as_no_debt():
    assert ff._total_debt({"shortLongTermDebtTotal": "None", "shortTermDebt": "10",
                           "longTermDebt": "90"}) == 100
    assert ff._total_debt({"shortLongTermDebtTotal": "None", "currentDebt": "5",
                           "shortTermDebt": "None", "longTermDebt": "None"}) == 5
    assert ff._total_debt({"shortLongTermDebtTotal": "None"}) == 0.0


# --- as first seen -----------------------------------------------------------


def test_only_quarters_inside_the_capture_window_are_stored():
    rows = ff.parse_statements(
        "GE",
        json.loads(_fixture("income_statement_GE.json")),
        json.loads(_fixture("balance_sheet_GE.json")),
    )
    merged, added = ff.merge_first_seen(None, rows, seen_at=NOW, capture_window_days=135)
    # 2026-06-30 is 109 days old on 10-17; 2026-03-31 (200) and the recast
    # 2024-03-31 would be numbers nobody had then.
    assert added == ["2026-06-30"]
    assert [r["report_date"] for r in merged] == ["2026-06-30"]
    assert merged[0]["first_seen_at"] == NOW.isoformat()


def test_a_stored_quarter_is_never_overwritten_by_a_restatement():
    first, _ = ff.merge_first_seen(
        None, ff.parse_statements("X", *map(json.loads, _statements("X", {"2026-06-30": {"ni": 100}}))),
        seen_at=NOW, capture_window_days=135,
    )
    restated = ff.parse_statements("X", *map(json.loads, _statements("X", {"2026-06-30": {"ni": -900}})))
    again, added = ff.merge_first_seen(
        first, restated, seen_at=NOW + timedelta(days=5), capture_window_days=135
    )
    assert added == []
    assert again == first


def test_legacy_rows_are_dropped_once_alpha_vantage_answers():
    legacy = [{"report_date": "2025-12-31", "roe": 0.3}]
    rows = ff.parse_statements("X", *map(json.loads, _statements("X", {"2026-06-30": {}})))
    merged, _ = ff.merge_first_seen(legacy, rows, seen_at=NOW, capture_window_days=135)
    assert [r["report_date"] for r in merged] == ["2026-06-30"]
    kept, _ = ff.merge_first_seen(legacy, [], seen_at=NOW, capture_window_days=135)
    assert kept == legacy


# --- the plan ------------------------------------------------------------------


def _earnings_rows(fiscal: str, reported: str) -> list[dict]:
    return [{"fiscal_period": fiscal, "earnings_date": reported, "actual_eps": 1.0}]


def _stored(period: str) -> list[dict]:
    return [{"report_date": period, "source": "alphavantage", "roe": 0.1}]


def _asked(days_ago: float, *, success=True) -> dict:
    moment = fe._iso(NOW - timedelta(days=days_ago))
    return {"last_attempt_at": moment, **({"last_success_at": moment} if success else {})}


def _plan(**kw):
    defaults = dict(state={}, cache={}, earnings={}, now=NOW, budget=100,
                    statement_lag_days=3, retry_days=3, aging_days=100,
                    aging_retry_days=7, refusal_limit=3)
    return ff.plan_fundamentals(**{**defaults, **kw})


def test_never_fetched_come_first_newest_reported_quarter_first():
    plan = _plan(
        live=["OLD", "NEW", "NONE"],
        earnings={"OLD": _earnings_rows("2026-06-30", "2026-07-20"),
                  "NEW": _earnings_rows("2026-09-30", "2026-10-10")},
    )
    assert plan.never == ["NEW", "OLD", "NONE"]


def test_a_ticker_behind_its_earnings_is_re_asked_after_the_statement_lag():
    earnings = {
        "DUE": _earnings_rows("2026-09-30", "2026-10-12"),     # 5 days ago
        "FRESH": _earnings_rows("2026-09-30", "2026-10-16"),   # 1 day: not filed yet
        "ASKED": _earnings_rows("2026-09-30", "2026-10-10"),   # asked yesterday
        "CURRENT": _earnings_rows("2026-09-30", "2026-10-10"),
    }
    state = {"DUE": _asked(10), "FRESH": _asked(10), "ASKED": _asked(1), "CURRENT": _asked(2)}
    cache = {t: _stored("2026-06-30") for t in ("DUE", "FRESH", "ASKED")}
    cache["CURRENT"] = _stored("2026-09-27")  # a 52/53-week year: the same quarter
    plan = _plan(live=list(earnings), earnings=earnings, state=state, cache=cache)
    assert plan.behind == ["DUE"]
    assert plan.aging == []


def test_an_aging_ticker_without_earnings_is_re_asked_weekly():
    cache = {"OLD": _stored("2026-05-31"), "WEEK": _stored("2026-05-31"), "OK": _stored("2026-08-31")}
    state = {"OLD": _asked(8), "WEEK": _asked(2), "OK": _asked(30)}
    plan = _plan(live=["OLD", "WEEK", "OK"], cache=cache, state=state)
    assert plan.aging == ["OLD"]


def test_the_budget_buys_two_requests_a_ticker():
    plan = _plan(live=["A", "B", "C", "D"], budget=5)
    assert plan.selected == ["A", "B"]


# --- the run -------------------------------------------------------------------


def test_the_initial_fill_stays_unstamped_until_every_live_ticker_is_fetched(tmp_path):
    av = FakeAV()
    for t in ("A", "B", "C"):
        av.add(t, {"2026-06-30": {}})
    first = _run(tmp_path, av, ["A", "B", "C"], budget=4)
    assert (first.status, first.exit_code) == ("INCOMPLETE", fe.EXIT_INCOMPLETE)
    doc = _cache(tmp_path)
    assert doc.exists and doc.fetched_at is None, "a partial fill must read stale"
    assert set(doc.data) == {"A", "B"}
    assert any("not yet fetched" in m for m in first.messages)

    second = _run(tmp_path, av, ["A", "B", "C"], budget=4, now=NOW + timedelta(days=1))
    assert (second.status, second.exit_code) == ("CURRENT", fe.EXIT_CURRENT)
    doc = _cache(tmp_path)
    assert doc.fetched_at == NOW + timedelta(days=1)
    assert set(doc.data) == {"A", "B", "C"}
    assert assess_fundamentals(doc, as_of=NOW + timedelta(days=1)).fresh
    assert len(av.urls) == 6


def test_a_current_cache_with_nothing_due_is_restamped_without_a_call(tmp_path):
    av = FakeAV()
    av.add("A", {"2026-06-30": {}})
    _run(tmp_path, av, ["A"])
    later = NOW + timedelta(days=1)
    result = _run(tmp_path, av, ["A"], now=later)
    assert result.status == "CURRENT" and result.calls == 0
    assert _cache(tmp_path).fetched_at == later


GOOD = [f"G{i}" for i in range(5)]


def _good(av):
    for t in GOOD:
        av.add(t, {"2026-06-30": {}})


def test_a_delisted_name_costs_one_request_and_is_reported(tmp_path):
    av = FakeAV({("INCOME_STATEMENT", "ATVI"): _fixture("income_statement_ATVI.json")})
    _good(av)
    result = _run(tmp_path, av, GOOD + ["ATVI"], budget=12)
    assert av.requested().count(("INCOME_STATEMENT", "ATVI")) == 1
    assert ("BALANCE_SHEET", "ATVI") not in av.requested()
    assert result.status == "CURRENT", "a name AV has nothing for must not hold the cache stale"
    assert any(m.startswith("LIVE_NO_DATA:") and "ATVI" in m for m in result.messages)


def test_a_quota_notice_stops_the_run_and_saves_what_was_fetched(tmp_path):
    av = FakeAV()
    av.add("A", {"2026-06-30": {}})
    av.table[("INCOME_STATEMENT", "B")] = json.dumps({"Information": "rate limit"}).encode()
    result = _run(tmp_path, av, ["A", "B", "C"])
    assert (result.status, result.exit_code) == ("FAILED", fe.EXIT_FAILED)
    assert "limit" in result.stopped
    assert ("INCOME_STATEMENT", "C") not in av.requested()
    doc = _cache(tmp_path)
    assert set(doc.data) == {"A"} and doc.fetched_at is None


def test_a_transient_error_skips_the_ticker_and_loses_nothing(tmp_path):
    av = FakeAV()
    av.add("A", {"2026-06-30": {}})
    _run(tmp_path, av, ["A"])
    # A new quarter is due and Alpha Vantage's connection drops (retried once).
    write_cache_document(tmp_path / EARNINGS_FILE, {"A": _earnings_rows("2026-09-30", "2026-10-20")})
    later = NOW + timedelta(days=10)
    result = _run(tmp_path, FakeAV(default=OSError("connection reset")), ["A"], now=later)
    assert result.errored == ["A"] and result.calls == 2
    # Every live ticker has been fetched once and the run was not stopped:
    # the stamp moves, and the period rule judges the quarter (as in earnings).
    assert result.status == "CURRENT"
    assert [r["report_date"] for r in _cache(tmp_path).data["A"]] == ["2026-06-30"]


def test_retries_spend_the_budget_too(tmp_path):
    av = FakeAV()
    av.add("A", {"2026-06-30": {}})
    av.add("B", {"2026-06-30": {}})
    good = av.table[("INCOME_STATEMENT", "A")]
    flaky = iter([OSError("reset"), good])

    def first_fails(url):
        answer = next(flaky)
        if isinstance(answer, Exception):
            raise answer
        return answer

    av.table[("INCOME_STATEMENT", "A")] = first_fails
    result = _run(tmp_path, av, ["A", "B"], budget=4)
    assert result.calls <= 4, "a retry must not push the run past its budget"
    assert result.fetched == ["A"]


def test_a_symbol_refused_three_days_running_is_excluded_and_named(tmp_path):
    av = FakeAV({("INCOME_STATEMENT", "BAD"): json.dumps({"Error Message": "Invalid API call"}).encode()})
    _good(av)
    for day in range(3):
        result = _run(tmp_path, av, GOOD + ["BAD"], budget=12, now=NOW + timedelta(days=day))
    assert result.status == "CURRENT", "a refused name must not hold the sleeve degraded forever"
    assert any(m.startswith("LIVE_REFUSED:") and "BAD" in m for m in result.messages)


def test_the_overrides_map_universe_tickers_to_av_symbols(tmp_path):
    av = FakeAV()
    av.add("MRSH", {"2026-06-30": {}})
    av.add("BRK-B", {"2026-06-30": {}})
    _run(tmp_path, av, ["MMC", "BRK B"], symbol_overrides={"MMC": "MRSH"})
    assert set(_cache(tmp_path).data) == {"MMC", "BRK B"}


def test_the_state_file_records_each_attempt(tmp_path):
    av = FakeAV()
    av.add("A", {"2026-06-30": {}})
    _run(tmp_path, av, ["A"])
    state = json.loads((tmp_path / FUNDAMENTALS_STATE_FILE).read_text())
    entry = state["tickers"]["A"]
    assert entry["status"] == "ok" and entry["av_symbol"] == "A"
    assert entry["newest_offered"] == "2026-06-30"


def test_an_unreadable_cache_is_never_overwritten(tmp_path):
    (tmp_path / FUNDAMENTALS_FILE).write_text("{not json")
    with pytest.raises(RuntimeError, match="unreadable"):
        _run(tmp_path, FakeAV(), ["A"])
    assert (tmp_path / FUNDAMENTALS_FILE).read_text() == "{not json"


# --- the CLI: lock, key ----------------------------------------------------------


def test_the_earnings_lock_also_holds_off_the_fundamentals_refresh(tmp_path, capsys):
    with fe._CacheLock(tmp_path):
        code = ff.main(["--tickers", "A", "--cache-dir", str(tmp_path), "--min-interval", "0", "--budget", "6"],
                       transport=FakeAV(), env={"ALPHAVANTAGE_API_KEY": KEY})
    assert code == fe.EXIT_LOCKED
    assert "FUNDAMENTALS_REFRESH status=SKIPPED" in capsys.readouterr().out


def test_the_key_never_reaches_the_output(tmp_path, capsys):
    def leaky(url, timeout):
        raise OSError(f"connection reset fetching {url}")

    code = ff.main(["--tickers", "A", "--cache-dir", str(tmp_path), "--min-interval", "0", "--budget", "6"],
                   transport=leaky, env={"ALPHAVANTAGE_API_KEY": KEY})
    out = capsys.readouterr()
    assert code == fe.EXIT_FAILED
    assert KEY not in out.out and KEY not in out.err
    assert "FUNDAMENTALS_REFRESH status=FAILED" in out.out


def test_no_key_is_an_error_before_any_request(tmp_path, capsys):
    av = FakeAV()
    code = ff.main(["--tickers", "A", "--cache-dir", str(tmp_path)], transport=av,
                   env={"ALGO_SECURITY_BIN": "/nonexistent/security"})
    assert code == fe.EXIT_ERROR and av.urls == []


def test_the_main_entry_writes_a_cache_the_lookup_reads(tmp_path, capsys):
    av = FakeAV()
    av.add("A", {"2026-06-30": {"ni": 50, "eq": 500}})
    code = ff.main(["--tickers", "A", "--cache-dir", str(tmp_path), "--min-interval", "0", "--budget", "6"],
                   transport=av, env={"ALPHAVANTAGE_API_KEY": KEY})
    out = capsys.readouterr().out
    assert code == fe.EXIT_CURRENT, out
    # A --tickers run saves rows but cannot vouch for the universe.
    assert "status=SUBSET" in out
    doc = _cache(tmp_path)
    assert doc.fetched_at is None
    lookup = ff.build_fundamentals_lookup(doc.data)
    assert lookup("A", date.today())["roe"] == 0.1


# --- SHALLOW ---------------------------------------------------------------------


def _shallow_cache(period: str, tickers) -> dict[str, list[dict]]:
    return {t: _stored(period) for t in tickers}


def test_coverage_is_judged_at_the_replay_start():
    names = [f"T{i}" for i in range(10)]
    cache = _shallow_cache("2026-06-30", names)
    problem = ff.history_coverage_problem(
        cache, start=date(2026, 1, 2), tickers=names, max_missing_fraction=0.2
    )
    assert problem and problem.startswith("SHALLOW: 10 of 10")
    assert ff.history_coverage_problem(
        cache, start=date(2026, 8, 14), tickers=names, max_missing_fraction=0.2
    ) is None


def test_coverage_tolerates_the_same_share_as_the_period_rule():
    names = [f"T{i}" for i in range(10)]
    cache = _shallow_cache("2025-06-30", names[:8])
    assert ff.history_coverage_problem(
        cache, start=date(2026, 1, 2), tickers=names, max_missing_fraction=0.2
    ) is None
    cache = _shallow_cache("2025-06-30", names[:7])
    assert ff.history_coverage_problem(
        cache, start=date(2026, 1, 2), tickers=names, max_missing_fraction=0.2
    )


def _fresh_fundamentals(tmp_path, period: str, tickers) -> None:
    rows = {t: _stored(period) + [{**_stored("2026-06-30")[0]}] for t in tickers}
    write_cache_document(tmp_path / FUNDAMENTALS_FILE, rows,
                         fetched_at=datetime(2026, 10, 16, tzinfo=timezone.utc))


def _bars(tickers, first: date) -> dict[str, list[dict]]:
    return {t: [{"date": first, "close": 1.0}, {"date": date(2026, 10, 16), "close": 1.0}]
            for t in tickers}


def test_a_backtest_older_than_the_fundamentals_history_degrades_quality_value(tmp_path):
    from scripts.run_backtest import load_backtest_caches

    names = list(UNIVERSE_REGISTRY["quality_value"])[:10]
    _fresh_fundamentals(tmp_path, "2026-03-31", names)
    _, degraded = load_backtest_caches(
        sleeves=["quality_value"], bars_by_ticker=_bars(names, date(2016, 1, 4)),
        allow_degraded=True, cache_dir=str(tmp_path),
    )
    assert degraded["quality_value"].startswith("SHALLOW")

    _, degraded = load_backtest_caches(
        sleeves=["quality_value"], bars_by_ticker=_bars(names, date(2026, 8, 3)),
        allow_degraded=True, cache_dir=str(tmp_path),
    )
    assert "quality_value" not in degraded


def test_a_shadow_warm_up_older_than_the_history_leaves_quality_value_ungraded():
    from scripts.run_paper import shadow_data_degraded

    names = list(UNIVERSE_REGISTRY["quality_value"])[:10]
    sessions = [date(2026, 1, 2) + timedelta(days=i) for i in range(300)]
    bars = {t: [{"date": d, "close": 1.0} for d in sessions] for t in names}
    shallow = {t: _stored("2026-06-30") for t in names}
    degraded = shadow_data_degraded(
        {"earnings_drift": "STALE"}, fundamentals=shallow, bars_by_ticker=bars,
        sessions_back=180, max_missing_fraction=0.2,
    )
    assert degraded["earnings_drift"] == "STALE"
    assert degraded["quality_value"].startswith("SHALLOW")

    deep = {t: _stored("2025-06-30") + _stored("2026-06-30") for t in names}
    assert "quality_value" not in shadow_data_degraded(
        {}, fundamentals=deep, bars_by_ticker=bars, sessions_back=180,
        max_missing_fraction=0.2,
    )


def test_the_paper_run_hands_the_shadow_its_own_degraded_set():
    """The helper is only half of it: the 05:15 run must pass its result."""
    import inspect

    from scripts import run_paper

    source = inspect.getsource(run_paper)
    call = source[source.index("shadow_path = produce_shadow_artifact("):]
    call = call[: call.index("print(f\"  Shadow series written")]
    assert "data_degraded=shadow_data_degraded(" in call


# --- review fixes (PR #247) ------------------------------------------------------


def test_a_quarter_is_not_available_before_the_cache_first_saw_it():
    row = {"report_date": "2026-06-30", "first_seen_at": "2026-10-17T01:00:00+00:00", "roe": 0.1}
    lookup = ff.build_fundamentals_lookup({"A": [row]})
    assert lookup("A", date(2026, 8, 14)) is None, "45 days after period end, but not yet seen"
    assert lookup("A", date(2026, 10, 17))["roe"] == 0.1


def test_a_full_pass_with_too_many_names_missing_is_thin_not_current(tmp_path):
    av = FakeAV()
    for t in ("A", "B", "C"):
        av.add(t, {"2026-06-30": {}})
    # D and E: Alpha Vantage has nothing (2 of 5 = 40% missing).
    result = _run(tmp_path, av, ["A", "B", "C", "D", "E"], budget=10)
    assert (result.status, result.exit_code) == ("THIN", fe.EXIT_FAILED)
    assert _cache(tmp_path).fetched_at is None
    assert any(m.startswith("THIN:") for m in result.messages)


def test_a_quarter_offered_but_unstorable_is_not_re_asked_every_few_days(tmp_path):
    earnings = {"A": _earnings_rows("2026-03-31", "2026-04-25")}
    state = {"A": {**_asked(4), "newest_offered": "2026-03-31"}}
    plan = _plan(live=["A"], earnings=earnings, state=state, cache={})
    assert plan.behind == [] and plan.aging == [], "asked 4 days ago: wait for the weekly cadence"
    state = {"A": {**_asked(8), "newest_offered": "2026-03-31"}}
    plan = _plan(live=["A"], earnings=earnings, state=state, cache={})
    assert plan.aging == ["A"]


def test_aging_is_only_for_a_ticker_the_earnings_cache_knows_nothing_about():
    cache = {"A": _stored("2026-05-31"), "B": _stored("2026-05-31")}
    state = {"A": _asked(30), "B": _asked(30)}
    earnings = {"A": _earnings_rows("2026-05-31", "2026-06-20")}  # nothing newer out yet
    plan = _plan(live=["A", "B"], cache=cache, state=state, earnings=earnings)
    assert plan.aging == ["B"]


def test_income_and_balance_dated_days_apart_are_one_quarter():
    income, balance = map(json.loads, _statements("X", {"2026-06-28": {}}))
    balance["quarterlyReports"][0]["fiscalDateEnding"] = "2026-06-30"
    rows = ff.parse_statements("X", income, balance)
    assert [r["report_date"] for r in rows] == ["2026-06-28"]


def test_every_request_including_retries_stays_inside_the_budget(tmp_path):
    av = FakeAV(default=OSError("reset"))
    result = _run(tmp_path, av, ["A", "B", "C"], budget=6)
    assert len(av.urls) <= 6
    result = _run(tmp_path, FakeAV(default=OSError("reset")), ["A"], budget=3)
    assert result.calls <= 3


def test_legacy_rows_survive_an_answer_with_nothing_storable():
    legacy = [{"report_date": "2025-12-31", "roe": 0.3}]
    old = ff.parse_statements("X", *map(json.loads, _statements("X", {"2025-06-30": {}})))
    merged, added = ff.merge_first_seen(legacy, old, seen_at=NOW, capture_window_days=135)
    assert added == [] and merged == legacy


def _flaky_first(answer: bytes):
    calls = iter([OSError("reset")])

    def respond(url):
        for exc in calls:
            raise exc
        return answer

    return respond


def test_a_retry_on_the_balance_sheet_cannot_overspend(tmp_path):
    av = FakeAV()
    av.add("A", {"2026-06-30": {}})
    av.table[("BALANCE_SHEET", "A")] = OSError("reset")
    result = _run(tmp_path, av, ["A"], budget=2)
    assert len(av.urls) == 2 and result.calls == 2


def test_a_retried_income_statement_leaves_no_room_for_the_balance_sheet(tmp_path):
    av = FakeAV()
    av.add("A", {"2026-06-30": {}})
    av.table[("INCOME_STATEMENT", "A")] = _flaky_first(av.table[("INCOME_STATEMENT", "A")])
    result = _run(tmp_path, av, ["A"], budget=2)
    assert len(av.urls) == 2, "the balance sheet would be the third request"
    assert result.fetched == [] and not _cache(tmp_path).data


def test_calls_the_earnings_run_borrowed_come_out_of_the_budget(tmp_path, capsys):
    av = FakeAV()
    code = ff.main(["--tickers", "A", "--cache-dir", str(tmp_path), "--min-interval", "0",
                    "--budget", "6", "--yield-calls", "6"],
                   transport=av, env={"ALPHAVANTAGE_API_KEY": KEY})
    assert av.urls == [], "a fully borrowed budget asks nothing"
    assert "budget 0 request(s)" in capsys.readouterr().out
    assert code != fe.EXIT_ERROR
