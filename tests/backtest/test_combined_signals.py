from __future__ import annotations


def test_combined_prefers_mean_reversion():
    """When mean-reversion produces a signal, it takes priority."""
    from scripts.run_backtest import make_combined_signals_fn

    def mr_fn(ticker, bars):
        return {"action": "buy", "ticker": ticker, "signals": {"strategy": "mean_reversion"}}

    def mom_fn(ticker, bars):
        return {"action": "buy", "ticker": ticker, "signals": {"strategy": "momentum"}}

    combined = make_combined_signals_fn(mr_fn, mom_fn)
    signal = combined("AAPL", [{"close": 150}])
    assert signal["signals"]["strategy"] == "mean_reversion"


def test_combined_falls_through_to_momentum():
    """When mean-reversion returns None, momentum is checked."""
    from scripts.run_backtest import make_combined_signals_fn

    def mr_fn(ticker, bars):
        return None

    def mom_fn(ticker, bars):
        return {"action": "buy", "ticker": ticker, "signals": {"strategy": "momentum"}}

    combined = make_combined_signals_fn(mr_fn, mom_fn)
    signal = combined("AAPL", [{"close": 150}])
    assert signal is not None
    assert signal["signals"]["strategy"] == "momentum"


def test_combined_sell_takes_priority():
    """If one strategy says sell and the other says buy, sell wins."""
    from scripts.run_backtest import make_combined_signals_fn

    def mr_fn(ticker, bars):
        return {"action": "sell", "ticker": ticker, "exit_reason": "trailing_stop"}

    def mom_fn(ticker, bars):
        return {"action": "buy", "ticker": ticker, "signals": {"strategy": "momentum"}}

    combined = make_combined_signals_fn(mr_fn, mom_fn)
    signal = combined("AAPL", [{"close": 150}])
    assert signal["action"] == "sell"
