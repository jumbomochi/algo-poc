"""Transaction-cost model shared by the backtest simulator and the divergence monitor.

Two realism gaps this closes (2026-08-06 implementation review, finding 4.7):

1. **No per-order commission floor.** IB charges ``max($1, $0.005/share)`` per
   order. At the live account's size most orders are a handful of shares, so
   the floor — not the per-share rate — is the binding cost. Modelling only
   the per-share rate understated commission by 10-20x on small orders.
2. **One slippage number for every instrument.** A mega-cap equity and a thin
   thematic ETF do not cost the same to trade. Per-instrument slippage keeps
   the thin sleeves from looking as cheap as the liquid ones.

The commission formula is deliberately imported from the live funding module
(``estimate_commission_usd``) rather than re-derived, so the backtest and the
live pre-trade cash check can never disagree about what an order costs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from services.risk_management.funding import estimate_commission_usd
from shared.universe import DEFENSIVE_TICKERS, THEMATIC_ETFS

DEFAULT_SLIPPAGE_BPS = 10.0
DEFAULT_COMMISSION_PER_SHARE = 0.005
# IB's US-equities per-order minimum. See config/default.yaml
# ``currency.minimum_commission_usd``, which the live path already honours.
DEFAULT_COMMISSION_MINIMUM = 1.0

# Instruments whose quoted spread is materially wider than a mega-cap's.
# Inverse ETFs (SH/PSQ/SDS) are liquid but carry a persistent spread; the
# thematic ETF book (ARK family, GAMR, PRNT, IZRL, ...) is genuinely thin.
_INVERSE_ETFS = frozenset({"SH", "PSQ", "SDS"})
_THEMATIC_ETFS = frozenset(THEMATIC_ETFS)

# Multipliers on the base slippage rate, by liquidity tier. Conservative by
# construction: a tier is only ever *more* expensive than the base rate, never
# less, so a mis-classified ticker cannot make a backtest look cheaper.
LIQUIDITY_MULTIPLIERS: dict[str, float] = {
    "liquid": 1.0,
    "inverse_etf": 2.0,
    "thematic_etf": 2.5,
}


def liquidity_tier(ticker: str) -> str:
    """Classify a ticker into a liquidity tier used for slippage scaling."""
    if ticker in _INVERSE_ETFS:
        return "inverse_etf"
    if ticker in _THEMATIC_ETFS:
        return "thematic_etf"
    return "liquid"


def default_slippage_bps_by_ticker(base_bps: float) -> dict[str, float]:
    """Per-ticker slippage for every instrument the sleeves can trade.

    Only tickers that differ from ``base_bps`` are included, so the returned
    map stays small and readable in saved backtest configs.
    """
    tiered: dict[str, float] = {}
    for ticker in sorted(_THEMATIC_ETFS | _INVERSE_ETFS | set(DEFENSIVE_TICKERS)):
        multiplier = LIQUIDITY_MULTIPLIERS[liquidity_tier(ticker)]
        if multiplier != 1.0:
            tiered[ticker] = base_bps * multiplier
    return tiered


@dataclass(frozen=True)
class CostModel:
    """Per-order commission and per-instrument slippage assumptions."""

    slippage_bps: float = DEFAULT_SLIPPAGE_BPS
    commission_per_share: float = DEFAULT_COMMISSION_PER_SHARE
    commission_minimum: float = DEFAULT_COMMISSION_MINIMUM
    slippage_bps_by_ticker: Mapping[str, float] = field(default_factory=dict)

    @classmethod
    def with_liquidity_tiers(
        cls,
        *,
        slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
        commission_per_share: float = DEFAULT_COMMISSION_PER_SHARE,
        commission_minimum: float = DEFAULT_COMMISSION_MINIMUM,
    ) -> CostModel:
        """Build a model whose thin instruments cost more than the base rate."""
        return cls(
            slippage_bps=slippage_bps,
            commission_per_share=commission_per_share,
            commission_minimum=commission_minimum,
            slippage_bps_by_ticker=default_slippage_bps_by_ticker(slippage_bps),
        )

    def slippage_bps_for(self, ticker: str | None = None) -> float:
        """Slippage in basis points for one instrument."""
        if ticker is None:
            return self.slippage_bps
        return float(self.slippage_bps_by_ticker.get(ticker, self.slippage_bps))

    def commission_for(self, quantity: float) -> float:
        """Commission for one order: ``max(minimum, |qty| * per_share)``."""
        return estimate_commission_usd(
            quantity,
            per_share=self.commission_per_share,
            minimum=self.commission_minimum,
        )

    def to_dict(self) -> dict:
        """Serializable form, saved into backtest results for the monitor."""
        return {
            "slippage_bps": self.slippage_bps,
            "commission_per_share": self.commission_per_share,
            "commission_minimum": self.commission_minimum,
            "slippage_bps_by_ticker": dict(self.slippage_bps_by_ticker),
        }
