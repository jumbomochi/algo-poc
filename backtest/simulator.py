from __future__ import annotations


class SimulatedExecutor:
    """Simulates order execution with configurable slippage and commission.

    Designed for backtesting: fills limit entries when the bar's low reaches
    the limit price, and fills market exits at the bar's open.
    """

    def __init__(self, slippage_bps: int, commission_per_share: float) -> None:
        self.slippage_bps = slippage_bps
        self.commission_per_share = commission_per_share

    def try_fill_limit_entry(
        self,
        limit_price: float,
        quantity: int,
        bar: dict,
    ) -> dict | None:
        """Attempt to fill a limit buy entry order against a bar.

        Fills if bar["low"] <= limit_price (price was reachable during the bar).
        Fill price includes slippage: limit_price * (1 + slippage_bps / 10_000)
        for buys (slippage makes entry worse).

        Returns:
            Fill dict with keys: filled, fill_price, quantity, commission, date.
            None if the limit price was not reachable.
        """
        if bar["low"] > limit_price:
            return None

        slippage_multiplier = 1 + self.slippage_bps / 10_000
        fill_price = limit_price * slippage_multiplier

        return {
            "filled": True,
            "fill_price": fill_price,
            "quantity": quantity,
            "commission": quantity * self.commission_per_share,
            "date": bar["date"],
        }

    def fill_market_exit(
        self,
        quantity: int,
        bar: dict,
    ) -> dict:
        """Fill a market sell exit order at the bar's open.

        Always fills. Fill price includes slippage:
        bar["open"] * (1 - slippage_bps / 10_000) for sells (slippage makes
        exit worse).

        Returns:
            Fill dict with keys: filled, fill_price, quantity, commission, date.
        """
        slippage_multiplier = 1 - self.slippage_bps / 10_000
        fill_price = bar["open"] * slippage_multiplier

        return {
            "filled": True,
            "fill_price": fill_price,
            "quantity": quantity,
            "commission": quantity * self.commission_per_share,
            "date": bar["date"],
        }
