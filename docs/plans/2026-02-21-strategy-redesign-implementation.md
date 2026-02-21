# Strategy Redesign Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Redesign the trading algorithm to buy large-cap industry leaders at support levels with RSI+volume confirmation, pyramid into winners, and exit via trailing stops instead of fixed profit targets.

**Architecture:** Add RSI and Volume signals to the signal generation module. Refactor the backtest runner to support multiple lots per ticker with per-lot trailing stops. Replace the backtest signal function with the new entry/exit logic. Update risk engine defaults and add max-lots-per-ticker constraint.

**Tech Stack:** Python, numpy, existing signal/backtest/risk framework. No new dependencies.

---

### Task 1: Add RSISignal

**Files:**
- Modify: `services/signal_generation/technical.py:205` (append after SupportTrendSignal)
- Test: `tests/services/signal_generation/test_technical.py`

**Step 1: Write the failing test**

Append to `tests/services/signal_generation/test_technical.py`:

```python
from services.signal_generation.technical import RSISignal

def test_rsi_signal_oversold():
    """RSI should be high (bullish) when price has been falling."""
    data = make_ohlcv(days=252, base_price=100.0)
    # Force a strong decline in last 14 days
    data["close"][-14:] = np.linspace(100, 70, 14)
    sig = RSISignal()
    result = sig.compute(data)
    # RSI < 35 maps to positive signal value (oversold = bullish for mean reversion)
    assert result.value > 0.3
    assert result.confidence > 0.5

def test_rsi_signal_overbought():
    """RSI should be negative (bearish) when price has been rising."""
    data = make_ohlcv(days=252, base_price=100.0)
    data["close"][-14:] = np.linspace(100, 130, 14)
    sig = RSISignal()
    result = sig.compute(data)
    assert result.value < 0

def test_rsi_signal_needs_min_bars():
    """RSI should return zero with insufficient data."""
    data = make_ohlcv(days=10, base_price=100.0)
    sig = RSISignal()
    result = sig.compute(data)
    assert result.value == 0.0
    assert result.confidence == 0.0
```

**Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/services/signal_generation/test_technical.py::test_rsi_signal_oversold -v`
Expected: FAIL — ImportError, RSISignal not found

**Step 3: Implement RSISignal**

Append to `services/signal_generation/technical.py` after `SupportTrendSignal`:

```python
class RSISignal(Signal):
    """14-day Relative Strength Index mapped to a mean-reversion signal.

    Oversold (RSI < 35) produces positive values (bullish for mean reversion).
    Overbought (RSI > 65) produces negative values (bearish).

    Scale:
    *  1.0 = RSI at 0 (extremely oversold)
    *  0.0 = RSI at 50 (neutral)
    * -1.0 = RSI at 100 (extremely overbought)
    """

    name = "rsi"

    def __init__(self, period: int = 14) -> None:
        self.period = period

    def compute(self, data: dict[str, Any]) -> SignalResult:
        closes = np.asarray(data["close"], dtype=float)

        if len(closes) < self.period + 1:
            return SignalResult(value=0.0, confidence=0.0)

        deltas = np.diff(closes[-(self.period + 1):])
        gains = np.where(deltas > 0, deltas, 0.0)
        losses = np.where(deltas < 0, -deltas, 0.0)

        avg_gain = np.mean(gains)
        avg_loss = np.mean(losses)

        if avg_loss == 0:
            rsi = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi = 100.0 - (100.0 / (1.0 + rs))

        # Map RSI to signal value: RSI 50 -> 0, RSI 0 -> +1, RSI 100 -> -1
        value = (50.0 - rsi) / 50.0

        # Confidence: highest when RSI is at extremes (< 30 or > 70)
        distance_from_center = abs(rsi - 50.0)
        confidence = min(distance_from_center / 30.0, 1.0)

        return SignalResult(value=value, confidence=confidence)
```

**Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/services/signal_generation/test_technical.py -v`
Expected: All pass including new RSI tests

**Step 5: Commit**

```bash
git add services/signal_generation/technical.py tests/services/signal_generation/test_technical.py
git commit -m "feat: add RSISignal for oversold/overbought detection"
```

---

### Task 2: Add VolumeSignal

**Files:**
- Modify: `services/signal_generation/technical.py` (append after RSISignal)
- Test: `tests/services/signal_generation/test_technical.py`

**Step 1: Write the failing test**

Append to `tests/services/signal_generation/test_technical.py`:

```python
from services.signal_generation.technical import VolumeSignal

def test_volume_signal_above_average():
    """Volume signal should be positive when current volume > 1.5x average."""
    data = make_ohlcv(days=252, base_price=100.0)
    data["volume"] = np.full(252, 500_000)  # flat baseline
    data["volume"][-1] = 1_000_000  # 2x average on last day
    sig = VolumeSignal()
    result = sig.compute(data)
    assert result.value > 0.5
    assert result.confidence > 0.5

def test_volume_signal_below_average():
    """Volume signal should be near zero when volume is below average."""
    data = make_ohlcv(days=252, base_price=100.0)
    data["volume"] = np.full(252, 500_000)
    data["volume"][-1] = 200_000  # 0.4x average
    sig = VolumeSignal()
    result = sig.compute(data)
    assert result.value < 0.0

def test_volume_signal_needs_min_bars():
    """Volume signal should return zero with insufficient data."""
    data = make_ohlcv(days=5, base_price=100.0)
    sig = VolumeSignal()
    result = sig.compute(data)
    assert result.value == 0.0
```

**Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/services/signal_generation/test_technical.py::test_volume_signal_above_average -v`
Expected: FAIL — ImportError

**Step 3: Implement VolumeSignal**

Append to `services/signal_generation/technical.py`:

```python
class VolumeSignal(Signal):
    """Compares current volume to the 20-day moving average.

    Positive when volume is elevated (confirms institutional activity).
    Neutral at 1x average, maximum at 3x average.

    Scale:
    *  1.0 = volume at 3x+ the 20-day average
    *  0.0 = volume at 1x the 20-day average
    * -1.0 = volume at 0 (no trading)
    """

    name = "volume_ratio"

    def __init__(self, lookback: int = 20) -> None:
        self.lookback = lookback

    def compute(self, data: dict[str, Any]) -> SignalResult:
        volumes = np.asarray(data["volume"], dtype=float)

        if len(volumes) < self.lookback + 1:
            return SignalResult(value=0.0, confidence=0.0)

        avg_volume = np.mean(volumes[-(self.lookback + 1):-1])
        if avg_volume == 0:
            return SignalResult(value=0.0, confidence=0.0)

        current_volume = float(volumes[-1])
        ratio = current_volume / avg_volume

        # Map ratio to signal: 1x -> 0, 3x -> +1, 0x -> -1
        value = (ratio - 1.0) / 2.0

        # Confidence: high when ratio is clearly above or below 1
        confidence = min(abs(ratio - 1.0) / 1.0, 1.0)

        return SignalResult(value=value, confidence=confidence)
```

**Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/services/signal_generation/test_technical.py -v`
Expected: All pass

**Step 5: Commit**

```bash
git add services/signal_generation/technical.py tests/services/signal_generation/test_technical.py
git commit -m "feat: add VolumeSignal for volume confirmation"
```

---

### Task 3: Add max_lots_per_ticker to RiskEngine

**Files:**
- Modify: `services/risk_management/engine.py:40-54` (constructor), `services/risk_management/engine.py:56-125` (check_entry)
- Test: `tests/services/risk_management/test_engine.py`

**Step 1: Write the failing test**

Create `tests/services/risk_management/test_engine_lots.py`:

```python
from __future__ import annotations

from services.risk_management.engine import RiskEngine, PortfolioState


def _make_portfolio(nav: float = 100_000) -> PortfolioState:
    return PortfolioState(
        nav=nav,
        peak_nav=nav,
        positions={},
        sector_exposure={},
        total_exposure_pct=0.0,
        margin_utilization_pct=0.0,
    )


def test_check_entry_respects_max_lots():
    """check_entry should reject when existing_lots >= max_lots_per_ticker."""
    engine = RiskEngine(
        position_entry_limit_pct=7.0,
        sector_concentration_pct=30.0,
        total_exposure_limit_pct=100.0,
        max_lots_per_ticker=2,
    )
    portfolio = _make_portfolio()
    # First lot: approved
    decision = engine.check_entry("AAPL", 50, 150.0, "Tech", portfolio, existing_lots=0)
    assert decision.approved

    # Second lot: approved
    decision = engine.check_entry("AAPL", 50, 155.0, "Tech", portfolio, existing_lots=1)
    assert decision.approved

    # Third lot: rejected (max 2)
    decision = engine.check_entry("AAPL", 50, 160.0, "Tech", portfolio, existing_lots=2)
    assert not decision.approved
    assert "max lots" in decision.reason.lower()


def test_check_entry_default_no_lot_limit():
    """Without max_lots_per_ticker, existing_lots should be ignored."""
    engine = RiskEngine(
        position_entry_limit_pct=7.0,
        sector_concentration_pct=30.0,
        total_exposure_limit_pct=100.0,
    )
    portfolio = _make_portfolio()
    decision = engine.check_entry("AAPL", 50, 150.0, "Tech", portfolio, existing_lots=5)
    assert decision.approved
```

**Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/services/risk_management/test_engine_lots.py -v`
Expected: FAIL — TypeError: check_entry() got unexpected keyword argument 'existing_lots'

**Step 3: Implement max_lots_per_ticker**

In `services/risk_management/engine.py`, add `max_lots_per_ticker` to `__init__` (after line 47):

```python
    def __init__(
        self,
        position_entry_limit_pct: float = 5.0,
        sector_concentration_pct: float = 20.0,
        total_exposure_limit_pct: float = 150.0,
        stop_loss_trailing_pct: float = 15.0,
        drawdown_pause_pct: float = 10.0,
        drawdown_circuit_breaker_pct: float = 20.0,
        max_lots_per_ticker: int | None = None,
    ):
        self.position_entry_limit_pct = position_entry_limit_pct
        self.sector_concentration_pct = sector_concentration_pct
        self.total_exposure_limit_pct = total_exposure_limit_pct
        self.stop_loss_trailing_pct = stop_loss_trailing_pct
        self.drawdown_pause_pct = drawdown_pause_pct
        self.drawdown_circuit_breaker_pct = drawdown_circuit_breaker_pct
        self.max_lots_per_ticker = max_lots_per_ticker
```

Add `existing_lots` parameter to `check_entry` (default 0) and add the lot check as the first gate after total exposure:

```python
    def check_entry(
        self,
        ticker: str,
        quantity: int,
        price: float,
        sector: str,
        portfolio: PortfolioState,
        existing_lots: int = 0,
    ) -> RiskDecision:
```

Add the check right after the total exposure check (after line 84):

```python
        # Check max lots per ticker
        if self.max_lots_per_ticker is not None and existing_lots >= self.max_lots_per_ticker:
            return RiskDecision(
                approved=False,
                reason=(
                    f"Max lots per ticker reached for {ticker}: "
                    f"{existing_lots} >= {self.max_lots_per_ticker}"
                ),
                adjusted_quantity=0,
            )
```

**Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/services/risk_management/ -v`
Expected: All pass (new and existing)

**Step 5: Commit**

```bash
git add services/risk_management/engine.py tests/services/risk_management/test_engine_lots.py
git commit -m "feat: add max_lots_per_ticker constraint to RiskEngine"
```

---

### Task 4: Refactor BacktestRunner for multi-lot positions

The current runner stores `positions` as `dict[str, _Position]` (one position per ticker). We need `dict[str, list[_Lot]]` to support multiple lots per ticker with per-lot trailing stops.

**Files:**
- Modify: `backtest/runner.py:59-176` (run method), `backtest/runner.py:179-188` (_Position -> _Lot)
- Modify: `backtest/runner.py:200-227` (_make_simple_portfolio)
- Test: `tests/backtest/test_runner_multilot.py`

**Step 1: Write the failing test**

Create `tests/backtest/test_runner_multilot.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from backtest.runner import BacktestRunner, BacktestResult
from backtest.simulator import SimulatedExecutor


def test_runner_supports_multiple_lots_per_ticker():
    """Runner should accept multiple buy signals for the same ticker."""
    executor = SimulatedExecutor(slippage_bps=0, commission_per_share=0)
    runner = BacktestRunner(executor=executor, initial_capital=100_000)

    # 10 days of data, price goes 100 -> 110 -> dips to 105 -> back to 115
    bars = {
        "TEST": [
            {"date": date(2024, 1, d), "open": p, "high": p + 1, "low": p - 1, "close": p, "volume": 1000}
            for d, p in [
                (2, 100), (3, 102), (4, 105), (5, 103), (6, 108),
                (7, 105), (8, 110), (9, 112), (10, 115), (11, 120),
            ]
        ],
    }

    call_count = {"buy": 0}

    def signals_fn(ticker, bars_so_far):
        if len(bars_so_far) < 2:
            return None
        price = bars_so_far[-1]["close"]
        # Buy on day 2 (price 102) and day 7 (price 105)
        if len(bars_so_far) == 2:
            call_count["buy"] += 1
            return {"action": "buy", "ticker": ticker, "limit_price": price + 1,
                    "quantity": 10, "sector": "Test", "signals": {}}
        if len(bars_so_far) == 6:
            call_count["buy"] += 1
            return {"action": "buy", "ticker": ticker, "limit_price": price + 1,
                    "quantity": 10, "sector": "Test", "signals": {}}
        # Sell all on day 10 (price 120)
        if len(bars_so_far) == 10:
            return {"action": "sell", "ticker": ticker, "limit_price": price,
                    "quantity": 0, "sector": "Test", "exit_reason": "trailing_stop",
                    "lot_index": "all"}
        return None

    @dataclass
    class AlwaysApprove:
        approved: bool = True
        adjusted_quantity: int = 0
        reason: str = "ok"

    class MockRisk:
        def check_entry(self, ticker, quantity, price, sector, portfolio, existing_lots=0):
            return AlwaysApprove(adjusted_quantity=quantity)

    result = runner.run(bars, signals_fn, MockRisk())

    assert call_count["buy"] == 2
    # Should have trades from exiting the lots
    assert len(result.trades) >= 1
    # Both lots should have been tracked
    assert result.metrics["total_trades"] >= 1
```

**Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/backtest/test_runner_multilot.py -v`
Expected: FAIL — second buy is rejected because `ticker in positions` is True

**Step 3: Refactor _Position to _Lot and update run() for multi-lot**

Rename `_Position` to `_Lot` and add `peak_price` for trailing stop:

```python
@dataclass
class _Lot:
    """Individual lot within a position."""

    ticker: str
    quantity: int
    entry_price: float
    entry_date: Any
    entry_commission: float
    entry_signals: dict = field(default_factory=dict)
    peak_price: float = 0.0

    def __post_init__(self):
        if self.peak_price == 0.0:
            self.peak_price = self.entry_price
```

Change `positions` from `dict[str, _Position]` to `dict[str, list[_Lot]]`.

In `run()`, replace the core loop logic:

**For buy signals** (currently line 116: `elif action == "buy" and ticker not in positions`):
Change to allow buys when ticker already has lots:

```python
                elif action == "buy":
                    # Check risk with existing lot count
                    existing_lots = positions.get(ticker, [])
                    limit_price = signal["limit_price"]
                    quantity = signal["quantity"]
                    sector = signal.get("sector", "Unknown")

                    portfolio_state = _make_simple_portfolio(
                        cash, positions, bars_by_date, current_date
                    )
                    decision = risk_engine.check_entry(
                        ticker, quantity, limit_price, sector, portfolio_state,
                        existing_lots=len(existing_lots),
                    )

                    if not decision.approved:
                        continue

                    adjusted_qty = decision.adjusted_quantity
                    fill = self.executor.try_fill_limit_entry(
                        limit_price=limit_price,
                        quantity=adjusted_qty,
                        bar=bar,
                    )

                    if fill is None:
                        continue

                    cost = fill["fill_price"] * fill["quantity"] + fill["commission"]
                    cash -= cost
                    lot = _Lot(
                        ticker=ticker,
                        quantity=fill["quantity"],
                        entry_price=fill["fill_price"],
                        entry_date=fill["date"],
                        entry_commission=fill["commission"],
                        entry_signals=signal.get("signals", {}),
                    )
                    if ticker not in positions:
                        positions[ticker] = []
                    positions[ticker].append(lot)
```

**For sell signals** (currently line 91: `if action == "sell" and ticker in positions`):
Exit all lots for the ticker:

```python
                if action == "sell" and ticker in positions:
                    lots = positions.pop(ticker)
                    for lot in lots:
                        fill = self.executor.fill_market_exit(
                            quantity=lot.quantity, bar=bar
                        )
                        exit_value = fill["fill_price"] * fill["quantity"]
                        entry_value = lot.entry_price * lot.quantity
                        pnl = exit_value - entry_value - lot.entry_commission - fill["commission"]
                        cash += exit_value - fill["commission"]

                        trades.append({
                            "ticker": ticker,
                            "entry_date": lot.entry_date,
                            "exit_date": fill["date"],
                            "entry_price": lot.entry_price,
                            "exit_price": fill["fill_price"],
                            "quantity": lot.quantity,
                            "pnl": pnl,
                            "entry_commission": lot.entry_commission,
                            "exit_commission": fill["commission"],
                            "entry_signals": lot.entry_signals,
                            "exit_reason": signal.get("exit_reason", "unknown"),
                        })
```

**For NAV calculation** (currently line 154-161):
Iterate over all lots:

```python
            nav = cash
            for ticker, lots in positions.items():
                bar = bars_by_date.get((ticker, current_date))
                for lot in lots:
                    if bar is not None:
                        nav += bar["close"] * lot.quantity
                        lot.peak_price = max(lot.peak_price, bar["close"])
                    else:
                        nav += lot.entry_price * lot.quantity
```

**Update `_make_simple_portfolio`** to work with list-based positions:

```python
def _make_simple_portfolio(
    cash: float,
    positions: dict[str, list[_Lot]],
    bars_by_date: dict,
    current_date: Any,
) -> Any:
    from backtest._portfolio_state import SimplePortfolioState

    nav = cash
    all_positions = {}
    for ticker, lots in positions.items():
        total_qty = sum(lot.quantity for lot in lots)
        all_positions[ticker] = {"quantity": total_qty}
        for lot in lots:
            bar = bars_by_date.get((ticker, current_date))
            price = bar["close"] if bar else lot.entry_price
            nav += price * lot.quantity

    return SimplePortfolioState(
        nav=nav,
        peak_nav=nav,
        positions=all_positions,
        sector_exposure={},
        total_exposure_pct=0.0,
        margin_utilization_pct=0.0,
    )
```

**Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/backtest/test_runner_multilot.py -v`
Expected: PASS

**Step 5: Run full test suite to check for regressions**

Run: `.venv/bin/pytest tests/backtest/ -v`
Expected: All pass. Existing tests that use single positions should still work because a single-lot list behaves the same.

**Step 6: Commit**

```bash
git add backtest/runner.py tests/backtest/test_runner_multilot.py
git commit -m "refactor: support multiple lots per ticker in BacktestRunner"
```

---

### Task 5: Rewrite make_signals_fn() with new strategy

Replace the old signal function with the new entry/exit logic: support + RSI + volume confirmation, per-lot trailing stops, pyramiding.

**Files:**
- Modify: `scripts/run_backtest.py:27-31` (imports), `scripts/run_backtest.py:138-252` (make_signals_fn)
- Modify: `scripts/run_backtest.py:394-398` (RiskEngine defaults in main)

**Step 1: Update imports**

At `scripts/run_backtest.py:27-31`, add RSISignal and VolumeSignal:

```python
from services.signal_generation.technical import (
    SupportProximitySignal,
    SupportStrengthSignal,
    SupportTrendSignal,
    RSISignal,
    VolumeSignal,
    find_support_levels,
)
```

**Step 2: Replace make_signals_fn()**

Replace the entire `make_signals_fn` function (lines 138-252) with:

```python
def make_signals_fn(
    position_size_pct: float = 0.07,
    initial_capital: float = 100_000,
    hard_stop_pct: float = 0.05,
    trailing_stop_pct: float = 0.05,
    max_lots: int = 2,
):
    """Create a signal function implementing mean-reversion on large-cap support levels.

    Entry (first lot): support proximity + RSI < 35 + volume > 1.5x avg + rising supports.
    Entry (add-on lot): in profit + new support signal + RSI < 40 + volume confirmation.
    Exit: 5% trailing stop from peak, or 5% hard stop from entry.
    """
    proximity_signal = SupportProximitySignal()
    strength_signal = SupportStrengthSignal()
    trend_signal = SupportTrendSignal()
    rsi_signal = RSISignal()
    volume_signal = VolumeSignal()

    # Per-ticker lot tracking: ticker -> list of {entry_price, entry_idx, peak_price}
    tracked: dict[str, list[dict]] = {}

    def signals_fn(ticker: str, bars: list[dict]) -> dict | None:
        if len(bars) < 60:
            return None

        current_price = bars[-1]["close"]
        bar_count = len(bars)
        lots = tracked.get(ticker, [])

        # === Exit logic: check each lot for trailing stop or hard stop ===
        if lots:
            # Update peak prices
            for lot in lots:
                lot["peak_price"] = max(lot["peak_price"], current_price)

            should_sell = False
            exit_reason = "unknown"

            for lot in lots:
                peak = lot["peak_price"]
                entry = lot["entry_price"]

                # Hard stop: 5% below entry
                if (current_price - entry) / entry <= -hard_stop_pct:
                    should_sell = True
                    exit_reason = "hard_stop"
                    break

                # Trailing stop: 5% below peak
                if peak > entry and (peak - current_price) / peak >= trailing_stop_pct:
                    should_sell = True
                    exit_reason = "trailing_stop"
                    break

            if should_sell:
                tracked.pop(ticker, None)
                return {
                    "action": "sell",
                    "ticker": ticker,
                    "limit_price": current_price,
                    "quantity": 0,
                    "sector": "Unknown",
                    "exit_reason": exit_reason,
                }

        # === Compute signals ===
        data = _build_data(bars)
        try:
            proximity = proximity_signal.compute(data)
            strength = strength_signal.compute(data)
            trend = trend_signal.compute(data)
            rsi = rsi_signal.compute(data)
            volume = volume_signal.compute(data)
        except Exception:
            return None

        # === Add-on entry (already have lots, in profit) ===
        if lots and len(lots) < max_lots:
            avg_entry = sum(l["entry_price"] for l in lots) / len(lots)
            in_profit = current_price > avg_entry

            if (
                in_profit
                and proximity.value > 0.6
                and strength.confidence > 0.5
                and rsi.value > 0.2  # RSI < 40
                and volume.value > 0.25  # volume > 1.5x avg
                and trend.value >= 0.0
            ):
                support_levels = find_support_levels(data)
                limit_price = support_levels[0] if support_levels else current_price
                quantity = max(1, int(initial_capital * position_size_pct / current_price))
                lots.append({
                    "entry_price": current_price,
                    "entry_idx": bar_count,
                    "peak_price": current_price,
                })
                return {
                    "action": "buy",
                    "ticker": ticker,
                    "limit_price": limit_price,
                    "quantity": quantity,
                    "sector": "Unknown",
                    "signals": {
                        "proximity": {"value": proximity.value, "confidence": proximity.confidence},
                        "strength": {"value": strength.value, "confidence": strength.confidence},
                        "trend": {"value": trend.value, "confidence": trend.confidence},
                        "rsi": {"value": rsi.value, "confidence": rsi.confidence},
                        "volume": {"value": volume.value, "confidence": volume.confidence},
                    },
                }

        # === First entry (no lots) ===
        if not lots:
            if (
                proximity.value > 0.6
                and strength.confidence > 0.5
                and rsi.value > 0.3  # RSI < 35
                and volume.value > 0.25  # volume > 1.5x avg
                and trend.value >= 0.0
            ):
                support_levels = find_support_levels(data)
                limit_price = support_levels[0] if support_levels else current_price
                quantity = max(1, int(initial_capital * position_size_pct / current_price))
                tracked[ticker] = [{
                    "entry_price": current_price,
                    "entry_idx": bar_count,
                    "peak_price": current_price,
                }]
                return {
                    "action": "buy",
                    "ticker": ticker,
                    "limit_price": limit_price,
                    "quantity": quantity,
                    "sector": "Unknown",
                    "signals": {
                        "proximity": {"value": proximity.value, "confidence": proximity.confidence},
                        "strength": {"value": strength.value, "confidence": strength.confidence},
                        "trend": {"value": trend.value, "confidence": trend.confidence},
                        "rsi": {"value": rsi.value, "confidence": rsi.confidence},
                        "volume": {"value": volume.value, "confidence": volume.confidence},
                    },
                }

        return None

    return signals_fn
```

**Step 3: Update RiskEngine defaults in main()**

At `scripts/run_backtest.py:394-398`, change:

```python
    risk_engine = RiskEngine(
        position_entry_limit_pct=7.0,
        sector_concentration_pct=30.0,
        total_exposure_limit_pct=100.0,
        max_lots_per_ticker=2,
    )
```

**Step 4: Run backtest tests**

Run: `.venv/bin/pytest tests/backtest/ -v`
Expected: All pass

**Step 5: Commit**

```bash
git add scripts/run_backtest.py
git commit -m "feat: rewrite signal function with support+RSI+volume entry and trailing stop exit"
```

---

### Task 6: Run backtest and generate updated report

Integration test with IB Gateway.

**Step 1: Run small backtest to validate**

Run: `.venv/bin/python scripts/run_backtest.py --tickers 5 --years 2 --output-dir output`
Expected: Completes, saves JSON. Check that trades include new signal names (rsi, volume) and exit reasons (trailing_stop, hard_stop).

**Step 2: Generate HTML report**

Run: `.venv/bin/python scripts/visualize_backtest.py output/backtest_*.json`
Expected: HTML report with updated signal annotations showing RSI and volume values.

**Step 3: Open and review**

Run: `open output/backtest_*.html`
Expected: Report shows new strategy behavior — fewer trades, larger winners, trailing stop exits.

**Step 4: If satisfied, run full backtest**

Run: `.venv/bin/python scripts/run_backtest.py --tickers 50 --years 5 --output-dir output`

**Step 5: Commit**

```bash
git commit --allow-empty -m "chore: validate strategy redesign backtest pipeline"
```
