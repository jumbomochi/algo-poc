"""Operator harness for the KAN-18 broker-stop prototype spike (DUN551088).

Answers the five questions the readiness design (D16) leaves open before
broker-native GTC stops can become the primary stop-loss protection:

1. Trigger semantics — which trigger method IB applies, and whether the stop
   arms outside regular trading hours.
2. Persistence — does a resting GTC stop survive an IB Gateway restart.
3. Visibility — are ``orderType`` / ``auxPrice`` / ``tif`` readable through the
   API, given ``BrokerOpenOrder`` (shared/broker_state.py:19) captures none of
   them today.
4. Cancel-all interaction — does the kill path's ``cancel_all_orders``
   (services/execution/order_manager.py:429) reach a resting stop.
5. Whole-share interaction — can a stop cover the exact held quantity given
   ``IBExecutor._effective_quantity`` truncation (ib_executor.py:142).

This is a spike harness, not production code: nothing here is imported by a
service. The findings land in ``docs/operations/broker-stop-prototype.md``.

Safety:
- Refuses any account that is not a DU-prefixed paper account (never live).
- Every phase is read-only unless ``--apply`` is passed; ``--apply`` requires an
  interactive TTY and an exact typed confirmation.
- Orders are stamped with an ``ORDERREF_PREFIX`` orderRef so ``cleanup`` can
  find and cancel exactly the spike's artifacts and nothing else.
- A SELL stop priced at or above the last trade triggers immediately. That is
  refused unless ``--allow-trigger`` is passed, because it sells real shares
  from the paper book.

Usage (see docs/operations/broker-stop-prototype.md for the full runbook)::

    python -m scripts.ops.broker_stop_spike positions
    python -m scripts.ops.broker_stop_spike place --symbol CSCO --apply
    python -m scripts.ops.broker_stop_spike observe
    python -m scripts.ops.broker_stop_spike cancel-probe
    python -m scripts.ops.broker_stop_spike cleanup --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from dataclasses import asdict, dataclass

from shared.config import load_config

# Stamped on every order this harness places. `cleanup` cancels exactly the
# orders carrying it, so a mis-typed cleanup can never touch a real order.
ORDERREF_PREFIX = "kan18-spike"

# Distinct from every other client id in the repo (execution 1, data 2,
# backtest/paper 10+, flatten 105) so the spike never evicts a live session.
DEFAULT_CLIENT_ID = 118
# Second id for the cross-client visibility check (question 3/4).
PEER_CLIENT_ID = 119

# The order fields the spike must observe to answer questions 1-3. Anything IB
# leaves unset shows up as a bare value in the dump rather than being hidden.
OBSERVED_ORDER_FIELDS = (
    "orderId",
    "permId",
    "clientId",
    "account",
    "orderRef",
    "action",
    "orderType",
    "totalQuantity",
    "auxPrice",
    "lmtPrice",
    "tif",
    "goodTillDate",
    "triggerMethod",
    "outsideRth",
    "transmit",
    "parentId",
    "ocaGroup",
    "trailStopPrice",
    "trailingPercent",
)

OBSERVED_STATUS_FIELDS = (
    "status",
    "filled",
    "remaining",
    "avgFillPrice",
    "lastFillPrice",
    "whyHeld",
)

# What BrokerOpenOrder (shared/broker_state.py:19) records today. Fields in
# OBSERVED_ORDER_FIELDS that are absent here are the gap KAN-20's verifier
# would have to close.
BROKER_OPEN_ORDER_FIELDS = (
    "orderId",
    "account",
    "action",
    "totalQuantity",
)


class SpikeRefusedError(RuntimeError):
    """Raised when a safety guard blocks a spike action."""


@dataclass(frozen=True)
class StopPlan:
    """One GTC stop order to place against a held paper position."""

    con_id: int
    symbol: str
    action: str  # always "SELL": the spike protects long positions
    quantity: float
    stop_price: float
    order_ref: str
    held_quantity: float
    uncovered_quantity: float
    triggers_immediately: bool


def order_ref_for(symbol: str) -> str:
    """Stable orderRef for a symbol, so a re-run is recognisable, not doubled."""
    return f"{ORDERREF_PREFIX}-{symbol.upper()}"


def ips_stop_price(last_price: float, trailing_pct: float) -> float:
    """Stop level from the IPS § 6 trailing rule (`stop_loss_trailing_pct`).

    Rounded down to a whole cent: rounding up could place the stop above the
    intended level and trigger earlier than the policy says.
    """
    if last_price <= 0:
        raise SpikeRefusedError(f"last price must be positive, got {last_price}")
    if not 0 < trailing_pct < 100:
        raise SpikeRefusedError(
            f"trailing pct must be in (0, 100), got {trailing_pct}"
        )
    return math.floor(last_price * (1.0 - trailing_pct / 100.0) * 100) / 100


def stop_coverage(
    held_quantity: float, *, allow_fractional: bool
) -> tuple[float, float]:
    """Split a held quantity into (coverable, uncovered) under IB sizing rules.

    Mirrors ``IBExecutor._effective_quantity`` (ib_executor.py:142): without
    fractional support a fractional holding truncates to whole shares, and the
    remainder is a position no broker stop protects. Question 5 of the spike.
    """
    held = float(held_quantity)
    if held <= 0:
        raise SpikeRefusedError(f"held quantity must be positive, got {held}")
    if allow_fractional or held.is_integer():
        return held, 0.0
    coverable = float(int(held))
    if coverable <= 0:
        raise SpikeRefusedError(
            f"held quantity {held} truncates to zero whole shares; "
            "no stop can be placed"
        )
    return coverable, round(held - coverable, 8)


def plan_stop(
    positions,
    *,
    account_id: str,
    symbol: str,
    last_price: float,
    trailing_pct: float,
    quantity: float | None = None,
    stop_price: float | None = None,
    allow_fractional: bool = False,
    allow_trigger: bool = False,
) -> StopPlan:
    """Build the single GTC stop the spike will place.

    Refuses a live account, an unheld symbol, an oversized quantity, and — by
    default — a stop priced where IB would trigger it on arrival.
    """
    if not account_id.startswith("DU"):
        raise SpikeRefusedError(
            f"refusing to place a spike order on non-paper account {account_id!r}"
        )

    wanted = symbol.upper()
    for position in positions:
        if str(position.account) != account_id:
            raise SpikeRefusedError(
                f"position from unexpected account {position.account!r}"
            )
    matches = [
        position
        for position in positions
        if _symbol_of(position.contract).upper() == wanted
        and float(position.position) != 0
    ]
    if not matches:
        raise SpikeRefusedError(f"no open position in {wanted} on {account_id}")
    if len(matches) > 1:
        raise SpikeRefusedError(f"multiple positions in {wanted} on {account_id}")

    position = matches[0]
    held = float(position.position)
    if held < 0:
        raise SpikeRefusedError(
            f"{wanted} is short ({held}); the spike only covers long positions"
        )

    coverable, uncovered = stop_coverage(held, allow_fractional=allow_fractional)
    placed = coverable if quantity is None else float(quantity)
    if placed <= 0:
        raise SpikeRefusedError(f"stop quantity must be positive, got {placed}")
    if placed > held:
        raise SpikeRefusedError(
            f"stop quantity {placed} exceeds the {held} shares held in {wanted}"
        )
    if quantity is not None:
        uncovered = round(held - placed, 8)

    price = (
        ips_stop_price(last_price, trailing_pct)
        if stop_price is None
        else round(float(stop_price), 2)
    )
    if price <= 0:
        raise SpikeRefusedError(f"stop price must be positive, got {price}")

    triggers_immediately = price >= last_price
    if triggers_immediately and not allow_trigger:
        raise SpikeRefusedError(
            f"stop price {price} is at or above the last trade {last_price}: "
            f"IB would trigger it on arrival and sell {placed} {wanted}. "
            "Pass --allow-trigger only when the trigger observation is the point."
        )

    return StopPlan(
        con_id=int(position.contract.conId),
        symbol=wanted,
        action="SELL",
        quantity=placed,
        stop_price=price,
        order_ref=order_ref_for(wanted),
        held_quantity=held,
        uncovered_quantity=uncovered,
        triggers_immediately=triggers_immediately,
    )


def build_stop_order(plan: StopPlan):
    """Materialise the ib_insync order for a plan. Kept separate so the plan
    stays testable without ib_insync installed."""
    from ib_insync import StopOrder

    order = StopOrder(plan.action, plan.quantity, plan.stop_price, tif="GTC")
    order.orderRef = plan.order_ref
    # Leave triggerMethod and outsideRth at IB's defaults on purpose: what the
    # defaults ARE is question 1, and forcing them would answer nothing.
    return order


def place_stop(ib, plan: StopPlan, *, confirm: str | None):
    """Place the planned stop. Requires the exact symbol typed back."""
    if confirm != plan.symbol:
        raise SpikeRefusedError(
            f"exact confirmation required: expected {plan.symbol!r}, got {confirm!r}"
        )
    from ib_insync import Stock

    contract = Stock(
        conId=plan.con_id, symbol=plan.symbol, exchange="SMART", currency="USD"
    )
    return ib.placeOrder(contract, build_stop_order(plan))


def describe_order(trade) -> dict:
    """Flatten one Trade into the evidence record the write-up quotes.

    Every field is read with ``getattr`` and reported even when unset — "IB
    returned nothing here" is itself an observation, and silently dropping a
    field would let an inference pass as evidence.
    """
    order = trade.order
    status = trade.orderStatus
    contract = trade.contract
    record: dict = {
        "symbol": _symbol_of(contract),
        "con_id": int(getattr(contract, "conId", 0) or 0),
        "sec_type": str(getattr(contract, "secType", "") or ""),
    }
    for field in OBSERVED_ORDER_FIELDS:
        record[field] = _plain(getattr(order, field, None))
    for field in OBSERVED_STATUS_FIELDS:
        record[field] = _plain(getattr(status, field, None))
    return record


def missing_from_broker_open_order(record: dict) -> list[str]:
    """Which observed fields BrokerOpenOrder cannot currently carry.

    Only fields IB actually populated count: a field IB leaves empty is not a
    reader gap, it is an absent value. Question 3 of the spike.
    """
    return [
        field
        for field in OBSERVED_ORDER_FIELDS
        if field not in BROKER_OPEN_ORDER_FIELDS
        and record.get(field) not in (None, "", 0, 0.0, False)
    ]


def select_spike_orders(trades, *, prefix: str = ORDERREF_PREFIX) -> list:
    """The subset of open trades this harness placed, by orderRef stamp."""
    return [
        trade
        for trade in trades
        if str(getattr(trade.order, "orderRef", "") or "").startswith(prefix)
    ]


def _symbol_of(contract) -> str:
    return str(getattr(contract, "localSymbol", None) or contract.symbol)


def _plain(value):
    """Coerce an IB value into something json.dumps can render verbatim."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _require_paper_account(ib) -> str:
    accounts = list(ib.managedAccounts())
    if len(accounts) != 1:
        raise SpikeRefusedError("expected exactly one managed account")
    account_id = str(accounts[0])
    if not account_id.startswith("DU"):
        raise SpikeRefusedError(
            f"refusing to run the spike against non-paper account {account_id!r}"
        )
    return account_id


def _confirm(prompt: str) -> str:
    if not sys.stdin.isatty():
        raise SpikeRefusedError("--apply requires an interactive TTY")
    return input(prompt).strip()


async def _last_price(ib, contract) -> float:
    """Last trade price, falling back to the midpoint when the tape is closed."""
    [ticker] = await ib.reqTickersAsync(contract)
    for candidate in (ticker.last, ticker.close, ticker.markPrice):
        value = float(candidate) if candidate is not None else float("nan")
        if math.isfinite(value) and value > 0:
            return value
    bid = float(ticker.bid or float("nan"))
    ask = float(ticker.ask or float("nan"))
    if math.isfinite(bid) and math.isfinite(ask) and bid > 0 and ask > 0:
        return (bid + ask) / 2
    raise SpikeRefusedError(
        f"no usable price for {_symbol_of(contract)}; "
        "run during market hours or pass --stop-price"
    )


def _dump(label: str, payload) -> None:
    print(f"\n=== {label} ===")
    print(json.dumps(payload, indent=2, sort_keys=False, default=str))


async def _connect(config, *, client_id: int, readonly: bool):
    from ib_insync import IB

    ib = IB()
    # ib_insync's synchronous API needs an implicit event loop, which Python
    # 3.14 no longer provides; drive it with the async API under asyncio.run.
    await ib.connectAsync(
        config.ib.host,
        config.ib.paper_port,
        clientId=client_id,
        readonly=readonly,
        timeout=20,
    )
    return ib


async def _cmd_positions(ib, config, args) -> int:
    account_id = _require_paper_account(ib)
    positions = [
        position
        for position in await ib.reqPositionsAsync()
        if float(position.position) > 0
        and str(getattr(position.contract, "secType", "")) == "STK"
    ]
    print(f"Account {account_id}: {len(positions)} long equity position(s)")
    trailing = config.risk.stop_loss_trailing_pct
    rows = []
    for position in positions:
        held = float(position.position)
        try:
            coverable, uncovered = stop_coverage(
                held, allow_fractional=args.allow_fractional
            )
        except SpikeRefusedError as exc:
            # One unstoppable holding must not hide the rest of the book.
            coverable, uncovered = 0.0, held
            print(f"  {_symbol_of(position.contract):<8} UNPROTECTABLE: {exc}")
        row = {
            "symbol": _symbol_of(position.contract),
            "con_id": int(position.contract.conId),
            "held": held,
            "coverable_by_stop": coverable,
            "uncovered": uncovered,
            "avg_cost": float(position.avgCost),
            "ips_stop_from_avg_cost": ips_stop_price(
                float(position.avgCost), trailing
            ),
        }
        rows.append(row)
        print(
            f"  {row['symbol']:<8} held={held:<10g} "
            f"stop_qty={coverable:<10g} uncovered={uncovered:g}"
        )
    _dump(f"positions ({trailing}% IPS trailing rule)", rows)
    return 0


async def _cmd_place(ib, config, args) -> int:
    from ib_insync import Stock

    account_id = _require_paper_account(ib)
    positions = list(await ib.reqPositionsAsync())

    last_price = args.last_price
    if last_price is None and args.stop_price is None:
        [contract] = await ib.qualifyContractsAsync(
            Stock(args.symbol.upper(), "SMART", "USD")
        )
        last_price = await _last_price(ib, contract)
    elif last_price is None:
        # Only used for the triggers-immediately guard; --allow-trigger is the
        # explicit opt-out, so an unknown tape must not silently disable it.
        raise SpikeRefusedError(
            "--stop-price also needs --last-price so the immediate-trigger "
            "guard has a reference; pass both."
        )

    plan = plan_stop(
        positions,
        account_id=account_id,
        symbol=args.symbol,
        last_price=last_price,
        trailing_pct=config.risk.stop_loss_trailing_pct,
        quantity=args.quantity,
        stop_price=args.stop_price,
        allow_fractional=args.allow_fractional,
        allow_trigger=args.allow_trigger,
    )
    _dump("planned stop", {**asdict(plan), "last_price": last_price})
    if plan.uncovered_quantity:
        print(
            f"WARNING: {plan.uncovered_quantity:g} share(s) of {plan.symbol} "
            "would be left with no broker stop (question 5)."
        )
    if plan.triggers_immediately:
        print(
            f"WARNING: this stop triggers on arrival and will SELL "
            f"{plan.quantity:g} {plan.symbol} at market."
        )
    if not args.apply:
        print("\nDry-run only. Re-run with --apply to place it.")
        return 0

    trade = place_stop(
        ib, plan, confirm=_confirm(f"\nType {plan.symbol} to place this stop: ")
    )
    await asyncio.sleep(3)
    _dump("placed stop", describe_order(trade))
    print(
        f"Placed order {trade.order.orderId} "
        f"(orderRef={plan.order_ref}); status={trade.orderStatus.status}"
    )
    return 0


async def _cmd_observe(ib, config, args) -> int:
    account_id = _require_paper_account(ib)

    own = [describe_order(trade) for trade in ib.openTrades()]
    _dump(f"{account_id}: openTrades() on client {args.client_id}", own)

    all_open = [describe_order(trade) for trade in await ib.reqAllOpenOrdersAsync()]
    _dump(f"{account_id}: reqAllOpenOrders()", all_open)

    gaps = {
        record.get("orderRef") or record.get("orderId"): missing_from_broker_open_order(
            record
        )
        for record in all_open
    }
    _dump("populated fields BrokerOpenOrder cannot carry today", gaps)

    if args.cross_client:
        # The execution service's find_order_by_ref/cancel_order only see
        # openTrades() for their OWN session. A second client shows whether a
        # stop placed elsewhere is reachable at all (questions 3 and 4).
        peer = await _connect(config, client_id=PEER_CLIENT_ID, readonly=True)
        try:
            await asyncio.sleep(2)
            peer_own = [describe_order(trade) for trade in peer.openTrades()]
            _dump(
                f"peer client {PEER_CLIENT_ID}: openTrades() "
                "BEFORE reqAllOpenOrders",
                peer_own,
            )
            peer_all = [
                describe_order(trade) for trade in await peer.reqAllOpenOrdersAsync()
            ]
            _dump(f"peer client {PEER_CLIENT_ID}: reqAllOpenOrders()", peer_all)
            _dump(
                f"peer client {PEER_CLIENT_ID}: openTrades() "
                "AFTER reqAllOpenOrders",
                [describe_order(trade) for trade in peer.openTrades()],
            )
        finally:
            if peer.isConnected():
                peer.disconnect()
    return 0


async def _cmd_cancel_probe(ib, config, args) -> int:
    """Question 4: can the kill path's cancel-all reach a resting stop?

    Drives the production objects — ``IBExecutor`` + ``OrderManager`` — rather
    than a re-implementation, so the answer is about the real kill path.
    ``cancel_all_orders`` only walks ``OrderManager.open_orders``, and
    ``IBExecutor.cancel_order`` no-ops for an order absent from its ``_trades``
    map, so the reachability question is decided by ``find_order_by_ref``.
    """
    from services.execution.ib_executor import IBExecutor
    from services.execution.order_manager import OrderManager

    account_id = _require_paper_account(ib)
    resting = select_spike_orders(await ib.reqAllOpenOrdersAsync())
    _dump(
        f"{account_id}: resting spike stops before the probe",
        [describe_order(trade) for trade in resting],
    )
    if not resting:
        print("No resting spike stop to probe — run `place --apply` first.")
        return 1

    executor = IBExecutor(
        config.ib.host, config.ib.paper_port, client_id=args.client_id + 10
    )
    await executor.connect(expect_paper=True)
    try:
        findings = {}
        for trade in resting:
            ref = str(trade.order.orderRef)
            found = await executor.find_order_by_ref(ref)
            findings[ref] = {
                "ib_order_id": str(trade.order.orderId),
                "find_order_by_ref": found,
                "reachable_by_kill_path": found is not None,
            }
        _dump("executor recovery view of the resting stops", findings)

        if not args.apply:
            print(
                "\nDry-run only. Re-run with --apply to drive "
                "OrderManager.cancel_all_orders() against these stops."
            )
            return 0

        manager = OrderManager(
            executor=executor, redis_client=None, db_session=None
        )
        for ref, finding in findings.items():
            if not finding["reachable_by_kill_path"]:
                continue
            manager.restore_submission(
                ref,
                finding["ib_order_id"],
                ticker=ref.rsplit("-", 1)[-1],
                quantity=0.0,
                limit_price=None,
            )
        tracked = sorted(manager.open_orders)
        print(f"\nOrderManager tracks {len(tracked)} spike order(s): {tracked}")
        _confirm("Type cancel-all to run the kill path's cancel_all_orders(): ")
        cancelled = await manager.cancel_all_orders()
        await asyncio.sleep(3)
        _dump("cancel_all_orders() returned", cancelled)
        _dump(
            "resting spike stops AFTER cancel_all_orders()",
            [
                describe_order(trade)
                for trade in select_spike_orders(await ib.reqAllOpenOrdersAsync())
            ],
        )
    finally:
        await executor.disconnect()
    return 0


async def _cmd_cleanup(ib, config, args) -> int:
    account_id = _require_paper_account(ib)
    resting = select_spike_orders(await ib.reqAllOpenOrdersAsync())
    _dump(
        f"{account_id}: spike orders still resting",
        [describe_order(trade) for trade in resting],
    )
    if not resting:
        print("Nothing to clean up — the account is flat of spike artifacts.")
        return 0
    if not args.apply:
        print(f"\nDry-run only. Re-run with --apply to cancel {len(resting)} order(s).")
        return 0

    answer = _confirm(f"\nType {len(resting)} to cancel these spike orders: ")
    if answer != str(len(resting)):
        raise SpikeRefusedError(
            f"exact confirmation required: expected '{len(resting)}', got {answer!r}"
        )
    for trade in resting:
        ib.cancelOrder(trade.order)
    await asyncio.sleep(3)
    remaining = select_spike_orders(await ib.reqAllOpenOrdersAsync())
    _dump(
        "spike orders remaining after cancel",
        [describe_order(trade) for trade in remaining],
    )
    if remaining:
        print(f"WARNING: {len(remaining)} spike order(s) still resting.")
        return 1
    print(f"Cancelled {len(resting)} spike order(s); account is flat of artifacts.")
    return 0


COMMANDS = {
    "positions": _cmd_positions,
    "place": _cmd_place,
    "observe": _cmd_observe,
    "cancel-probe": _cmd_cancel_probe,
    "cleanup": _cmd_cleanup,
}


async def _run(args) -> int:
    config = load_config("config/default.yaml")
    if config.mode != "paper":
        raise SpikeRefusedError(
            f"config mode is {config.mode!r}; the spike runs against paper only"
        )
    # `cancel-probe` opens its own writable executor session; its outer
    # connection stays read-only.
    readonly = not args.apply or args.command == "cancel-probe"
    ib = await _connect(config, client_id=args.client_id, readonly=readonly)
    try:
        return await COMMANDS[args.command](ib, config, args)
    finally:
        if ib.isConnected():
            ib.disconnect()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="KAN-18 broker-stop prototype spike (paper account only)."
    )
    parser.add_argument(
        "command", choices=sorted(COMMANDS), help="Spike phase to run."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Perform the phase's write action (default: dry-run report).",
    )
    parser.add_argument("--symbol", help="Ticker to place the stop on (place).")
    parser.add_argument(
        "--quantity",
        type=float,
        help="Stop quantity (place). Defaults to the whole coverable holding.",
    )
    parser.add_argument(
        "--stop-price",
        type=float,
        help="Explicit stop price (place). Defaults to the IPS trailing level.",
    )
    parser.add_argument(
        "--last-price",
        type=float,
        help="Reference last trade, required alongside --stop-price.",
    )
    parser.add_argument(
        "--allow-trigger",
        action="store_true",
        help="Permit a stop that triggers on arrival — it SELLS shares.",
    )
    parser.add_argument(
        "--allow-fractional",
        action="store_true",
        help="Treat the account as fractional-capable when sizing stops.",
    )
    parser.add_argument(
        "--cross-client",
        action="store_true",
        help="Also report what a second API client sees (observe).",
    )
    parser.add_argument(
        "--client-id", type=int, default=DEFAULT_CLIENT_ID, help="IB API client id."
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "place" and not args.symbol:
        build_parser().error("place requires --symbol")
    try:
        return asyncio.run(_run(args))
    except SpikeRefusedError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
