from __future__ import annotations

from backtest.costs import CostModel


class SimulatedExecutor:
    """Fills backtest orders against a bar, using a :class:`CostModel`.

    **Next-bar semantics.** The caller must pass the bar *after* the one the
    decision was made on. A signal computed from ``close[t]`` cannot be filled
    before ``open[t+1]`` — the executor never sees the decision bar, so it
    cannot fill inside it. (Before the 2026-08-06 review this class was handed
    the decision bar itself, which filled entries at the same day's low and
    exits at the same day's open: findings 4.2 and 4.3.)

    Limit entries follow day-order semantics: reachable within the bar or gone.
    Market exits always fill at the bar's open.
    """

    def __init__(self, cost_model: CostModel) -> None:
        self.cost_model = cost_model

    def try_fill_limit_entry(
        self,
        limit_price: float,
        quantity: float,
        bar: dict,
        ticker: str | None = None,
    ) -> dict | None:
        """Attempt to fill a resting limit buy against the next session's bar.

        - ``open <= limit``: the market opened through the limit, so the order
          fills at the open (better than the limit price).
        - ``low <= limit < open``: the limit was touched intraday and fills at
          the limit price.
        - otherwise: the price was never reachable and the day order expires.

        Slippage makes the buy worse: ``price * (1 + slippage_bps / 10_000)``.

        Returns:
            Fill dict with keys: filled, fill_price, quantity, commission, date.
            None if the limit price was not reachable during the bar.
        """
        if bar["open"] <= limit_price:
            base_price = bar["open"]
        elif bar["low"] <= limit_price:
            base_price = limit_price
        else:
            return None

        slippage_multiplier = 1 + self.cost_model.slippage_bps_for(ticker) / 10_000
        fill_price = base_price * slippage_multiplier

        return {
            "filled": True,
            "fill_price": fill_price,
            "quantity": quantity,
            "commission": self.cost_model.commission_for(quantity),
            "date": bar["date"],
        }

    def fill_market_exit(
        self,
        quantity: float,
        bar: dict,
        ticker: str | None = None,
    ) -> dict:
        """Fill a market sell exit at the next session's open.

        Always fills. Slippage makes the sell worse:
        ``open * (1 - slippage_bps / 10_000)``.

        Returns:
            Fill dict with keys: filled, fill_price, quantity, commission, date.
        """
        slippage_multiplier = 1 - self.cost_model.slippage_bps_for(ticker) / 10_000
        fill_price = bar["open"] * slippage_multiplier

        return {
            "filled": True,
            "fill_price": fill_price,
            "quantity": quantity,
            "commission": self.cost_model.commission_for(quantity),
            "date": bar["date"],
        }
