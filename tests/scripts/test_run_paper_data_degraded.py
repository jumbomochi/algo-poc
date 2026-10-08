"""KAN-109: the paper run and its shadow fail loudly on missing or stale caches.

From 2026-09-24 the deploy clone had no ``data/cache/`` files, the loaders
returned ``{}``, and quality_value and earnings_drift ran with nothing to decide
on — no alert, no log line, a book that read as a quiet market. These tests pin
the replacement, end to end through ``main()`` and piece by piece:

* a missing or stale cache raises ONE alert naming the sleeve, cache and age;
* that sleeve places no new entries (its sells still run);
* its shadow is not replayed, and the artifact says why;
* fresh caches behave exactly as before — no alert, nothing degraded.
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import scripts.run_paper as run_paper
from backtest.shadow_artifact import load_shadow
from scripts.paper_state import PaperTradingState
from scripts.run_backtest import PortfolioConfig
from services.risk_management.engine import RiskEngine
from shared.data_cache import write_cache_document
from shared.models.base import Base

SGT = ZoneInfo("Asia/Singapore")
#: Tue 2026-10-06 05:15 SGT = Mon 2026-10-05 17:15 EDT: Monday has closed.
RUN_AT = datetime(2026, 10, 6, 5, 15, tzinfo=SGT)
SESSION = date(2026, 10, 5)
DEGRADABLE = {"quality_value", "earnings_drift"}


# ---------------------------------------------------------------------------
# main(): the wiring, with everything past the cache check captured
# ---------------------------------------------------------------------------


class FakeSession:
    def close(self): pass
    def commit(self): pass
    def rollback(self): pass


@pytest.fixture
def harness(monkeypatch, tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setenv("ALGO_DATA_CACHE_DIR", str(cache_dir))
    h = SimpleNamespace(alerts=[], run_daily=None, shadow=None, cache_dir=cache_dir)

    def fake_alert(redis_url, **kwargs):
        h.alerts.append(kwargs)
        return True

    async def fake_broker_snapshot(**kwargs):
        return SimpleNamespace(account_id="DU0000000")

    bars = {
        t: [{"date": SESSION - timedelta(days=3), "open": 10, "high": 10,
             "low": 10, "close": 10.0, "volume": 1},
            {"date": SESSION, "open": 10, "high": 10, "low": 10,
             "close": 10.5, "volume": 1}]
        for t in ("AAA", "BBB")
    }
    capital = SimpleNamespace(
        net_liquidation_base=1.0, net_liquidation_trading_equivalent=1.0,
        fx_base_per_trading=1.0, settled_cash_trading=1.0,
        deployable_capital=1.0, sleeve_budgets={},
    )

    def capture_run_daily(*args, **kwargs):
        h.run_daily = kwargs
        return []

    def capture_shadow(**kwargs):
        h.shadow = kwargs
        return tmp_path / "shadow.json"

    monkeypatch.setattr(run_paper, "_utc_now", lambda: RUN_AT.astimezone(timezone.utc))
    monkeypatch.setattr(run_paper, "emit_alert_best_effort", fake_alert)
    monkeypatch.setattr(run_paper, "make_db_session", lambda url: FakeSession())
    monkeypatch.setattr(
        run_paper.PaperTradingState, "load", classmethod(lambda cls, s: SimpleNamespace())
    )
    monkeypatch.setattr(run_paper, "read_broker_snapshot", fake_broker_snapshot)
    monkeypatch.setattr(
        run_paper, "prepare_daily_run",
        lambda **kw: SimpleNamespace(
            reconciliation=SimpleNamespace(entries_allowed=True, severity="ok"),
            capital=capital,
            capital_snapshot=SimpleNamespace(captured_at=RUN_AT),
        ),
    )
    monkeypatch.setattr(run_paper, "get_union_universe", lambda sleeves: ["AAA", "BBB"])
    monkeypatch.setattr(run_paper, "fetch_bars_from_ib", lambda **kw: bars)
    monkeypatch.setattr(run_paper, "build_portfolio_contexts", lambda *a, **kw: {})
    monkeypatch.setattr(run_paper, "build_portfolios", lambda **kw: {})
    monkeypatch.setattr(run_paper, "build_sell_availability", lambda *a: {})
    monkeypatch.setattr(run_paper, "account_buy_commitments_after_snapshot", lambda *a, **kw: 0.0)
    monkeypatch.setattr(run_paper, "run_daily", capture_run_daily)
    monkeypatch.setattr(run_paper, "produce_shadow_artifact", capture_shadow)
    monkeypatch.setattr(run_paper, "live_equity_by_sleeve", lambda state: {})
    monkeypatch.setattr(sys, "argv", [
        "run_paper.py", "--db-url", "sqlite://", "--redis-url", "redis://x",
        "--ml-shadow-model", "", "--shadow-output-dir", str(tmp_path),
    ])
    return h


def _write_fresh(cache_dir, *, fetched_at=RUN_AT):
    write_cache_document(
        cache_dir / "fundamentals.json",
        {"AAA": [{"report_date": "2026-06-30", "roe": 0.2}]},
        fetched_at=fetched_at,
    )
    write_cache_document(
        cache_dir / "earnings.json",
        {"AAA": [{"earnings_date": "2026-07-20", "surprise_pct": 6.0}]},
        fetched_at=fetched_at,
    )


def test_missing_caches_page_once_and_degrade_both_sleeves_live_and_shadow(harness, capsys):
    assert run_paper.main() == 0

    [alert] = harness.alerts
    assert alert["event_type"] == "sleeve_data_degraded"
    assert alert["priority"] == "high"
    assert alert["context"]["sleeves"] == ["earnings_drift", "quality_value"]
    assert "MISSING" in alert["message"]
    for cache in ("fundamentals", "earnings"):
        assert alert["context"]["caches"][cache]["fresh"] is False
        assert str(harness.cache_dir) in alert["context"]["caches"][cache]["path"]

    assert set(harness.run_daily["data_degraded"]) == DEGRADABLE
    assert set(harness.shadow["data_degraded"]) == DEGRADABLE

    out = capsys.readouterr().out
    assert "DATA-DEGRADED: quality_value" in out
    assert "DATA-DEGRADED: earnings_drift" in out
    assert "DATA-DEGRADED this run (no new entries): earnings_drift, quality_value" in out


def test_legacy_caches_without_fetched_at_are_stale(harness):
    """The 2026-03-26 caches' format: loadable, but of unknown age."""
    (harness.cache_dir / "fundamentals.json").write_text(json.dumps(
        {"AAA": [{"report_date": "2026-06-30", "roe": 0.2}]}))
    (harness.cache_dir / "earnings.json").write_text(json.dumps(
        {"AAA": [{"earnings_date": "2026-07-20", "surprise_pct": 6.0}]}))

    assert run_paper.main() == 0

    [alert] = harness.alerts
    assert "fetch time unknown" in alert["message"]
    assert set(harness.run_daily["data_degraded"]) == DEGRADABLE
    assert set(harness.shadow["data_degraded"]) == DEGRADABLE


def test_a_stale_earnings_fetch_degrades_only_earnings_drift(harness):
    _write_fresh(harness.cache_dir)
    write_cache_document(
        harness.cache_dir / "earnings.json",
        {"AAA": [{"earnings_date": "2026-07-20", "surprise_pct": 6.0}]},
        fetched_at=RUN_AT - timedelta(days=3),
    )

    run_paper.main()

    [alert] = harness.alerts
    assert alert["context"]["sleeves"] == ["earnings_drift"]
    assert set(harness.run_daily["data_degraded"]) == {"earnings_drift"}
    assert set(harness.shadow["data_degraded"]) == {"earnings_drift"}


def test_fresh_caches_behave_as_before(harness, capsys):
    _write_fresh(harness.cache_dir)

    assert run_paper.main() == 0

    assert harness.alerts == []
    assert harness.run_daily["data_degraded"] == {}
    assert harness.shadow["data_degraded"] == {}
    out = capsys.readouterr().out
    assert "DATA-DEGRADED" not in out
    assert "Data cache: fundamentals cache ok" in out


def test_a_tagged_drill_run_is_not_paged_about_sleeves_it_does_not_trade(harness, monkeypatch):
    monkeypatch.setattr(run_paper, "ensure_tagged_portfolio", lambda *a: False)
    monkeypatch.setattr(run_paper, "build_drill_portfolio", lambda **kw: object())
    monkeypatch.setattr(
        run_paper, "build_portfolio_contexts",
        lambda *a, **kw: {"__drill__": SimpleNamespace(reserved_notional=0.0)},
    )
    sys.argv += ["--portfolio-tag", "__drill__", "--portfolio-tag-capital", "500"]

    run_paper.main()

    assert harness.alerts == []
    assert harness.run_daily["data_degraded"] == {}


# ---------------------------------------------------------------------------
# run_daily: no new entries from a degraded sleeve, exits still run
# ---------------------------------------------------------------------------


@pytest.fixture
def state():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    st = PaperTradingState.create_new(
        portfolio_capitals={"quality_value": 10_000.0, "momentum": 10_000.0},
        session=session,
    )
    yield st
    session.close()


def _bars(close: float = 100.0) -> list[dict]:
    return [{"date": f"2026-07-0{i + 1}", "open": close, "high": close,
             "low": close, "close": close, "volume": 1000} for i in range(5)]


def _portfolio(name, signals_fn) -> PortfolioConfig:
    return PortfolioConfig(
        name=name, capital=10_000.0, signals_fn=signals_fn,
        risk_engine=RiskEngine(position_entry_limit_pct=10.0,
                               sector_concentration_pct=100.0,
                               total_exposure_limit_pct=100.0,
                               max_lots_per_ticker=1),
    )


def _run_daily(state, **kwargs):
    def signal_fn(ticker, bars):
        action = "buy" if ticker == "MSFT" else "sell"
        return {"action": action, "limit_price": 100.0, "quantity": 1.0}

    portfolios = {
        "quality_value": _portfolio("quality_value", signal_fn),
        "momentum": _portfolio("momentum", signal_fn),
    }
    return run_paper.run_daily(
        state, portfolios, {"AAPL": _bars(), "MSFT": _bars()},
        settled_cash_trading=1_000_000, active_buy_reservations_usd=0,
        commission_per_share_usd=0.005, minimum_commission_usd=1,
        minimum_settled_usd_reserve=0, **kwargs,
    )


def test_a_degraded_sleeve_places_no_entries_but_still_exits(state, capsys):
    signals = _run_daily(
        state, data_degraded={"quality_value": "fundamentals cache MISSING"}
    )

    by_sleeve = {(s["portfolio"], s["action"]) for s in signals}
    assert ("quality_value", "buy") not in by_sleeve
    assert ("quality_value", "sell") in by_sleeve
    # Other sleeves are untouched.
    assert ("momentum", "buy") in by_sleeve
    assert ("momentum", "sell") in by_sleeve
    assert "[quality_value] (data-degraded)" in capsys.readouterr().out


def test_no_degradation_is_the_old_behaviour(state):
    assert _run_daily(state, data_degraded={}) == _run_daily(state)


# ---------------------------------------------------------------------------
# The shadow: not replayed, and named in the artifact
# ---------------------------------------------------------------------------

SESSIONS = [date(2026, 1, 5) + timedelta(days=i) for i in range(140)]


def _universe_bars():
    from shared.universe import ACTIVE_SLEEVES, UNIVERSE_REGISTRY

    universe = sorted({t for s in ACTIVE_SLEEVES for t in UNIVERSE_REGISTRY[s]})
    return {
        t: [{"date": d, "open": 100.0, "high": 101.0, "low": 99.0,
             "close": 100.0 * (1 + 0.0005 * (n + 1) * i), "volume": 1_000_000}
            for i, d in enumerate(SESSIONS)]
        for n, t in enumerate(universe)
    }


def _produce(tmp_path, name, **overrides):
    from shared.universe import ACTIVE_SLEEVES

    kwargs = dict(
        output_path=tmp_path / name,
        capital=100_000.0,
        bars_by_ticker=_universe_bars(),
        regime_by_date={d: "bull" for d in SESSIONS},
        fundamentals_lookup=lambda t, d: {"roe": 0.3, "debt_equity": 0.4,
                                          "profit_margin": 0.2},
        earnings_lookup=lambda t, d: {"surprise_pct": 8.0},
        live_equity={s: {d: 10_000.0 for d in SESSIONS} for s in ACTIVE_SLEEVES},
        window_sessions=30,
    )
    kwargs.update(overrides)
    return load_shadow(run_paper.produce_shadow_artifact(**kwargs))


def test_a_degraded_sleeve_s_shadow_is_not_replayed_and_says_why(tmp_path):
    from shared.universe import ACTIVE_SLEEVES

    reasons = {"quality_value": "fundamentals cache MISSING",
               "earnings_drift": "earnings cache STALE"}
    artifact = _produce(tmp_path, "degraded.json", data_degraded=reasons)

    assert set(artifact.series) == set(ACTIVE_SLEEVES) - DEGRADABLE
    assert artifact.data_degraded == reasons


def test_degradation_does_not_move_the_shadow_id(tmp_path):
    """Being degraded is a data condition, not a model change: it must not
    restart the epoch (D13)."""
    clean = _produce(tmp_path, "clean.json")
    degraded = _produce(tmp_path, "degraded.json",
                        data_degraded={"quality_value": "missing"})
    assert clean.shadow_id == degraded.shadow_id
    assert clean.data_degraded == {}
    assert clean.series["momentum"] == degraded.series["momentum"]


# ---------------------------------------------------------------------------
# PR #235 review: a rank replacement is a data-driven exit, a stop is not
# ---------------------------------------------------------------------------

EXITS = {"AAPL": "rank_replacement", "MSFT": "trailing_stop", "NVDA": "time_exit"}


def _run_exits(state, **kwargs):
    def signal_fn(ticker, bars):
        return {"action": "sell", "limit_price": 100.0, "quantity": 1.0,
                "exit_reason": EXITS[ticker]}

    return run_paper.run_daily(
        state, {"quality_value": _portfolio("quality_value", signal_fn)},
        {t: _bars() for t in EXITS},
        settled_cash_trading=1_000_000, active_buy_reservations_usd=0,
        commission_per_share_usd=0.005, minimum_commission_usd=1,
        minimum_settled_usd_reserve=0, **kwargs,
    )


def test_a_degraded_sleeve_holds_its_rank_replacements_but_takes_its_stops(state, capsys):
    """Ranked on the untrusted cache, with the paired buy already skipped, a
    rank replacement would sell a holding and buy nothing in its place."""
    signals = _run_exits(state, data_degraded={"quality_value": "fundamentals cache STALE"})

    assert sorted(s["exit_reason"] for s in signals) == ["time_exit", "trailing_stop"]
    assert ("AAPL  [quality_value] (data-degraded: rank_replacement exit"
            in capsys.readouterr().out)


def test_a_healthy_sleeve_still_takes_its_rank_replacements(state):
    signals = _run_exits(state, data_degraded={})
    assert sorted(s["exit_reason"] for s in signals) == [
        "rank_replacement", "time_exit", "trailing_stop",
    ]
