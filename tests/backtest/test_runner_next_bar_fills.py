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


D1, D2, D3, D4 = (
    date(2025, 1, 6),
    date(2025, 1, 7),
    date(2025, 1, 8),
    date(2025, 1, 9),
)


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


class TestDelistedMarking:
    def test_open_position_marks_at_last_close_not_entry_price(self):
        """A ticker that stops printing bars must not be frozen at cost."""
        bars_by_ticker = {
            "GONE": _bars([
                (D1, 100.0, 101.0, 99.0, 100.0),
                (D2, 100.0, 101.0, 99.0, 100.0),
                (D3, 60.0, 61.0, 59.0, 60.0),  # last print before it vanishes
            ]),
            "LIVE": _bars([
                (D1, 10.0, 11.0, 9.0, 10.0),
                (D2, 10.0, 11.0, 9.0, 10.0),
                (D3, 10.0, 11.0, 9.0, 10.0),
                (D4, 10.0, 11.0, 9.0, 10.0),
            ]),
        }
        signals = _signals({
            ("GONE", D1): {
                "action": "buy", "ticker": "GONE", "limit_price": 101.0,
                "quantity": 10, "sector": "Technology",
            },
        })

        result = _runner(capital=10_000.0).run(
            bars_by_ticker, signals, _approving_risk_engine(quantity=10)
        )

        # Bought 10 @ 100 on D2 -> 9_000 cash. On D4 GONE has no bar; it must
        # be marked at its D3 close of 60, not at the 100 entry price.
        assert result.portfolio_values[-1] == pytest.approx(9_000.0 + 10 * 60.0)
