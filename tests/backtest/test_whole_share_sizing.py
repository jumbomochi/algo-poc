"""Whole-share sizing on the backtest path (KAN-34).

The backtest has always sized positions fractionally, while live execution
truncates to whole shares and skips the order when that rounds to zero
(``services/execution/ib_executor.py`` ``_effective_quantity``). At $100k of
capital the difference is noise; at Rung-0 capital (~USD 3.7k split six ways)
a position budget of $34–119 cannot buy one share of most S&P names, so the
question stops being "how much drag?" and becomes "does the position open at
all?".

These tests pin both halves of that: the flag is off by default and leaves
every existing invocation byte-identical, and when on it truncates toward zero
and records what it dropped instead of failing silently.
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta
from pathlib import Path

from backtest.runner import BacktestResult
from scripts import run_backtest
from scripts.run_backtest import (
    PortfolioConfig,
    SkipLedger,
    _entry_quantity,
    make_earnings_drift_signals_fn,
    make_tail_risk_hedge_signals_fn,
    save_multi_portfolio_results,
)
from services.risk_management.engine import RiskEngine


PRICE = 150.0


def _make_bars(days: int = 100, price: float = PRICE) -> list[dict]:
    return [
        {
            "date": date(2024, 1, 1) + timedelta(days=d),
            "open": price,
            "high": price + 1,
            "low": price - 1,
            "close": price,
            "volume": 50_000,
        }
        for d in range(days)
    ]


def _earnings_lookup():
    from scripts.fetch_earnings import build_earnings_lookup

    cache = {
        "AAPL": [
            {"earnings_date": "2024-02-01", "actual_eps": 2.20,
             "estimate_eps": 2.00, "surprise_pct": 10.0},
        ],
        "MSFT": [
            {"earnings_date": "2024-02-01", "actual_eps": 2.20,
             "estimate_eps": 2.00, "surprise_pct": 10.0},
        ],
    }
    return build_earnings_lookup(cache, window_days=2)


def _bull_regime() -> dict:
    return {date(2024, 1, 1) + timedelta(days=i): "bull" for i in range(120)}


# --- AC1: flag off is byte-identical to today -------------------------------


def test_flag_off_preserves_fractional_quantity():
    """Without --whole-shares, quantities match the pre-KAN-34 formula exactly."""
    signals_fn = make_earnings_drift_signals_fn(
        earnings_lookup=_earnings_lookup(),
        surprise_threshold_pct=5.0,
        position_size_pct=0.08,
        initial_capital=5_000,
    )

    result = signals_fn("AAPL", _make_bars()[:32])

    assert result is not None and result["action"] == "buy"
    expected = round(max(0.0001, 5_000 * 0.08 / PRICE), 4)
    assert result["quantity"] == expected
    assert result["quantity"] == 2.6667


def test_flag_off_preserves_fractional_quantity_on_weighted_site():
    """The regime-weighted sizing site is fractional too when the flag is off."""
    signals_fn = make_tail_risk_hedge_signals_fn(
        regime_by_date=_bull_regime(),
        position_size_pct=0.25,
        initial_capital=474.71,
    )

    result = signals_fn("GLD", _make_bars(10))

    assert result is not None and result["action"] == "buy"
    expected = round(max(0.0001, 474.71 * 0.25 * 0.50 / PRICE), 4)
    assert result["quantity"] == expected


def test_every_sizing_site_routes_through_the_helper():
    """All nine sizing sites use ``_entry_quantity``.

    A site left on the inline ``round(max(0.0001, ...))`` form would silently
    ignore --whole-shares, making the flag's behaviour depend on which factory
    a sleeve happens to use.
    """
    source = Path(run_backtest.__file__).read_text()

    # The one surviving inline formula is inside ``_entry_quantity`` itself.
    assert source.count("round(max(0.0001,") == 1
    call_sites = re.findall(r"_entry_quantity\(", source)
    # nine call sites plus the definition
    assert len(call_sites) == 10, f"expected 9 call sites, found {len(call_sites) - 1}"


# --- AC1: flag on truncates toward zero -------------------------------------


def test_whole_shares_truncates_toward_zero():
    signals_fn = make_earnings_drift_signals_fn(
        earnings_lookup=_earnings_lookup(),
        surprise_threshold_pct=5.0,
        position_size_pct=0.08,
        initial_capital=5_000,
        whole_shares=True,
    )

    result = signals_fn("AAPL", _make_bars()[:32])

    assert result is not None
    assert result["quantity"] == 2.0  # 2.6667 truncated, not rounded


def test_whole_shares_truncates_the_weighted_site():
    signals_fn = make_tail_risk_hedge_signals_fn(
        regime_by_date=_bull_regime(),
        position_size_pct=0.25,
        initial_capital=12_830,
        whole_shares=True,
    )

    result = signals_fn("GLD", _make_bars(10))

    assert result is not None
    assert result["quantity"] == 10.0  # 12830 * 0.25 * 0.50 / 150 = 10.69


def test_entry_quantity_helper_modes():
    """The helper itself: fractional preserved off, truncated on."""
    fractional = _entry_quantity(
        initial_capital=711.51,
        position_size_pct=0.08,
        current_price=PRICE,
        whole_shares=False,
        skip_ledger=None,
        ticker="AAPL",
        current_date=date(2024, 2, 1),
    )
    assert fractional == round(max(0.0001, 711.51 * 0.08 / PRICE), 4)

    truncated = _entry_quantity(
        initial_capital=711.51,
        position_size_pct=0.08,
        current_price=PRICE,
        whole_shares=True,
        skip_ledger=None,
        ticker="AAPL",
        current_date=date(2024, 2, 1),
    )
    assert truncated is None  # 0.3795 shares -> unfillable


# --- AC2: a zero-share signal is skipped, and the skip is recorded ----------


def test_zero_whole_share_quantity_produces_no_trade():
    """A Rung-0 earnings_drift budget cannot buy one share at $150."""
    ledger = SkipLedger()
    signals_fn = make_earnings_drift_signals_fn(
        earnings_lookup=_earnings_lookup(),
        surprise_threshold_pct=5.0,
        position_size_pct=0.08,
        initial_capital=711.51,
        whole_shares=True,
        skip_ledger=ledger,
    )

    assert signals_fn("AAPL", _make_bars()[:32]) is None

    payload = ledger.to_dict()
    assert payload["count"] == 1
    entry = payload["signals"][0]
    assert entry == {
        "ticker": "AAPL",
        "date": "2024-02-01",
        "fractional_quantity": round(711.51 * 0.08 / PRICE, 4),
        "price": PRICE,
    }


def test_skip_ledger_records_one_entry_per_occurrence():
    """One entry per rejected occurrence, not per unique ticker (AC 4d)."""
    ledger = SkipLedger()
    signals_fn = make_earnings_drift_signals_fn(
        earnings_lookup=_earnings_lookup(),
        surprise_threshold_pct=5.0,
        position_size_pct=0.08,
        initial_capital=711.51,
        whole_shares=True,
        skip_ledger=ledger,
    )

    bars = _make_bars()
    # AAPL rejected on two consecutive days inside the earnings window, MSFT once.
    assert signals_fn("AAPL", bars[:32]) is None
    assert signals_fn("AAPL", bars[:33]) is None
    assert signals_fn("MSFT", bars[:32]) is None

    payload = ledger.to_dict()
    assert payload["count"] == 3
    assert [s["ticker"] for s in payload["signals"]] == ["AAPL", "AAPL", "MSFT"]
    assert [s["date"] for s in payload["signals"]] == [
        "2024-02-01", "2024-02-02", "2024-02-01",
    ]


def test_no_skip_recorded_when_flag_is_off():
    ledger = SkipLedger()
    signals_fn = make_earnings_drift_signals_fn(
        earnings_lookup=_earnings_lookup(),
        surprise_threshold_pct=5.0,
        position_size_pct=0.08,
        initial_capital=711.51,
        whole_shares=False,
        skip_ledger=ledger,
    )

    result = signals_fn("AAPL", _make_bars()[:32])

    assert result is not None
    assert ledger.to_dict() == {"count": 0, "signals": []}


def test_ledger_counts_every_entry_signal_it_sized():
    """The skip count needs a denominator, or "300 skips" means nothing.

    Every entry signal that reaches sizing is counted, in both modes — so the
    memo can say *N of M* were unfillable rather than just N.
    """
    ledger = SkipLedger()
    signals_fn = make_earnings_drift_signals_fn(
        earnings_lookup=_earnings_lookup(),
        surprise_threshold_pct=5.0,
        position_size_pct=0.08,
        initial_capital=711.51,
        whole_shares=True,
        skip_ledger=ledger,
    )

    bars = _make_bars()
    assert signals_fn("AAPL", bars[:32]) is None
    assert signals_fn("MSFT", bars[:32]) is None

    assert ledger.sized == 2
    assert ledger.to_dict()["count"] == 2

    # A fillable signal counts toward the denominator but not the skips.
    fillable = SkipLedger()
    rich_fn = make_earnings_drift_signals_fn(
        earnings_lookup=_earnings_lookup(),
        surprise_threshold_pct=5.0,
        position_size_pct=0.08,
        initial_capital=100_000,
        whole_shares=True,
        skip_ledger=fillable,
    )
    assert rich_fn("AAPL", bars[:32]) is not None
    assert fillable.sized == 1
    assert fillable.to_dict()["count"] == 0

    # The pinned schema stays exactly two keys — the denominator rides beside
    # it in the artifact, not inside it.
    assert set(fillable.to_dict()) == {"count", "signals"}


# --- AC2: risk-engine downsizing is truncated too ---------------------------


def _run_one_buy(quantity: float, *, whole_shares: bool, ledger: SkipLedger | None,
                 entry_limit_pct: float = 50.0):
    """Drive one approved buy through the runner and return the result."""
    from backtest.costs import CostModel
    from backtest.runner import BacktestRunner
    from backtest.simulator import SimulatedExecutor

    def signals_fn(ticker: str, bars: list[dict]) -> dict | None:
        if len(bars) != 5:
            return None
        return {
            "action": "buy",
            "ticker": ticker,
            "limit_price": PRICE,
            "quantity": quantity,
            "sector": "Unknown",
            "signals": {"strategy": "test"},
        }

    runner = BacktestRunner(
        executor=SimulatedExecutor(CostModel(slippage_bps=0, commission_per_share=0.0)),
        initial_capital=1_000,
        whole_shares=whole_shares,
        skip_ledger=ledger,
    )
    return runner.run(
        bars_by_ticker={"AAPL": _make_bars(30)},
        signals_fn=signals_fn,
        risk_engine=RiskEngine(position_entry_limit_pct=entry_limit_pct),
    )


def test_risk_engine_downsizing_is_truncated_to_whole_shares():
    """The order the runner places must be whole, not just the signal.

    ``RiskEngine.check_entry`` returns an ``adjusted_quantity`` floored to four
    decimal places when it caps an entry. Live that fractional quantity meets
    ``ib_executor._effective_quantity`` and is truncated (or skipped) at the
    last moment; the backtest has to do the same or it books lots no broker
    would ever fill.
    """
    # 5 shares of a $150 name is $750 on $1,000 of capital; a 50% entry limit
    # caps it, and the cap lands on a fraction.
    result = _run_one_buy(5.0, whole_shares=True, ledger=None)

    held = result.trades + result.open_positions
    assert held, "expected the capped entry to still open"
    for lot in held:
        assert lot["quantity"] == int(lot["quantity"]), lot


def test_risk_downsizing_below_one_share_is_skipped_and_recorded():
    ledger = SkipLedger()
    # A 10% entry limit on $1,000 caps the order at $100 — two thirds of a
    # $150 share.
    result = _run_one_buy(5.0, whole_shares=True, ledger=ledger, entry_limit_pct=10.0)

    assert result.trades == []
    assert result.open_positions == []
    payload = ledger.to_dict()
    assert payload["count"] == 1
    assert payload["signals"][0]["ticker"] == "AAPL"
    assert payload["signals"][0]["fractional_quantity"] < 1.0


def test_risk_downsizing_left_fractional_when_flag_off():
    """Default behaviour is untouched: fractional lots still get booked."""
    result = _run_one_buy(5.0, whole_shares=False, ledger=None)

    held = result.trades + result.open_positions
    assert held
    assert any(lot["quantity"] != int(lot["quantity"]) for lot in held)


# --- AC4b: positions still open at the end are counted, not booked ----------


def test_open_positions_at_end_are_reported_separately():
    """``trades`` holds only closed round-trips; the rest must still be visible.

    The round-trip commission drag is averaged over closed trades only, so a
    half-finished trade cannot dilute it — which only works if the leftovers
    are counted somewhere rather than vanishing.
    """
    from backtest.costs import CostModel
    from backtest.runner import BacktestRunner
    from backtest.simulator import SimulatedExecutor

    bars = _make_bars(30)
    bars_by_ticker = {"AAPL": bars}

    def signals_fn(ticker: str, bars: list[dict]) -> dict | None:
        # Buy once on the 5th bar and never sell.
        if len(bars) != 5:
            return None
        return {
            "action": "buy",
            "ticker": ticker,
            "limit_price": PRICE,
            "quantity": 10.0,
            "sector": "Unknown",
            "signals": {"strategy": "test"},
        }

    runner = BacktestRunner(
        executor=SimulatedExecutor(CostModel(slippage_bps=0, commission_per_share=0.0)),
        initial_capital=100_000,
    )
    result = runner.run(signals_fn=signals_fn, bars_by_ticker=bars_by_ticker,
                        risk_engine=RiskEngine(position_entry_limit_pct=50.0))

    assert result.trades == []
    assert len(result.open_positions) == 1
    assert result.open_positions[0]["ticker"] == "AAPL"
    assert result.open_positions[0]["quantity"] == 10.0


# --- AC3: the skips reach the artifact --------------------------------------


def test_skipped_signals_present_per_sleeve_in_artifact(tmp_path):
    ledger = SkipLedger()
    ledger.record(
        ticker="AAPL",
        current_date=date(2024, 2, 1),
        fractional_quantity=0.3795,
        price=PRICE,
    )

    results = {
        "earnings_drift": BacktestResult(
            trades=[], portfolio_values=[711.51], dates=[date(2024, 2, 1)], metrics={},
        ),
        "momentum": BacktestResult(
            trades=[], portfolio_values=[853.96], dates=[date(2024, 2, 1)], metrics={},
        ),
    }
    portfolio_configs = {
        name: PortfolioConfig(
            name=name,
            capital=1000.0,
            signals_fn=lambda ticker, bars: None,
            risk_engine=RiskEngine(position_entry_limit_pct=10.0),
        )
        for name in results
    }

    path = save_multi_portfolio_results(
        config={"initial_capital": 3700, "whole_shares": True},
        results=results,
        portfolio_configs=portfolio_configs,
        aggregate={"metrics": {}, "portfolio_values": []},
        bars={},
        output_dir=str(tmp_path),
        skipped_signals={"earnings_drift": ledger.to_dict()},
        entry_signals_sized={"earnings_drift": 1},
    )

    payload = json.loads(Path(path).read_text())
    ed = payload["portfolios"]["earnings_drift"]["skipped_signals"]
    assert ed["count"] == 1
    assert ed["signals"][0]["ticker"] == "AAPL"
    assert ed["signals"][0]["date"] == "2024-02-01"
    assert payload["portfolios"]["earnings_drift"]["entry_signals_sized"] == 1

    # A sleeve that skipped nothing still reports the block, so "no skips" and
    # "not measured" are distinguishable.
    assert payload["portfolios"]["momentum"]["skipped_signals"] == {
        "count": 0, "signals": [],
    }
