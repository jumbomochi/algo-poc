# Phase 5: Live Trading Preparation

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Bridge the 8 backtest strategies into a paper trading mode by adding strategy tagging to stream messages, a paper trading state manager for position persistence, and a daily paper trading runner script.

**Architecture:** Rather than refactoring the microservice pipeline (which is designed for real-time intraday execution), Phase 5 creates a simpler `scripts/run_paper.py` that reuses the exact same signal functions from the backtest. It fetches the latest bars daily, runs all 8 signal functions, and submits resulting orders via IB. State (positions, equity, trade history) persists to a JSON file between runs. This approach gets paper trading working immediately while the microservice pipeline can be enhanced later for intraday execution.

**Tech Stack:** Python, ib_insync (existing), pytest

---

## Context

### Current state

- 8 signal function factories in `scripts/run_backtest.py` producing `(ticker, bars) -> dict | None`
- `fetch_bars_from_ib()` fetches historical bars from IB Gateway
- `RiskEngine` per strategy with independent limits
- `SimulatedExecutor` for backtest fills — needs real IB executor for paper
- Stream messages (`ApprovedOrderMessage`, `FillMessage`) have no `portfolio` field

### What we're building

1. **Strategy tagging** — Add `portfolio` field to `ApprovedOrderMessage` and `FillMessage` for attribution
2. **`PaperTradingState`** — JSON-persisted state tracking positions, equity, and trade history per portfolio
3. **`scripts/run_paper.py`** — Daily runner that fetches bars, runs signals, submits orders, updates state
4. **Tests** for state persistence and strategy tagging

### What we're NOT building (deferred)

- Real-time intraday signal processing (Phase 5b)
- Notification channel implementations (Slack/SMS/email)
- Database persistence for positions
- IB connection recovery/reconnect
- Microservice pipeline refactoring for multi-portfolio

---

## Task 1: Add `portfolio` Field to Stream Messages

**Files:**
- Modify: `shared/schemas/messages.py`
- Modify: `tests/shared/test_messages.py` (if exists, or create)

### Step 1: Write the failing test

Create or update `tests/shared/test_message_portfolio_field.py`:

```python
from __future__ import annotations

from datetime import datetime, timezone

from shared.schemas.messages import ApprovedOrderMessage, FillMessage


def test_approved_order_has_portfolio_field():
    """ApprovedOrderMessage should accept an optional portfolio field."""
    order = ApprovedOrderMessage(
        ticker="AAPL",
        timestamp=datetime.now(timezone.utc),
        action="buy",
        quantity=10,
        order_type="limit",
        limit_price=150.0,
        recommendation_id="test-123",
        portfolio="momentum",
    )
    assert order.portfolio == "momentum"

    # Stream round-trip preserves portfolio
    d = order.to_stream_dict()
    restored = ApprovedOrderMessage.from_stream_dict(d)
    assert restored.portfolio == "momentum"


def test_approved_order_portfolio_defaults_to_none():
    """Portfolio field should be optional and default to None."""
    order = ApprovedOrderMessage(
        ticker="AAPL",
        timestamp=datetime.now(timezone.utc),
        action="buy",
        quantity=10,
        order_type="limit",
        limit_price=150.0,
        recommendation_id="test-123",
    )
    assert order.portfolio is None


def test_fill_message_has_portfolio_field():
    """FillMessage should accept an optional portfolio field."""
    fill = FillMessage(
        ticker="AAPL",
        timestamp=datetime.now(timezone.utc),
        side="buy",
        quantity=10,
        fill_price=150.0,
        commission=0.05,
        recommendation_id="test-123",
        order_id="order-456",
        portfolio="mean_reversion",
    )
    assert fill.portfolio == "mean_reversion"

    # Stream round-trip preserves portfolio
    d = fill.to_stream_dict()
    restored = FillMessage.from_stream_dict(d)
    assert restored.portfolio == "mean_reversion"
```

### Step 2: Run tests to verify they fail

Run: `pytest tests/shared/test_message_portfolio_field.py -v`
Expected: FAIL — `portfolio` field not recognized

### Step 3: Write implementation

In `shared/schemas/messages.py`, add `portfolio: str | None = None` to both:

`ApprovedOrderMessage` (after `risk_adjustments`):
```python
class ApprovedOrderMessage(StreamSerializable):
    ticker: str
    timestamp: datetime
    action: Literal["buy", "sell"]
    quantity: int
    order_type: Literal["limit", "market"]
    limit_price: float | None = None
    recommendation_id: str
    risk_adjustments: dict[str, Any] = Field(default_factory=dict)
    portfolio: str | None = None
```

`FillMessage` (after `order_id`):
```python
class FillMessage(StreamSerializable):
    ticker: str
    timestamp: datetime
    side: Literal["buy", "sell"]
    quantity: int
    fill_price: float
    commission: float
    recommendation_id: str
    order_id: str
    portfolio: str | None = None
```

### Step 4: Run tests

Run: `pytest tests/shared/test_message_portfolio_field.py -v`
Expected: 3 passed

Run: `pytest tests/ -v --tb=short`
Expected: All existing tests still pass (the new field is optional with default None)

### Step 5: Commit

```bash
git add shared/schemas/messages.py tests/shared/test_message_portfolio_field.py
git commit -m "feat: add optional portfolio field to ApprovedOrderMessage and FillMessage"
```

---

## Task 2: Paper Trading State Manager

**Files:**
- Create: `scripts/paper_state.py`
- Create: `tests/backtest/test_paper_state.py`

### Step 1: Write the failing tests

Create `tests/backtest/test_paper_state.py`:

```python
from __future__ import annotations

import os
import tempfile
from datetime import date

from scripts.paper_state import PaperTradingState


def test_initial_state_has_correct_structure():
    """New state should have empty portfolios with correct capital."""
    state = PaperTradingState.create_new(
        portfolio_capitals={"momentum": 20_000, "mr": 14_000},
        state_path="/tmp/nonexistent.json",
    )
    assert state.portfolios["momentum"]["capital"] == 20_000
    assert state.portfolios["momentum"]["positions"] == {}
    assert state.portfolios["momentum"]["trades"] == []
    assert state.portfolios["mr"]["capital"] == 14_000


def test_save_and_load_round_trip():
    """State should survive save/load round-trip."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "state.json")

        state = PaperTradingState.create_new(
            portfolio_capitals={"momentum": 20_000},
            state_path=path,
        )
        state.record_fill(
            portfolio="momentum",
            ticker="AAPL",
            action="buy",
            quantity=10,
            price=150.0,
            fill_date=date(2024, 1, 15),
        )
        state.save()

        loaded = PaperTradingState.load(path)
        assert "AAPL" in loaded.portfolios["momentum"]["positions"]
        pos = loaded.portfolios["momentum"]["positions"]["AAPL"]
        assert pos["quantity"] == 10
        assert pos["entry_price"] == 150.0


def test_record_buy_creates_position():
    """Recording a buy fill should create or add to a position."""
    state = PaperTradingState.create_new(
        portfolio_capitals={"mr": 10_000},
        state_path="/tmp/test.json",
    )
    state.record_fill("mr", "AAPL", "buy", 10, 150.0, date(2024, 1, 15))

    pos = state.portfolios["mr"]["positions"]["AAPL"]
    assert pos["quantity"] == 10
    assert pos["entry_price"] == 150.0
    assert pos["peak_price"] == 150.0


def test_record_sell_removes_position():
    """Recording a sell fill should remove the position and create a trade record."""
    state = PaperTradingState.create_new(
        portfolio_capitals={"mr": 10_000},
        state_path="/tmp/test.json",
    )
    state.record_fill("mr", "AAPL", "buy", 10, 150.0, date(2024, 1, 1))
    state.record_fill("mr", "AAPL", "sell", 10, 160.0, date(2024, 1, 15))

    assert "AAPL" not in state.portfolios["mr"]["positions"]
    assert len(state.portfolios["mr"]["trades"]) == 1
    trade = state.portfolios["mr"]["trades"][0]
    assert trade["ticker"] == "AAPL"
    assert trade["pnl"] == 100.0  # (160 - 150) * 10


def test_update_peak_prices():
    """update_peak_prices should update peak for held positions."""
    state = PaperTradingState.create_new(
        portfolio_capitals={"mr": 10_000},
        state_path="/tmp/test.json",
    )
    state.record_fill("mr", "AAPL", "buy", 10, 150.0, date(2024, 1, 1))

    current_prices = {"AAPL": 160.0}
    state.update_peak_prices("mr", current_prices)

    pos = state.portfolios["mr"]["positions"]["AAPL"]
    assert pos["peak_price"] == 160.0
```

### Step 2: Run tests to verify they fail

Run: `pytest tests/backtest/test_paper_state.py -v`
Expected: FAIL — `ModuleNotFoundError`

### Step 3: Write implementation

Create `scripts/paper_state.py`:

```python
#!/usr/bin/env python3
"""Paper trading state persistence.

Manages position tracking, trade history, and equity across
multiple portfolios. State is persisted to a JSON file between
daily runs.
"""
from __future__ import annotations

import json
import os
from datetime import date
from typing import Any


class PaperTradingState:
    """Manages paper trading state for multiple portfolios.

    Each portfolio tracks:
    - capital: initial capital allocation
    - cash: current cash available
    - positions: {ticker: {quantity, entry_price, entry_date, peak_price}}
    - trades: list of completed trades with P&L
    - equity_history: list of {date, equity} snapshots
    """

    def __init__(self, data: dict[str, Any], state_path: str) -> None:
        self._data = data
        self._state_path = state_path

    @property
    def portfolios(self) -> dict[str, Any]:
        return self._data["portfolios"]

    @classmethod
    def create_new(
        cls,
        portfolio_capitals: dict[str, float],
        state_path: str,
    ) -> PaperTradingState:
        """Create a fresh state with initial capital per portfolio."""
        data: dict[str, Any] = {"portfolios": {}}
        for name, capital in portfolio_capitals.items():
            data["portfolios"][name] = {
                "capital": capital,
                "cash": capital,
                "positions": {},
                "trades": [],
                "equity_history": [],
            }
        return cls(data, state_path)

    @classmethod
    def load(cls, state_path: str) -> PaperTradingState:
        """Load state from JSON file."""
        with open(state_path) as f:
            data = json.load(f)
        return cls(data, state_path)

    def save(self) -> None:
        """Save state to JSON file."""
        os.makedirs(os.path.dirname(self._state_path) or ".", exist_ok=True)
        with open(self._state_path, "w") as f:
            json.dump(self._data, f, indent=2, default=str)

    def record_fill(
        self,
        portfolio: str,
        ticker: str,
        action: str,
        quantity: int,
        price: float,
        fill_date: date,
    ) -> None:
        """Record a fill (buy or sell) for a portfolio."""
        pf = self.portfolios[portfolio]

        if action == "buy":
            if ticker in pf["positions"]:
                # Average into existing position
                pos = pf["positions"][ticker]
                old_qty = pos["quantity"]
                old_price = pos["entry_price"]
                new_qty = old_qty + quantity
                pos["entry_price"] = (old_price * old_qty + price * quantity) / new_qty
                pos["quantity"] = new_qty
                pos["peak_price"] = max(pos["peak_price"], price)
            else:
                pf["positions"][ticker] = {
                    "quantity": quantity,
                    "entry_price": price,
                    "entry_date": str(fill_date),
                    "peak_price": price,
                }
            pf["cash"] -= price * quantity

        elif action == "sell":
            pos = pf["positions"].get(ticker)
            if pos:
                pnl = (price - pos["entry_price"]) * quantity
                pf["trades"].append({
                    "ticker": ticker,
                    "entry_price": pos["entry_price"],
                    "exit_price": price,
                    "quantity": quantity,
                    "entry_date": pos.get("entry_date", ""),
                    "exit_date": str(fill_date),
                    "pnl": pnl,
                    "portfolio": portfolio,
                })
                pf["cash"] += price * quantity
                del pf["positions"][ticker]

    def update_peak_prices(
        self, portfolio: str, current_prices: dict[str, float]
    ) -> None:
        """Update peak prices for all held positions."""
        for ticker, pos in self.portfolios[portfolio]["positions"].items():
            if ticker in current_prices:
                pos["peak_price"] = max(pos["peak_price"], current_prices[ticker])

    def compute_equity(
        self, portfolio: str, current_prices: dict[str, float]
    ) -> float:
        """Compute current equity (cash + market value of positions)."""
        pf = self.portfolios[portfolio]
        market_value = sum(
            pos["quantity"] * current_prices.get(ticker, pos["entry_price"])
            for ticker, pos in pf["positions"].items()
        )
        return pf["cash"] + market_value
```

### Step 4: Run tests

Run: `pytest tests/backtest/test_paper_state.py -v`
Expected: 5 passed

### Step 5: Commit

```bash
git add scripts/paper_state.py tests/backtest/test_paper_state.py
git commit -m "feat: add paper trading state manager with JSON persistence"
```

---

## Task 3: Paper Trading Runner Script

**Files:**
- Create: `scripts/run_paper.py`

### Step 1: Write the runner script

Create `scripts/run_paper.py`:

```python
#!/usr/bin/env python3
"""Daily paper trading runner.

Reuses the exact same signal functions from the backtest system.
Fetches latest bars from IB Gateway, runs all 8 signal functions,
and submits resulting orders. State persists to JSON between runs.

Usage:
    python scripts/run_paper.py [--capital N] [--state-file PATH]
    python scripts/run_paper.py --init  # Initialize fresh state
    python scripts/run_paper.py --status  # Print current positions
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date

from scripts.paper_state import PaperTradingState
from scripts.run_backtest import (
    BEAR_TICKERS,
    PortfolioConfig,
    compute_regime_by_date,
    fetch_bars_from_ib,
    get_union_universe,
    make_earnings_drift_signals_fn,
    make_momentum_signals_fn,
    make_quality_value_signals_fn,
    make_sector_rotation_signals_fn,
    make_short_term_mr_signals_fn,
    make_signals_fn,
    make_tail_risk_hedge_signals_fn,
    make_thematic_momentum_signals_fn,
)
from scripts.fetch_fundamentals import load_fundamentals_cache, build_fundamentals_lookup, SECTOR_MAP
from scripts.fetch_earnings import load_earnings_cache, build_earnings_lookup
from services.risk_management.engine import RiskEngine


DEFAULT_STATE_PATH = "data/paper_state.json"

# Capital allocations (same as backtest main())
CAPITAL_ALLOCATIONS = {
    "mean_reversion": 0.12,
    "momentum": 0.18,
    "sector_rotation": 0.12,
    "quality_value": 0.12,
    "earnings_drift": 0.15,
    "short_term_mr": 0.10,
    "thematic_momentum": 0.11,
    "tail_risk_hedge": 0.10,
}


def build_portfolios(
    capital: float,
    bars_by_ticker: dict[str, list[dict]],
    regime_by_date: dict,
    fundamentals_lookup,
    earnings_lookup,
) -> dict[str, PortfolioConfig]:
    """Build all 8 portfolio configs (same as backtest main())."""
    portfolios = {}

    mr_capital = capital * CAPITAL_ALLOCATIONS["mean_reversion"]
    portfolios["mean_reversion"] = PortfolioConfig(
        name="mean_reversion",
        capital=mr_capital,
        signals_fn=make_signals_fn(
            position_size_pct=0.12,
            initial_capital=mr_capital,
            trailing_stop_pct=0.10,
        ),
        risk_engine=RiskEngine(
            position_entry_limit_pct=15.0,
            sector_concentration_pct=30.0,
            total_exposure_limit_pct=120.0,
            max_lots_per_ticker=2,
        ),
    )

    mom_capital = capital * CAPITAL_ALLOCATIONS["momentum"]
    portfolios["momentum"] = PortfolioConfig(
        name="momentum",
        capital=mom_capital,
        signals_fn=make_momentum_signals_fn(
            bars_by_ticker=bars_by_ticker,
            top_n=5,
            lookback_days=126,
            position_size_pct=0.12,
            initial_capital=mom_capital,
            trailing_stop_pct=0.10,
            bear_tickers=BEAR_TICKERS,
        ),
        risk_engine=RiskEngine(
            position_entry_limit_pct=12.0,
            sector_concentration_pct=30.0,
            total_exposure_limit_pct=150.0,
            max_lots_per_ticker=1,
        ),
    )

    sec_capital = capital * CAPITAL_ALLOCATIONS["sector_rotation"]
    portfolios["sector_rotation"] = PortfolioConfig(
        name="sector_rotation",
        capital=sec_capital,
        signals_fn=make_sector_rotation_signals_fn(
            bars_by_ticker=bars_by_ticker,
            top_n=3,
            lookback_days=63,
            position_size_pct=0.20,
            initial_capital=sec_capital,
            trailing_stop_pct=0.08,
        ),
        risk_engine=RiskEngine(
            position_entry_limit_pct=20.0,
            sector_concentration_pct=50.0,
            total_exposure_limit_pct=100.0,
            max_lots_per_ticker=1,
        ),
    )

    qv_capital = capital * CAPITAL_ALLOCATIONS["quality_value"]
    portfolios["quality_value"] = PortfolioConfig(
        name="quality_value",
        capital=qv_capital,
        signals_fn=make_quality_value_signals_fn(
            fundamentals_lookup=fundamentals_lookup,
            sector_map=SECTOR_MAP,
            top_n=15,
            position_size_pct=0.10,
            initial_capital=qv_capital,
            trailing_stop_pct=0.12,
        ),
        risk_engine=RiskEngine(
            position_entry_limit_pct=10.0,
            sector_concentration_pct=30.0,
            total_exposure_limit_pct=100.0,
            max_lots_per_ticker=1,
        ),
    )

    ed_capital = capital * CAPITAL_ALLOCATIONS["earnings_drift"]
    portfolios["earnings_drift"] = PortfolioConfig(
        name="earnings_drift",
        capital=ed_capital,
        signals_fn=make_earnings_drift_signals_fn(
            earnings_lookup=earnings_lookup,
            surprise_threshold_pct=5.0,
            max_hold_days=20,
            position_size_pct=0.08,
            initial_capital=ed_capital,
            trailing_stop_pct=0.06,
        ),
        risk_engine=RiskEngine(
            position_entry_limit_pct=8.0,
            sector_concentration_pct=30.0,
            total_exposure_limit_pct=100.0,
            max_lots_per_ticker=1,
        ),
    )

    stmr_capital = capital * CAPITAL_ALLOCATIONS["short_term_mr"]
    portfolios["short_term_mr"] = PortfolioConfig(
        name="short_term_mr",
        capital=stmr_capital,
        signals_fn=make_short_term_mr_signals_fn(
            position_size_pct=0.08,
            initial_capital=stmr_capital,
            max_hold_days=5,
        ),
        risk_engine=RiskEngine(
            position_entry_limit_pct=8.0,
            sector_concentration_pct=30.0,
            total_exposure_limit_pct=100.0,
            max_lots_per_ticker=1,
        ),
    )

    them_capital = capital * CAPITAL_ALLOCATIONS["thematic_momentum"]
    portfolios["thematic_momentum"] = PortfolioConfig(
        name="thematic_momentum",
        capital=them_capital,
        signals_fn=make_thematic_momentum_signals_fn(
            bars_by_ticker=bars_by_ticker,
            top_n=8,
            lookback_days=63,
            position_size_pct=0.15,
            initial_capital=them_capital,
            trailing_stop_pct=0.10,
        ),
        risk_engine=RiskEngine(
            position_entry_limit_pct=15.0,
            sector_concentration_pct=50.0,
            total_exposure_limit_pct=120.0,
            max_lots_per_ticker=1,
        ),
    )

    tr_capital = capital * CAPITAL_ALLOCATIONS["tail_risk_hedge"]
    portfolios["tail_risk_hedge"] = PortfolioConfig(
        name="tail_risk_hedge",
        capital=tr_capital,
        signals_fn=make_tail_risk_hedge_signals_fn(
            regime_by_date=regime_by_date,
            position_size_pct=0.25,
            initial_capital=tr_capital,
        ),
        risk_engine=RiskEngine(
            position_entry_limit_pct=25.0,
            sector_concentration_pct=50.0,
            total_exposure_limit_pct=100.0,
            max_lots_per_ticker=1,
        ),
    )

    return portfolios


def print_status(state: PaperTradingState) -> None:
    """Print current paper trading status."""
    print("\n" + "=" * 60)
    print("  PAPER TRADING STATUS")
    print("=" * 60)

    total_equity = 0.0
    total_capital = 0.0
    total_positions = 0

    for name, pf in state.portfolios.items():
        cash = pf["cash"]
        positions = pf["positions"]
        n_pos = len(positions)
        total_positions += n_pos
        total_capital += pf["capital"]

        # Estimate equity (use entry prices if no current prices available)
        market_value = sum(
            pos["quantity"] * pos["entry_price"]
            for pos in positions.values()
        )
        equity = cash + market_value
        total_equity += equity
        pnl = equity - pf["capital"]
        n_trades = len(pf["trades"])

        print(f"\n  --- {name} ---")
        print(f"    Capital:    ${pf['capital']:>12,.2f}")
        print(f"    Cash:       ${cash:>12,.2f}")
        print(f"    Equity:     ${equity:>12,.2f}")
        print(f"    P&L:        ${pnl:>+12,.2f}")
        print(f"    Positions:  {n_pos}")
        print(f"    Trades:     {n_trades}")

        if positions:
            for ticker, pos in positions.items():
                print(f"      {ticker:>6s}  {pos['quantity']:>4d} shares @ ${pos['entry_price']:.2f}")

    print(f"\n  --- TOTAL ---")
    print(f"    Capital:    ${total_capital:>12,.2f}")
    print(f"    Equity:     ${total_equity:>12,.2f}")
    print(f"    P&L:        ${total_equity - total_capital:>+12,.2f}")
    print(f"    Positions:  {total_positions}")
    print("=" * 60)


def run_daily(
    state: PaperTradingState,
    portfolios: dict[str, PortfolioConfig],
    bars_by_ticker: dict[str, list[dict]],
) -> list[dict]:
    """Run one daily cycle: generate signals for all portfolios.

    Returns list of signals generated (for logging/review).
    Does NOT submit orders — just identifies what would be traded.
    Actual IB submission is a separate step (--execute flag).
    """
    signals_generated: list[dict] = []
    today = date.today()

    for name, pc in portfolios.items():
        pf = state.portfolios.get(name, {})
        universe = list(bars_by_ticker.keys())

        for ticker in universe:
            bars = bars_by_ticker.get(ticker, [])
            if not bars:
                continue

            signal = pc.signals_fn(ticker, bars)
            if signal is not None:
                signal["portfolio"] = name
                signal["date"] = str(today)
                signals_generated.append(signal)

                action = signal["action"]
                price = signal["limit_price"]
                qty = signal.get("quantity", 0)

                if action == "buy":
                    print(f"  BUY  {ticker:>6s}  {qty:>4d} @ ${price:>8.2f}  [{name}]")
                elif action == "sell":
                    reason = signal.get("exit_reason", "signal")
                    print(f"  SELL {ticker:>6s}             @ ${price:>8.2f}  [{name}] ({reason})")

    return signals_generated


def main():
    parser = argparse.ArgumentParser(description="Daily paper trading runner")
    parser.add_argument("--capital", type=float, default=100_000,
                        help="Total capital (default: 100000)")
    parser.add_argument("--state-file", default=DEFAULT_STATE_PATH,
                        help="Path to state JSON file")
    parser.add_argument("--years", type=int, default=1,
                        help="Years of historical bars to fetch for signal warmup (default: 1)")
    parser.add_argument("--init", action="store_true",
                        help="Initialize fresh paper trading state")
    parser.add_argument("--status", action="store_true",
                        help="Print current status and exit")
    parser.add_argument("--ib-host", default="127.0.0.1")
    parser.add_argument("--ib-port", type=int, default=7497)
    args = parser.parse_args()

    # --init: create fresh state
    if args.init:
        capitals = {name: args.capital * pct for name, pct in CAPITAL_ALLOCATIONS.items()}
        state = PaperTradingState.create_new(capitals, args.state_file)
        state.save()
        print(f"Initialized paper trading state at {args.state_file}")
        print(f"Total capital: ${args.capital:,.0f}")
        for name, cap in capitals.items():
            print(f"  {name}: ${cap:,.0f}")
        return

    # --status: print current state
    if args.status:
        try:
            state = PaperTradingState.load(args.state_file)
        except FileNotFoundError:
            print(f"No state file found at {args.state_file}")
            print("Run with --init to create one.")
            sys.exit(1)
        print_status(state)
        return

    # Daily run
    try:
        state = PaperTradingState.load(args.state_file)
    except FileNotFoundError:
        print(f"No state file found at {args.state_file}")
        print("Run with --init to create one.")
        sys.exit(1)

    print(f"Paper Trading Daily Run — {date.today()}")
    print(f"State loaded from {args.state_file}")

    # Fetch bars from IB
    all_tickers = get_union_universe(list(CAPITAL_ALLOCATIONS.keys()))
    print(f"\nFetching bars for {len(all_tickers)} tickers ({args.years} year)...")
    bars_by_ticker = fetch_bars_from_ib(
        tickers=all_tickers,
        years=args.years,
        host=args.ib_host,
        port=args.ib_port,
    )

    if not bars_by_ticker:
        print("ERROR: No data fetched. Is IB Gateway running?")
        sys.exit(1)

    # Load caches
    fundamentals_cache = load_fundamentals_cache("data/cache/fundamentals.json")
    earnings_cache = load_earnings_cache("data/cache/earnings.json")
    fundamentals_lookup = build_fundamentals_lookup(fundamentals_cache)
    earnings_lookup = build_earnings_lookup(earnings_cache, window_days=2)

    # Compute regime
    regime_by_date = compute_regime_by_date(bars_by_ticker)

    # Build portfolios
    portfolios = build_portfolios(
        capital=args.capital,
        bars_by_ticker=bars_by_ticker,
        regime_by_date=regime_by_date,
        fundamentals_lookup=fundamentals_lookup,
        earnings_lookup=earnings_lookup,
    )

    # Run signals
    print(f"\nRunning signals across {len(portfolios)} portfolios...")
    signals = run_daily(state, portfolios, bars_by_ticker)

    if signals:
        print(f"\n{len(signals)} signals generated")
    else:
        print("\nNo signals generated today")

    # Save state
    state.save()
    print(f"\nState saved to {args.state_file}")


if __name__ == "__main__":
    main()
```

### Step 2: Verify it's syntactically valid

Run: `python -c "import scripts.run_paper"`
Expected: No import errors (IB not required for import)

### Step 3: Commit

```bash
git add scripts/run_paper.py
git commit -m "feat: add daily paper trading runner script"
```

---

## Task 4: Update Documentation

**Files:**
- Modify: `docs/strategy.md`

### Step 1: Add Paper Trading section

After the "Performance-Adaptive Rebalancer" section, add:

```markdown
## Paper Trading

A daily runner script (`scripts/run_paper.py`) reuses the exact same signal functions from the backtest for paper trading against IB Gateway.

### Setup

```bash
# Initialize paper trading state
python scripts/run_paper.py --init --capital 100000

# Populate data caches (if not already done)
python scripts/fetch_fundamentals.py
python scripts/fetch_earnings.py
```

### Daily Run

```bash
# Run daily signals (requires IB Gateway on paper port 7497)
python scripts/run_paper.py

# Check current positions
python scripts/run_paper.py --status
```

### How It Works

1. Loads persisted state from `data/paper_state.json`
2. Fetches latest bars from IB Gateway (1 year of history for signal warmup)
3. Runs all 8 signal functions (identical to backtest)
4. Prints generated buy/sell signals with portfolio attribution
5. Saves updated state

State persists positions, trade history, and cash per portfolio between runs. The signal functions maintain their own internal state (tracked positions, rankings) which is rebuilt from historical bars each run.

### Stream Message Tagging

`ApprovedOrderMessage` and `FillMessage` include an optional `portfolio` field for strategy attribution. This enables per-strategy P&L tracking in the live pipeline.
```

### Step 2: Commit

```bash
git add docs/strategy.md
git commit -m "docs: add paper trading section to strategy.md"
```

---

## Verification

After all tasks:

```bash
# All tests should pass
pytest tests/ -v --tb=short

# Paper state tests specifically
pytest tests/backtest/test_paper_state.py -v

# Message field tests
pytest tests/shared/test_message_portfolio_field.py -v
```
