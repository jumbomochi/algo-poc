"""KAN-119 — backtest-only symbol aliases for renamed former members, and the
members whose IB series is another company.

Pinned:

* live isolation: no live path can reach the alias table — not
  ``make_stock_contract``, not the paper run's bar fetch, not any service;
* the fetch prices an alias under its conId, stores the bars under the
  membership ticker, and stops them where the membership file lists the new
  ticker (no session prices one conId under two tickers);
* every entry in the table satisfies the same-instrument invariants that can
  be checked offline (the IB evidence is in the decision record).
"""

from __future__ import annotations

import sys
import types
from datetime import date, timedelta
from pathlib import Path

import pytest

from backtest import symbol_aliases as sa
from shared.universe import (
    CONTRACT_CONID_OVERRIDES,
    MEMBERSHIP_SNAPSHOT_PATH,
    SP500_TOP100,
    MembershipCalendar,
    make_stock_contract,
)

REPO = Path(__file__).resolve().parents[2]

ALIAS = sa.BacktestAlias(
    ticker="OLD", ib_symbol="NEW", con_id=4242, long_name="NEW CO",
    until=date(2022, 7, 1), evidence="synthetic",
)


# --- live isolation -------------------------------------------------------------


def test_no_live_module_imports_the_alias_table():
    offenders = []
    for root in ("shared", "services"):
        for path in (REPO / root).rglob("*.py"):
            if "symbol_aliases" in path.read_text():
                offenders.append(str(path.relative_to(REPO)))
    for script in ("scripts/run_paper.py", "scripts/reconcile_paper.py"):
        text = (REPO / script).read_text()
        if any(n in text for n in ("symbol_aliases", "BACKTEST_ALIASES", "BACKTEST_UNPRICED")):
            offenders.append(script)
    assert offenders == []


def test_live_contract_construction_is_unchanged_for_every_name():
    names = set(SP500_TOP100) | set(sa.BACKTEST_ALIASES)
    for ticker in sorted(names):
        contract = make_stock_contract(ticker)
        if ticker in CONTRACT_CONID_OVERRIDES:
            assert contract.conId == CONTRACT_CONID_OVERRIDES[ticker]
        else:
            assert (contract.symbol, contract.conId) == (ticker, 0), ticker


def test_without_aliases_the_backtest_builds_exactly_the_live_contract():
    for ticker in ("AAPL", "MMC", "FB"):
        assert sa.backtest_contract(ticker, None) == make_stock_contract(ticker)


def test_an_alias_is_pinned_by_conid():
    contract = sa.backtest_contract("OLD", {"OLD": ALIAS})
    assert (contract.conId, contract.exchange, contract.currency) == (4242, "SMART", "USD")


# --- the clip ---------------------------------------------------------------------


def test_alias_bars_stop_where_the_new_ticker_joins():
    bars = [{"date": date(2022, 6, 30)}, {"date": date(2022, 7, 1)}, {"date": "2022-07-05"}]
    assert sa.clip_to_alias("OLD", bars, {"OLD": ALIAS}) == bars[:1]
    assert sa.clip_to_alias("OTHER", bars, {"OLD": ALIAS}) == bars


# --- the fetch ---------------------------------------------------------------------


class _Bar:
    def __init__(self, day: date):
        self.date, self.open, self.high, self.low, self.close, self.volume = day, 1, 1, 1, 1, 1


class _FakeIB:
    requested: list = []

    def connect(self, *a, **k):
        pass

    def managedAccounts(self):
        return ["DU0"]

    def qualifyContracts(self, contract):
        return [contract]

    def reqHistoricalData(self, contract, endDateTime, **k):
        _FakeIB.requested.append(contract)
        end = date(int(endDateTime[:4]), int(endDateTime[4:6]), int(endDateTime[6:8]))
        return [_Bar(end - timedelta(days=d)) for d in range(0, 360, 7)]

    def sleep(self, s):
        pass

    def disconnect(self):
        pass


def test_the_fetch_prices_an_alias_under_its_conid_and_stores_it_under_the_old_ticker(monkeypatch):
    from scripts import run_backtest

    fake = types.ModuleType("ib_insync")
    fake.IB = _FakeIB
    import ib_insync

    fake.Stock = ib_insync.Stock
    monkeypatch.setitem(sys.modules, "ib_insync", fake)
    _FakeIB.requested = []
    alias = sa.BacktestAlias(
        ticker="OLD", ib_symbol="NEW", con_id=4242, long_name="NEW CO",
        until=date.today() - timedelta(days=100), evidence="synthetic",
    )
    bars = run_backtest.fetch_bars_from_ib(["OLD", "AAPL"], years=1, aliases={"OLD": alias})
    assert [c.conId for c in _FakeIB.requested[:1]] == [4242]
    assert set(bars) == {"OLD", "AAPL"}
    assert bars["OLD"] and max(b["date"] for b in bars["OLD"]) < alias.until
    assert max(b["date"] for b in bars["AAPL"]) >= alias.until


def test_an_unpriced_member_is_stored_with_no_bars_and_never_requested(monkeypatch):
    from scripts import run_backtest

    fake = types.ModuleType("ib_insync")
    fake.IB = _FakeIB
    import ib_insync

    fake.Stock = ib_insync.Stock
    monkeypatch.setitem(sys.modules, "ib_insync", fake)
    _FakeIB.requested = []
    unpriced = {"PARA": sa.Unpriced("PARA", None, "synthetic")}
    bars = run_backtest.fetch_bars_from_ib(["PARA", "AAPL"], years=1, unpriced=unpriced)
    assert bars["PARA"] == [] and bars["AAPL"]
    assert all(c.symbol != "PARA" for c in _FakeIB.requested)


def test_a_partly_unpriced_member_keeps_only_its_own_company_sessions(monkeypatch):
    """IR: IB's IR is Gardner Denver before 2020, so those bars are dropped."""
    from scripts import run_backtest

    fake = types.ModuleType("ib_insync")
    fake.IB = _FakeIB
    import ib_insync

    fake.Stock = ib_insync.Stock
    monkeypatch.setitem(sys.modules, "ib_insync", fake)
    before = date.today() - timedelta(days=100)
    unpriced = {"IR": sa.Unpriced("IR", before, "synthetic")}
    bars = run_backtest.fetch_bars_from_ib(["IR"], years=1, unpriced=unpriced)
    assert bars["IR"] and min(b["date"] for b in bars["IR"]) >= before


def test_drop_unpriced():
    bars = [{"date": date(2020, 3, 31)}, {"date": "2020-04-01"}]
    assert sa.drop_unpriced("IR", bars, {"IR": sa.Unpriced("IR", date(2020, 4, 1), "x")}) == bars[1:]
    assert sa.drop_unpriced("PARA", bars, {"PARA": sa.Unpriced("PARA", None, "x")}) == []
    assert sa.drop_unpriced("AAPL", bars, {"PARA": sa.Unpriced("PARA", None, "x")}) == bars


def test_the_paper_run_fetches_without_aliases():
    source = (REPO / "scripts" / "run_paper.py").read_text()
    call = source[source.index("bars_by_ticker = fetch_bars_from_ib("):]
    call = call[: call.index(")") + 1]
    assert "aliases" not in call and "unpriced" not in call


def test_the_backtest_entry_point_passes_both_tables():
    source = (REPO / "scripts" / "run_backtest.py").read_text()
    assert "aliases=BACKTEST_ALIASES" in source and "unpriced=BACKTEST_UNPRICED" in source


# --- the table ------------------------------------------------------------------------


@pytest.fixture(scope="module")
def membership() -> MembershipCalendar:
    return MembershipCalendar.from_json_file(str(MEMBERSHIP_SNAPSHOT_PATH))


def _snapshots() -> list[tuple[date, set[str]]]:
    import json

    raw = json.loads(Path(MEMBERSHIP_SNAPSHOT_PATH).read_text())["snapshots"]
    return sorted((date.fromisoformat(d), set(m)) for d, m in raw.items())


@pytest.mark.parametrize("alias", list(sa.BACKTEST_ALIASES.values()), ids=lambda a: a.ticker)
def test_each_alias_is_a_former_member_under_another_symbol(alias, membership):
    assert alias.ticker in membership.all_tickers()
    assert alias.ib_symbol != alias.ticker
    assert alias.ticker not in CONTRACT_CONID_OVERRIDES, "live overrides are a different table"
    assert alias.con_id > 0 and alias.long_name and alias.evidence


@pytest.mark.parametrize("alias", list(sa.BACKTEST_ALIASES.values()), ids=lambda a: a.ticker)
def test_each_alias_stops_where_the_membership_file_lists_the_new_ticker(alias):
    snaps = _snapshots()
    first_new = next((d for d, members in snaps if alias.ib_symbol in members), None)
    assert alias.until == first_new, (
        f"{alias.ticker}: until must be the first snapshot listing {alias.ib_symbol}"
    )
    if first_new is not None:
        # No snapshot lists both: the old ticker's days all precede `until`.
        assert not any(
            alias.ticker in members and alias.ib_symbol in members for _, members in snaps
        )


def test_unpriced_and_aliased_are_disjoint_and_unpriced_names_are_members(membership):
    assert not set(sa.BACKTEST_UNPRICED) & set(sa.BACKTEST_ALIASES)
    for ticker, entry in sa.BACKTEST_UNPRICED.items():
        assert entry.ticker == ticker and entry.evidence
        assert ticker in membership.all_tickers()
        if entry.before is not None:
            assert any(d >= entry.before and ticker in m for d, m in _snapshots()), (
                f"{ticker}: nothing left to price from {entry.before}"
            )
        assert ticker not in SP500_TOP100, "never block a name live trading prices"


def test_no_conid_is_priced_under_two_aliases_at_once():
    snaps = _snapshots()
    by_conid: dict[int, list[sa.BacktestAlias]] = {}
    for alias in sa.BACKTEST_ALIASES.values():
        by_conid.setdefault(alias.con_id, []).append(alias)
    for con_id, group in by_conid.items():
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                both = [d for d, m in snaps if a.ticker in m and b.ticker in m]
                assert not both, f"conId {con_id} would price {a.ticker} and {b.ticker} on {both[0]}"
