"""Next-bar fills and point-in-time universe gating in the backtest runner.

Findings 4.1-4.3 of the 2026-08-06 implementation review: a decision taken on
``close[t]`` used to fill inside bar ``t`` (entries at that day's low, exits at
that day's open), and the universe was a static winner-preselected list.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pytest

from backtest.costs import CostModel
from backtest.runner import BacktestRunner
from backtest.simulator import SimulatedExecutor
from shared.universe import MembershipCalendar


def _bars(data: list[tuple]) -> list[dict]:
    return [
        {"date": d, "open": o, "high": h, "low": lo, "close": c}
        for d, o, h, lo, c in data
    ]


def _runner(capital: float = 100_000.0) -> BacktestRunner:
    executor = SimulatedExecutor(
        CostModel(slippage_bps=0, commission_per_share=0.0, commission_minimum=0.0)
    )
    return BacktestRunner(executor=executor, initial_capital=capital)


def _approving_risk_engine(quantity: float = 100) -> MagicMock:
    engine = MagicMock()
    engine.check_entry.return_value = MagicMock(
        approved=True, adjusted_quantity=quantity, reason="ok"
    )
    return engine


def _signals(by_date: dict) -> object:
    def signals_fn(ticker: str, bars_so_far: list[dict]) -> dict | None:
        return by_date.get((ticker, bars_so_far[-1]["date"]))

    return signals_fn


ALL_DAYS = [date(2025, 1, 6 + i) for i in range(8)]
D1, D2, D3, D4, D5, D6, D7, D8 = ALL_DAYS


class TestNextBarEntryFill:
    def test_entry_decided_on_close_fills_at_the_next_open(self):
        bars_by_ticker = {
            "AAPL": _bars([
                (D1, 150.0, 155.0, 148.0, 153.0),
                (D2, 152.0, 158.0, 151.0, 156.0),
                (D3, 156.0, 160.0, 154.0, 159.0),
            ])
        }
        signals = _signals({
            ("AAPL", D1): {
                "action": "buy", "ticker": "AAPL", "limit_price": 152.0,
                "quantity": 100, "sector": "Technology",
            },
            ("AAPL", D2): {"action": "sell", "ticker": "AAPL", "exit_reason": "target"},
        })

        result = _runner().run(bars_by_ticker, signals, _approving_risk_engine())

        assert len(result.trades) == 1
        trade = result.trades[0]
        # Decided on D1's close -> filled at D2's open (152, the limit is
        # touched at the open), never inside D1 at its 148.0 low.
        assert trade["entry_date"] == D2
        assert trade["entry_price"] == pytest.approx(152.0)
        # Exit decided on D2's close -> filled at D3's open, not D2's open.
        assert trade["exit_date"] == D3
        assert trade["exit_price"] == pytest.approx(156.0)
        assert trade["pnl"] == pytest.approx(400.0)

    def test_signal_on_the_final_bar_never_fills(self):
        bars_by_ticker = {
            "AAPL": _bars([
                (D1, 150.0, 155.0, 148.0, 153.0),
                (D2, 152.0, 158.0, 151.0, 156.0),
            ])
        }
        signals = _signals({
            ("AAPL", D2): {
                "action": "buy", "ticker": "AAPL", "limit_price": 158.0,
                "quantity": 100, "sector": "Technology",
            },
        })

        result = _runner().run(bars_by_ticker, signals, _approving_risk_engine())

        assert result.trades == []
        assert result.portfolio_values[-1] == pytest.approx(100_000.0)

    def test_unfilled_limit_expires_as_a_day_order(self):
        """An unreachable limit dies with the session — it does not rest for days."""
        bars_by_ticker = {
            "AAPL": _bars([
                (D1, 100.0, 101.0, 99.0, 100.0),
                (D2, 99.0, 100.0, 98.0, 99.0),   # limit 95 unreachable
                (D3, 94.0, 95.0, 90.0, 92.0),    # would have filled if still live
            ])
        }
        signals = _signals({
            ("AAPL", D1): {
                "action": "buy", "ticker": "AAPL", "limit_price": 95.0,
                "quantity": 100, "sector": "Technology",
            },
        })

        result = _runner().run(bars_by_ticker, signals, _approving_risk_engine())

        assert result.trades == []
        assert result.portfolio_values[-1] == pytest.approx(100_000.0)


class TestPendingEntryReservation:
    def test_same_day_pending_entries_are_reserved_against_cash(self):
        """Cash committed by an unfilled pending entry must not be double-spent.

        Under same-bar fills the first buy debited cash before the second buy's
        risk check. With next-open fills nothing has settled yet, so the
        pending notional has to be passed to the risk engine explicitly.
        """
        bars_by_ticker = {
            "AAA": _bars([(D1, 100.0, 101.0, 99.0, 100.0), (D2, 100.0, 101.0, 99.0, 100.0)]),
            "BBB": _bars([(D1, 50.0, 51.0, 49.0, 50.0), (D2, 50.0, 51.0, 49.0, 50.0)]),
        }
        signals = _signals({
            ("AAA", D1): {
                "action": "buy", "ticker": "AAA", "limit_price": 100.0,
                "quantity": 10, "sector": "Technology",
            },
            ("BBB", D1): {
                "action": "buy", "ticker": "BBB", "limit_price": 50.0,
                "quantity": 10, "sector": "Technology",
            },
        })
        engine = _approving_risk_engine(quantity=10)

        _runner().run(bars_by_ticker, signals, engine)

        reserved = [
            call.kwargs.get("reserved_notional")
            for call in engine.check_entry.call_args_list
        ]
        assert reserved[0] == pytest.approx(0.0)
        # AAA's 10 x $100 pending order is reserved when BBB is checked.
        assert reserved[1] == pytest.approx(1_000.0)


class TestPointInTimeUniverse:
    def test_buy_is_blocked_when_the_ticker_is_not_a_member_yet(self):
        bars_by_ticker = {
            "NEWCO": _bars([
                (D1, 100.0, 101.0, 99.0, 100.0),
                (D2, 100.0, 101.0, 99.0, 100.0),
                (D3, 100.0, 101.0, 99.0, 100.0),
            ])
        }
        signals = _signals({
            ("NEWCO", D1): {
                "action": "buy", "ticker": "NEWCO", "limit_price": 101.0,
                "quantity": 10, "sector": "Technology",
            },
        })
        membership = MembershipCalendar({D3: ["NEWCO"]})
        engine = _approving_risk_engine(quantity=10)

        result = _runner().run(
            bars_by_ticker, signals, engine, membership=membership
        )

        assert result.trades == []
        engine.check_entry.assert_not_called()

    def test_position_is_exited_when_the_ticker_leaves_the_universe(self):
        bars_by_ticker = {
            "OLDCO": _bars([
                (D1, 100.0, 101.0, 99.0, 100.0),
                (D2, 100.0, 101.0, 99.0, 100.0),
                (D3, 90.0, 95.0, 89.0, 92.0),
                (D4, 80.0, 85.0, 79.0, 82.0),
            ])
        }
        signals = _signals({
            ("OLDCO", D1): {
                "action": "buy", "ticker": "OLDCO", "limit_price": 101.0,
                "quantity": 10, "sector": "Technology",
            },
        })
        # OLDCO is dropped from the index effective D3.
        membership = MembershipCalendar({D1: ["OLDCO"], D3: []})

        result = _runner().run(
            bars_by_ticker,
            signals,
            _approving_risk_engine(quantity=10),
            membership=membership,
        )

        assert len(result.trades) == 1
        trade = result.trades[0]
        assert trade["exit_reason"] == "universe_removal"
        # Removal seen on D3's close -> exit fills at D4's open.
        assert trade["exit_date"] == D4
        assert trade["exit_price"] == pytest.approx(80.0)

    def test_always_members_are_tradable_without_index_membership(self):
        bars_by_ticker = {
            "XLK": _bars([
                (D1, 100.0, 101.0, 99.0, 100.0),
                (D2, 100.0, 101.0, 99.0, 100.0),
                (D3, 110.0, 111.0, 109.0, 110.0),
            ])
        }
        signals = _signals({
            ("XLK", D1): {
                "action": "buy", "ticker": "XLK", "limit_price": 101.0,
                "quantity": 10, "sector": "Technology",
            },
            ("XLK", D2): {"action": "sell", "ticker": "XLK", "exit_reason": "target"},
        })
        membership = MembershipCalendar({D1: ["AAPL"]}, always=["XLK"])

        result = _runner().run(
            bars_by_ticker,
            signals,
            _approving_risk_engine(quantity=10),
            membership=membership,
        )

        assert len(result.trades) == 1
        assert result.trades[0]["exit_reason"] == "target"


class TestDelisting:
    """A ticker whose bars simply stop is what a delisting looks like.

    Before this was fixed, such a position was never liquidated: it stayed out
    of `trades` (so win rate and expectancy were survivors-only — survivorship
    bias relocated from the universe into the trade stats) while its last close
    was credited to NAV forever.
    """

    def _bars_by_ticker(self, gone_last_date=D2, live_last_index=5):
        """GONE stops printing after ``gone_last_date``; LIVE keeps trading."""
        gone = [(D1, 100.0, 101.0, 99.0, 100.0)]
        if gone_last_date >= D2:
            gone.append((D2, 100.0, 101.0, 99.0, 60.0))
        if gone_last_date >= D3:
            gone.append((D3, 60.0, 61.0, 59.0, 60.0))
        return {
            "GONE": _bars(gone),
            "LIVE": _bars([
                (day, 10.0, 11.0, 9.0, 10.0) for day in ALL_DAYS[:live_last_index]
            ]),
        }

    def _buy_gone_on_d1(self):
        return _signals({
            ("GONE", D1): {
                "action": "buy", "ticker": "GONE", "limit_price": 101.0,
                "quantity": 10, "sector": "Technology",
            },
        })

    def test_position_in_a_ticker_whose_bars_stop_is_written_off(self):
        """The reviewer's repro: entry at 100 on D2, last close 60, bars stop.

        Previously: `trades == []` and 9_600 of NAV carried to the end of the
        run with the position never closing.
        """
        result = _runner(capital=10_000.0).run(
            self._bars_by_ticker(),
            self._buy_gone_on_d1(),
            _approving_risk_engine(quantity=10),
            delisting_stale_sessions=1,
        )

        assert len(result.trades) == 1
        trade = result.trades[0]
        assert trade["ticker"] == "GONE"
        assert trade["exit_reason"] == "delisted"
        # One session after its last print, it is written off at that close.
        assert trade["exit_date"] == D3
        assert trade["exit_price"] == pytest.approx(60.0)
        assert trade["pnl"] == pytest.approx(10 * 60.0 - 10 * 100.0)
        assert result.metrics["total_trades"] == 1

    def test_written_off_position_stops_contributing_phantom_nav(self):
        """With exit slippage the write-off price differs from the stale mark."""
        executor = SimulatedExecutor(
            CostModel(
                slippage_bps=50, commission_per_share=0.0, commission_minimum=0.0
            )
        )
        runner = BacktestRunner(executor=executor, initial_capital=10_000.0)

        result = runner.run(
            self._bars_by_ticker(),
            self._buy_gone_on_d1(),
            _approving_risk_engine(quantity=10),
            delisting_stale_sessions=1,
        )

        # Entry at D2's open of 100 with 50 bps -> 100.5; write-off at the 60.0
        # last close less 50 bps -> 59.7. Marking the stale position instead
        # would have left NAV at 9_000-ish + 10 x 60.
        expected_cash = 10_000.0 - 10 * 100.5 + 10 * 59.7
        assert result.portfolio_values[-1] == pytest.approx(expected_cash)
        assert result.trades[0]["exit_price"] == pytest.approx(59.7)

    def test_write_off_keeps_a_queued_removal_reason(self):
        """Membership removal is the decision; the delisting is only why it filled.

        Also proves the membership gate runs for a held ticker that did not
        print a bar that day — GONE stops after D3 but is dropped from the
        index effective D5.
        """
        membership = MembershipCalendar({D1: ["GONE", "LIVE"], D5: ["LIVE"]})

        result = _runner(capital=10_000.0).run(
            self._bars_by_ticker(gone_last_date=D3, live_last_index=8),
            self._buy_gone_on_d1(),
            _approving_risk_engine(quantity=10),
            membership=membership,
            delisting_stale_sessions=3,
        )

        assert len(result.trades) == 1
        trade = result.trades[0]
        assert trade["exit_reason"] == "universe_removal"
        # Queued on D5 (no bar that day), written off once 3 stale sessions
        # have passed since the D3 print.
        assert trade["exit_date"] == D6

    def test_open_position_marks_at_last_close_while_still_within_the_grace_window(self):
        """Until the write-off trips, the last close is the honest mark."""
        result = _runner(capital=10_000.0).run(
            self._bars_by_ticker(gone_last_date=D3, live_last_index=4),
            self._buy_gone_on_d1(),
            _approving_risk_engine(quantity=10),
            delisting_stale_sessions=5,
        )

        # Bought 10 @ 100 on D2 -> 9_000 cash. D4 is one stale session, well
        # inside the grace window, so GONE marks at its D3 close of 60 rather
        # than at the 100 entry price.
        assert result.trades == []
        assert result.portfolio_values[-1] == pytest.approx(9_000.0 + 10 * 60.0)


class TestDayOrderExpiry:
    def test_pending_entry_expires_even_when_its_ticker_does_not_print(self):
        """A day order cannot outlive its session, printing or not.

        A queued entry that waited for its ticker's next bar kept its notional
        inside `reserved_notional` indefinitely, permanently shrinking the
        sleeve's buying power.
        """
        bars_by_ticker = {
            # AAA has no bar on D2 at all.
            "AAA": _bars([
                (D1, 100.0, 101.0, 99.0, 100.0),
                (D3, 95.0, 96.0, 94.0, 95.0),
                (D4, 95.0, 96.0, 94.0, 95.0),
            ]),
            "BBB": _bars([
                (D1, 50.0, 51.0, 49.0, 50.0),
                (D2, 50.0, 51.0, 49.0, 50.0),
                (D3, 50.0, 51.0, 49.0, 50.0),
                (D4, 50.0, 51.0, 49.0, 50.0),
            ]),
        }
        signals = _signals({
            ("AAA", D1): {
                "action": "buy", "ticker": "AAA", "limit_price": 100.0,
                "quantity": 10, "sector": "Technology",
            },
            ("BBB", D2): {
                "action": "buy", "ticker": "BBB", "limit_price": 50.0,
                "quantity": 10, "sector": "Technology",
            },
            # If AAA's expired order had rested and filled on D3, this would
            # close it out on D4 and show up in trades.
            ("AAA", D3): {"action": "sell", "ticker": "AAA", "exit_reason": "target"},
        })
        engine = _approving_risk_engine(quantity=10)

        result = _runner(capital=10_000.0).run(bars_by_ticker, signals, engine)

        reserved = [
            call.kwargs.get("reserved_notional")
            for call in engine.check_entry.call_args_list
        ]
        # AAA's D1 order died with the D2 session it could not trade in, so
        # BBB's D2 risk check sees nothing reserved.
        assert reserved == [pytest.approx(0.0), pytest.approx(0.0)]
        assert [t["ticker"] for t in result.trades] == []
