"""One-off guarded tool to flatten (close) all positions on the IB *paper*
account, used by the Path A re-baseline. Market-sells longs and buys back
shorts to bring the account to zero equity positions.

Safety:
- Refuses any account that is not a DU-prefixed paper account (never live).
- Dry-run by default; --apply requires an interactive TTY and an exact typed
  count confirmation.
- Run during US regular trading hours: market orders will not rest outside RTH.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass

from shared.config import load_config


class FlattenRefusedError(RuntimeError):
    """Raised when a safety guard blocks the flatten."""


@dataclass(frozen=True)
class FlattenOrder:
    con_id: int
    symbol: str
    action: str  # "SELL" (close long) or "BUY" (cover short)
    quantity: float


def plan_flatten(positions, *, account_id: str) -> list[FlattenOrder]:
    """Build the closing orders for every non-zero position on a paper account."""
    if not account_id.startswith("DU"):
        raise FlattenRefusedError(
            f"refusing to flatten non-paper account {account_id!r}"
        )
    plan: list[FlattenOrder] = []
    for p in positions:
        if str(p.account) != account_id:
            raise FlattenRefusedError(
                f"position from unexpected account {p.account!r}"
            )
        qty = float(p.position)
        if qty == 0:
            continue
        contract = p.contract
        symbol = str(getattr(contract, "localSymbol", None) or contract.symbol)
        plan.append(FlattenOrder(
            con_id=int(contract.conId),
            symbol=symbol,
            action="SELL" if qty > 0 else "BUY",
            quantity=abs(qty),
        ))
    return plan


def execute_flatten(ib, plan: list[FlattenOrder], *, confirm: str | None):
    """Place one market order per plan item. Requires exact count confirmation."""
    if confirm != str(len(plan)):
        raise FlattenRefusedError(
            f"exact confirmation required: expected '{len(plan)}', got {confirm!r}"
        )
    from ib_insync import MarketOrder, Stock

    trades = []
    for o in plan:
        # conId uniquely identifies the contract; no separate qualify needed.
        contract = Stock(conId=o.con_id, symbol=o.symbol, exchange="SMART", currency="USD")
        order = MarketOrder(o.action, o.quantity)
        trades.append(ib.placeOrder(contract, order))
    return trades


async def _run(apply: bool) -> int:
    from ib_insync import IB

    config = load_config("config/default.yaml")
    ib = IB()
    # ib_insync's synchronous API needs an implicit event loop, which Python
    # 3.14 no longer provides; drive it with the async API under asyncio.run.
    await ib.connectAsync(
        config.ib.host, config.ib.paper_port, clientId=105,
        readonly=not apply, timeout=20,
    )
    try:
        accounts = ib.managedAccounts()
        if len(accounts) != 1:
            raise FlattenRefusedError("expected exactly one managed account")
        account_id = str(accounts[0])
        plan = plan_flatten(await ib.reqPositionsAsync(), account_id=account_id)

        print(f"Account {account_id}: {len(plan)} position(s) to flatten")
        for o in plan:
            print(f"  {o.action} {o.quantity} {o.symbol} (con_id={o.con_id})")
        if not apply:
            print("\nDry-run only. Re-run with --apply during US market hours.")
            return 0
        if not plan:
            print("Nothing to flatten.")
            return 0
        if not sys.stdin.isatty():
            raise FlattenRefusedError("--apply requires an interactive TTY")
        answer = input(f"\nType {len(plan)} to place these market orders: ")
        trades = execute_flatten(ib, plan, confirm=answer.strip())
        await asyncio.sleep(2)
        for t in trades:
            print(f"  {t.order.action} {t.order.totalQuantity} "
                  f"{t.contract.symbol}: {t.orderStatus.status}")
        print(f"Placed {len(trades)} closing order(s).")
        return 0
    finally:
        if ib.isConnected():
            ib.disconnect()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Flatten all positions on the IB paper account (Path A)."
    )
    parser.add_argument("--apply", action="store_true",
                        help="Place the closing orders (default: dry-run report).")
    args = parser.parse_args(argv)
    return asyncio.run(_run(args.apply))


if __name__ == "__main__":
    raise SystemExit(main())
