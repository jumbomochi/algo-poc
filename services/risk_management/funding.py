from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class FundingDecision:
    approved: bool
    required_usd: float
    remaining_usd: float
    reason: str


def estimate_commission_usd(
    quantity: float, *, per_share: float, minimum: float
) -> float:
    return max(float(minimum), abs(float(quantity)) * float(per_share))


def check_settled_usd_funding(
    *,
    order_notional_usd: float,
    settled_cash_usd: float | None,
    active_reservations_usd: float,
    estimated_commission_usd: float,
    minimum_reserve_usd: float,
) -> FundingDecision:
    try:
        settled_cash = float(settled_cash_usd)
    except (TypeError, ValueError):
        settled_cash = math.nan
    if not math.isfinite(settled_cash):
        return FundingDecision(
            approved=False,
            required_usd=math.inf,
            remaining_usd=-math.inf,
            reason="invalid settled USD cash",
        )

    try:
        requirements = tuple(
            float(value)
            for value in (
                order_notional_usd,
                active_reservations_usd,
                estimated_commission_usd,
                minimum_reserve_usd,
            )
        )
    except (TypeError, ValueError):
        requirements = (math.nan,)
    if not all(math.isfinite(value) for value in requirements):
        return FundingDecision(
            approved=False,
            required_usd=math.inf,
            remaining_usd=-math.inf,
            reason="invalid USD funding data",
        )

    required = sum(max(0.0, value) for value in requirements)
    remaining = settled_cash - required
    approved = remaining >= 0
    return FundingDecision(
        approved=approved,
        required_usd=required,
        remaining_usd=remaining,
        reason=(
            "settled USD cash available"
            if approved
            else "insufficient settled USD cash"
        ),
    )
