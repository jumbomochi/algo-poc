from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal


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


#: Default ``currency.sleeve_cash_buffer_bps`` (KAN-111). Headroom kept on top
#: of an order's limit notional when it is sized against its sleeve's ledger
#: cash. Live, a DAY limit buy never fills above its limit (execution's sweep
#: cancels, it does not reprice upward), so the buffer covers what the estimate
#: can get wrong: IB's pass-through fees over ``estimate_commission_usd`` and
#: rounding. 25 bps is also the backtest's dearest slippage tier (thematic ETFs,
#: 2.5 x 10 bps in ``backtest.costs``), so the replay — which fills a limit buy
#: at up to ``limit * (1 + slippage)`` — can never book a buy this check funded
#: below zero cash either. Cost: at most 0.25% of a buy's notional left idle.
DEFAULT_SLEEVE_CASH_BUFFER_BPS = 25.0

_QUANTITY_STEP = Decimal("0.0001")


@dataclass(frozen=True)
class SleeveCashDecision:
    """What a sleeve's ledger cash allows of one proposed buy.

    ``quantity`` is the quantity to place: the request unchanged when it fits,
    smaller when it was downsized, 0 when the buy must be skipped.
    ``available_usd`` is the cash left for this order after other commitments;
    ``required_usd`` is what the *requested* quantity would have needed.
    """

    approved: bool
    quantity: float
    requested_quantity: float
    available_usd: float
    required_usd: float
    reason: str

    @property
    def downsized(self) -> bool:
        return self.approved and self.quantity < self.requested_quantity


def sleeve_buy_cost_usd(
    quantity: float,
    price: float,
    *,
    per_share: float,
    minimum: float,
    buffer_bps: float,
) -> float:
    """Worst-case cash a buy can take from its sleeve.

    The limit notional grown by the slippage buffer, plus the estimated
    commission. This is what ``size_to_sleeve_cash`` holds every order to, and
    what a run must remember for each buy it has already accepted.
    """
    notional = abs(float(quantity)) * float(price)
    return notional * (1.0 + float(buffer_bps) / 10_000.0) + estimate_commission_usd(
        quantity, per_share=per_share, minimum=minimum
    )


def size_to_sleeve_cash(
    *,
    quantity: float,
    price: float,
    sleeve_cash_usd: float | None,
    committed_usd: float,
    per_share: float,
    minimum: float,
    buffer_bps: float,
    whole_shares: bool,
) -> SleeveCashDecision:
    """Cap one buy at what its sleeve's ledger cash can actually pay for (KAN-111).

    The fill projector refuses any buy fill that would take the sleeve's cash
    (``portfolio_configs.cash``) below zero, so a buy the sleeve cannot fund is
    worse than no buy: it fills at IB, the book refuses it, and reconciliation
    disables entries. Cash is therefore a hard sizing constraint, separate from
    and in addition to the NAV-exposure limits the risk engine applies.

    ``committed_usd`` is everything already spoken for: active buy orders'
    unfilled notional and commission, plus whatever this run has already
    accepted (at :func:`sleeve_buy_cost_usd`). Same-day sell proceeds are NOT
    cash here — the projector only credits them when the sell fills, and the
    buy can fill first.

    A buy that fits is returned unchanged. One that does not is downsized to the
    largest quantity that fits — whole shares when the account cannot trade
    fractions, else the risk engine's 0.0001 step — and skipped when even the
    smallest order (1 share, or 0.0001) does not fit. Unusable inputs fail
    closed.
    """
    try:
        values = tuple(
            float(v)
            for v in (
                quantity, price, sleeve_cash_usd, committed_usd,
                per_share, minimum, buffer_bps,
            )
        )
    except (TypeError, ValueError):
        values = (math.nan,)
    if not all(math.isfinite(v) for v in values) or values[1] <= 0 or values[0] <= 0:
        return SleeveCashDecision(
            approved=False,
            quantity=0.0,
            requested_quantity=float(quantity) if _is_number(quantity) else 0.0,
            available_usd=-math.inf,
            required_usd=math.inf,
            reason="sleeve cash or order data unusable",
        )
    requested, price, cash, committed, per_share, minimum, buffer_bps = values
    per_share, minimum, buffer_bps = (
        max(0.0, per_share), max(0.0, minimum), max(0.0, buffer_bps)
    )

    def cost(q: float) -> float:
        return sleeve_buy_cost_usd(
            q, price, per_share=per_share, minimum=minimum, buffer_bps=buffer_bps
        )

    available = cash - max(0.0, committed)
    required = cost(requested)
    if required <= available:
        return SleeveCashDecision(
            approved=True,
            quantity=requested,
            requested_quantity=requested,
            available_usd=available,
            required_usd=required,
            reason="sleeve cash available",
        )

    # cost(q) = q * p' + max(minimum, q * per_share) is increasing in q, so it
    # fits iff both q * p' + minimum and q * (p' + per_share) fit.
    unit = price * (1.0 + buffer_bps / 10_000.0)
    largest = max(0.0, min((available - minimum) / unit, available / (unit + per_share)))
    step = Decimal(1) if whole_shares else _QUANTITY_STEP
    fitted = float(
        (Decimal(repr(largest)) / step).to_integral_value(rounding=ROUND_DOWN) * step
    )
    fitted = min(fitted, requested)
    # Float rounding at the boundary must never round a buy *into* overdraft.
    while fitted > 0 and cost(fitted) > available:
        fitted = float(Decimal(repr(fitted)) - step)
    smallest = float(step)
    if fitted < smallest:
        return SleeveCashDecision(
            approved=False,
            quantity=0.0,
            requested_quantity=requested,
            available_usd=available,
            required_usd=required,
            reason=(
                f"insufficient sleeve cash: have ${available:,.2f}, "
                f"need ${required:,.2f}"
                + (
                    f"; {smallest:g} share needs ${cost(smallest):,.2f}"
                    if smallest < requested
                    else ""
                )
            ),
        )
    return SleeveCashDecision(
        approved=True,
        quantity=fitted,
        requested_quantity=requested,
        available_usd=available,
        required_usd=required,
        reason=(
            f"downsized to sleeve cash: have ${available:,.2f}, "
            f"need ${required:,.2f}"
        ),
    )


def _is_number(value: object) -> bool:
    try:
        return math.isfinite(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
