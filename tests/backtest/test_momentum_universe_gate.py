"""The momentum sleeve must rank only its own, point-in-time universe.

`make_momentum_signals_fn` was handed the whole of `bars_by_ticker` — the union
of every sleeve's tickers — so its top-N could be thematic ETFs it was never
meant to hold. Once `resolve_backtest_universe` widened that set to every
historical index member, the top-N could also be filled with names that were
not members on the date, leaving the sleeve unable to trade anything.
"""
from __future__ import annotations

from datetime import date

from scripts.run_backtest import make_momentum_signals_fn
from shared.universe import MembershipCalendar


DAYS = [date(2025, 3, 3 + i) for i in range(5)]
D1, D2, D3, D4, D5 = DAYS

# Closes chosen so the 2-day lookback return at D5 ranks ARKK > AAPL > MSFT.
CLOSES = {
    "AAPL": [100.0, 100.0, 100.0, 130.0, 130.0],  # +30% over the lookback
    "MSFT": [100.0, 100.0, 100.0, 110.0, 120.0],  # +20%
    "ARKK": [100.0, 100.0, 100.0, 200.0, 200.0],  # +100%, but not an equity name
}


def _bars_by_ticker() -> dict[str, list[dict]]:
    return {
        ticker: [
            {
                "date": day, "open": close, "high": close * 1.01,
                "low": close * 0.99, "close": close, "volume": 1_000,
            }
            for day, close in zip(DAYS, closes)
        ]
        for ticker, closes in CLOSES.items()
    }


def _signals_fn(**overrides):
    kwargs = dict(
        bars_by_ticker=_bars_by_ticker(),
        top_n=1,
        lookback_days=2,
        position_size_pct=0.10,
        initial_capital=10_000.0,
    )
    kwargs.update(overrides)
    return make_momentum_signals_fn(**kwargs)


class TestEligibleTickers:
    def test_without_a_universe_the_sleeve_ranks_everything(self):
        """Current (unscoped) behaviour: the thematic ETF wins the ranking."""
        signals_fn = _signals_fn()
        bars = _bars_by_ticker()

        arkk = signals_fn("ARKK", bars["ARKK"])

        assert arkk is not None
        assert arkk["action"] == "buy"

    def test_ineligible_tickers_are_excluded_from_the_ranking(self):
        signals_fn = _signals_fn(eligible_tickers=["AAPL", "MSFT"])
        bars = _bars_by_ticker()

        assert signals_fn("ARKK", bars["ARKK"]) is None

        aapl = signals_fn("AAPL", bars["AAPL"])
        assert aapl is not None
        assert aapl["action"] == "buy"
        assert aapl["signals"]["rank"] == 1

    def test_bear_tickers_stay_rankable_when_scoped_to_the_sleeve(self):
        """Inverse ETFs are part of the momentum sleeve's universe by design."""
        signals_fn = _signals_fn(
            eligible_tickers=["AAPL", "MSFT", "ARKK"],
            bear_tickers={"ARKK"},
        )
        bars = _bars_by_ticker()

        arkk = signals_fn("ARKK", bars["ARKK"])
        assert arkk is not None
        assert arkk["action"] == "buy"


class TestPointInTimeRanking:
    def test_a_non_member_does_not_occupy_a_top_n_slot(self):
        """Otherwise the sleeve's whole allocation can be ranked into names it
        is not allowed to buy, and it silently trades nothing."""
        membership = MembershipCalendar({D1: ["AAPL", "MSFT"], D5: ["MSFT"]})
        signals_fn = _signals_fn(
            eligible_tickers=["AAPL", "MSFT"], membership=membership
        )
        bars = _bars_by_ticker()

        # AAPL out-returns MSFT but left the index effective D5.
        assert signals_fn("AAPL", bars["AAPL"]) is None

        msft = signals_fn("MSFT", bars["MSFT"])
        assert msft is not None
        assert msft["action"] == "buy"
        assert msft["signals"]["rank"] == 1

    def test_membership_is_applied_per_date_not_once(self):
        membership = MembershipCalendar({D1: ["AAPL", "MSFT"], D5: ["MSFT"]})
        signals_fn = _signals_fn(
            eligible_tickers=["AAPL", "MSFT"], membership=membership
        )
        bars = _bars_by_ticker()

        # On D4 AAPL is still a member and still the top name.
        aapl_d4 = signals_fn("AAPL", bars["AAPL"][:4])
        assert aapl_d4 is not None
        assert aapl_d4["action"] == "buy"


class TestSectorRotationScoping:
    """The sector sleeve holds SPDR sector ETFs, not whatever else is in the run.

    Same defect class as the momentum sleeve: it ranked all of
    `bars_by_ticker`, so widening the universe to every historical index member
    let it buy individual equities. A smoke run caught it holding a delisted
    common stock.
    """

    def test_ineligible_tickers_are_excluded_from_the_ranking(self):
        from scripts.run_backtest import make_sector_rotation_signals_fn

        signals_fn = make_sector_rotation_signals_fn(
            bars_by_ticker=_bars_by_ticker(),
            top_n=1,
            lookback_days=2,
            position_size_pct=0.20,
            initial_capital=10_000.0,
            eligible_tickers=["AAPL", "MSFT"],
        )
        bars = _bars_by_ticker()

        # ARKK has the strongest return but is not in this sleeve's universe.
        assert signals_fn("ARKK", bars["ARKK"]) is None
        top = signals_fn("AAPL", bars["AAPL"])
        assert top is not None
        assert top["action"] == "buy"
