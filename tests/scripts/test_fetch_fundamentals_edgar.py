"""SEC EDGAR fundamentals: first-filed quarters, derived Q4, and the run's stamp."""
from __future__ import annotations

import json

import pytest

from scripts import fetch_earnings as fe
from scripts import fetch_fundamentals_edgar as fx
from shared.data_cache import FUNDAMENTALS_FILE, read_cache_document, write_cache_document

UA = "algo-poc test@example.com"


def _flow(val, start, end, filed, form="10-Q"):
    return {"val": val, "start": start, "end": end, "filed": filed, "form": form}


def _inst(val, end, filed, form="10-Q"):
    return {"val": val, "end": end, "filed": filed, "form": form}


def _company(**concepts) -> dict:
    return {"facts": {"us-gaap": {c: {"units": {"USD": f}} for c, f in concepts.items()}}}


def _year(ni=(10, 20, 30, 100), revenue_concept="Revenues", restated_q1=None):
    """Fiscal 2025 by calendar quarter: three 10-Q quarters and the 10-K year."""
    q = [("2025-01-01", "2025-03-31", "2025-05-01"), ("2025-04-01", "2025-06-30", "2025-08-01"),
         ("2025-07-01", "2025-09-30", "2025-11-01")]
    net = [_flow(v, s, e, f) for v, (s, e, f) in zip(ni[:3], q)]
    net.append(_flow(ni[3], "2025-01-01", "2025-12-31", "2026-02-15", "10-K"))
    if restated_q1 is not None:  # the next year's 10-Q comparative, restated
        net.append(_flow(restated_q1, "2025-01-01", "2025-03-31", "2026-05-01"))
    ends = [e for _, e, _ in q] + ["2025-12-31"]
    filed = [f for *_, f in q] + ["2026-02-15"]
    return _company(
        NetIncomeLoss=net,
        **{revenue_concept: [_flow(100, s, e, f) for s, e, f in q]
           + [_flow(400, "2025-01-01", "2025-12-31", "2026-02-15", "10-K")]},
        StockholdersEquity=[_inst(200, e, f) for e, f in zip(ends, filed)],
        LongTermDebtNoncurrent=[_inst(80, e, f) for e, f in zip(ends, filed)],
        LongTermDebtCurrent=[_inst(20, e, f) for e, f in zip(ends, filed)],
        CommercialPaper=[_inst(10, e, f) for e, f in zip(ends, filed)],
    )


def test_q4_is_the_year_less_three_quarters_and_dated_by_the_10k():
    rows = fx.parse_companyfacts("AAA", _year())
    assert [r["report_date"] for r in rows] == ["2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31"]
    q4 = rows[-1]
    assert q4["net_income"] == 40 and q4["total_revenue"] == 100
    assert q4["filing_date"] == "2026-02-15"
    assert q4["roe"] == 0.2 and q4["profit_margin"] == 0.4
    # Noncurrent + current long-term debt + commercial paper.
    assert q4["total_debt"] == 110 and q4["debt_equity"] == 0.55
    assert q4["source"] == "edgar"


def test_a_later_restated_comparative_never_replaces_the_first_filing():
    rows = fx.parse_companyfacts("AAA", _year(restated_q1=999))
    assert rows[0]["net_income"] == 10
    assert rows[0]["filing_date"] == "2025-05-01"


def test_revenue_falls_back_to_the_next_concept_a_filer_uses():
    rows = fx.parse_companyfacts("BANK", _year(revenue_concept="RevenuesNetOfInterestExpense"))
    assert len(rows) == 4 and rows[0]["total_revenue"] == 100


def test_a_quarter_missing_equity_or_revenue_is_dropped():
    company = _year()
    company["facts"]["us-gaap"]["StockholdersEquity"]["units"]["USD"].pop(0)
    rows = fx.parse_companyfacts("AAA", company)
    assert "2025-03-31" not in [r["report_date"] for r in rows]


def test_brk_b_is_spelled_the_way_sec_lists_it():
    assert fx.edgar_ticker("BRK B") == fx.edgar_ticker("BRK.B") == "BRK-B"


class FakeSEC:
    def __init__(self, companies: dict[str, dict], fail: dict[str, int] | None = None):
        self.tickers = {str(i): {"cik_str": 1000 + i, "ticker": t} for i, t in enumerate(companies)}
        self.by_cik = {1000 + i: c for i, c in enumerate(companies.values())}
        self.fail = dict(fail or {})
        self.agents: set[str] = set()

    def __call__(self, url, user_agent, timeout):
        self.agents.add(user_agent)
        if url == fx.TICKERS_URL:
            return json.dumps(self.tickers).encode()
        cik = int(url.rsplit("CIK", 1)[1].split(".")[0])
        if self.fail.get(cik, 0) > 0:
            self.fail[cik] -= 1
            raise OSError("timed out")
        return json.dumps(self.by_cik[cik]).encode()


def _run(tmp_path, sec, live, **kw):
    client = fx.EdgarClient(UA, transport=sec, min_interval_seconds=0)
    return fx.run_edgar_refresh(client=client, cache_dir=tmp_path, live=live, log=lambda _: None, **kw)


def test_a_full_pass_stamps_fetched_at_and_drops_other_sources(tmp_path):
    write_cache_document(tmp_path / FUNDAMENTALS_FILE,
                         {"AAA": [{"report_date": "2025-06-30", "source": "alphavantage"}]},
                         fetch_time_unknown=True, source="alphavantage")
    sec = FakeSEC({"AAA": _year(), "BBB": _year()})
    summary, code = _run(tmp_path, sec, ["AAA", "BBB"])
    doc = read_cache_document(tmp_path / FUNDAMENTALS_FILE)
    assert code == fe.EXIT_CURRENT and "status=CURRENT" in summary
    assert doc.fetched_at is not None and doc.source == "edgar"
    assert {r["source"] for rows in doc.data.values() for r in rows} == {"edgar"}
    assert sec.agents == {UA}, "every request names the contact"


def test_a_transient_failure_is_retried_once(tmp_path):
    sec = FakeSEC({"AAA": _year()}, fail={1000: 1})
    _, code = _run(tmp_path, sec, ["AAA"])
    assert code == fe.EXIT_CURRENT


def test_a_ticker_that_keeps_failing_keeps_the_stamp_where_it_was(tmp_path):
    names = ["AAA", "BBB", "CCC", "DDD", "EEE"]  # one of five failing is not THIN
    sec = FakeSEC({n: _year() for n in names}, fail={1001: 2})
    summary, code = _run(tmp_path, sec, names)
    assert code == fe.EXIT_FAILED and "status=FAILED" in summary
    assert read_cache_document(tmp_path / FUNDAMENTALS_FILE).fetched_at is None


def test_too_many_unmapped_names_is_thin_not_current(tmp_path):
    sec = FakeSEC({"AAA": _year()})
    summary, code = _run(tmp_path, sec, ["AAA", "GONE1", "GONE2"])
    assert code == fe.EXIT_FAILED and "status=THIN" in summary


def test_a_renamed_listing_is_found_through_the_override(tmp_path):
    sec = FakeSEC({"MRSH": _year()})
    _, code = _run(tmp_path, sec, ["MMC"], symbol_overrides={"MMC": "MRSH"})
    assert code == fe.EXIT_CURRENT
    assert "MMC" in read_cache_document(tmp_path / FUNDAMENTALS_FILE).data


def test_no_user_agent_is_an_error_before_any_request(tmp_path, capsys):
    sec = FakeSEC({"AAA": _year()})
    code = fx.main(["--tickers", "AAA", "--cache-dir", str(tmp_path)], transport=sec,
                   env={"ALGO_SECURITY_BIN": "/nonexistent/security"})
    assert code == fe.EXIT_ERROR and sec.agents == set()
    assert "SEC_USER_AGENT" in capsys.readouterr().err


def test_the_main_entry_honours_the_cache_lock(tmp_path, capsys):
    with fe._CacheLock(tmp_path):
        code = fx.main(["--tickers", "AAA", "--cache-dir", str(tmp_path)],
                       transport=FakeSEC({"AAA": _year()}), env={"SEC_USER_AGENT": UA})
    assert code == fe.EXIT_LOCKED
    assert "status=SKIPPED" in capsys.readouterr().out
