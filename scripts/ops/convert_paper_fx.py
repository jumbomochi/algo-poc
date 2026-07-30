from __future__ import annotations

"""Convert SGD -> USD cash on the PAPER IB account (Path A funding step).

The dual-currency funding gate (services/risk_management/funding.py) only funds
USD-stock buys from *settled USD cash*. After the Path A re-baseline the paper
account holds ~1M SGD but ~0 USD, so every buy is skipped for "insufficient
settled USD cash". This tool places one FX order (BUY USD.SGD) to fund USD cash.

Safety (mirrors scripts/ops/retire_legacy_positions.py):
  * Dry-run by default; --apply required to place the order.
  * PAPER ONLY: refuses any account whose id does not start with "DU".
  * --apply requires an interactive TTY and an exact typed confirmation
    (the USD amount). No bypass (yes | / --force) permitted.
"""

import argparse
import asyncio
import math
import sys


class FxRefusedError(RuntimeError):
    """Raised when a safety guard blocks the FX conversion."""


def _usd_cash(summary) -> float:
    for row in summary:
        if row.tag == "TotalCashBalance" and row.currency == "USD":
            return float(row.value)
    return 0.0


def _cash(summary, currency: str) -> float:
    for row in summary:
        if row.tag == "TotalCashBalance" and row.currency == currency:
            return float(row.value)
    return 0.0


async def convert(
    *, host: str, port: int, client_id: int, usd_amount: float, apply: bool
) -> int:
    from ib_insync import IB, Forex, MarketOrder

    ib = IB()
    # readonly=False: this session must be able to place an order on --apply.
    await ib.connectAsync(host, port, clientId=client_id, readonly=False, timeout=20)
    try:
        accounts = ib.managedAccounts()
        account_id = accounts[0] if accounts else ""
        # PAPER guard — never touch a live (U*) account.
        if not account_id.startswith("DU"):
            raise FxRefusedError(
                f"refusing: account {account_id!r} is not a paper (DU*) account"
            )

        summary = await asyncio.wait_for(ib.accountSummaryAsync(), 20)
        usd_before = _usd_cash(summary)
        sgd_before = _cash(summary, "SGD")

        contract = Forex("USDSGD")
        await ib.qualifyContractsAsync(contract)
        ticker = ib.reqMktData(contract, "", False, False)
        await asyncio.sleep(3)
        rate = ticker.marketPrice()
        if not rate or math.isnan(rate):  # NaN guard
            rate = (ticker.ask + ticker.bid) / 2 if ticker.ask and ticker.bid else 0.0

        print(f"Account:        {account_id} (PAPER)")
        print(f"USD cash now:   {usd_before:,.2f}")
        print(f"SGD cash now:   {sgd_before:,.2f}")
        print(f"USD.SGD rate:   {rate:.5f}" if rate else "USD.SGD rate:   (unavailable)")
        approx_sgd = usd_amount * rate if rate else float("nan")
        print(
            f"\nProposed: BUY {usd_amount:,.0f} USD.SGD "
            f"(spend ~{approx_sgd:,.0f} SGD) on IDEALPRO."
        )

        if not apply:
            print("\nDry-run only. Re-run with --apply to place the FX order.")
            return 0

        if not sys.stdin.isatty():
            raise FxRefusedError("--apply requires an interactive TTY")
        answer = input(f"\nType {usd_amount:.0f} to place this FX order: ").strip()
        if answer != f"{usd_amount:.0f}":
            raise FxRefusedError(
                f"exact confirmation required: expected '{usd_amount:.0f}', got {answer!r}"
            )

        order = MarketOrder("BUY", usd_amount)
        trade = ib.placeOrder(contract, order)
        # Wait for terminal state (Filled / Cancelled) up to ~30s.
        for _ in range(60):
            await asyncio.sleep(0.5)
            if trade.isDone():
                break
        status = trade.orderStatus.status
        filled = trade.orderStatus.filled
        avg = trade.orderStatus.avgFillPrice
        print(f"\nOrder status: {status}; filled {filled:,.0f} USD @ {avg}")
        if status != "Filled":
            print("WARNING: order not fully filled — inspect in the Gateway/TWS.")
            return 1

        summary_after = await asyncio.wait_for(ib.accountSummaryAsync(), 20)
        print(f"USD cash after: {_usd_cash(summary_after):,.2f}")
        print(f"SGD cash after: {_cash(summary_after, 'SGD'):,.2f}")
        return 0
    finally:
        if ib.isConnected():
            ib.disconnect()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Convert SGD->USD cash on the paper IB account (Path A funding)."
    )
    parser.add_argument("--usd-amount", type=float, default=100000.0,
                        help="USD to acquire (default: 100000).")
    parser.add_argument("--ib-host", default="127.0.0.1")
    parser.add_argument("--ib-port", type=int, default=7497)
    parser.add_argument("--ib-client-id", type=int, default=95)
    parser.add_argument("--apply", action="store_true",
                        help="Place the FX order (default: dry-run report only).")
    args = parser.parse_args(argv)
    return asyncio.run(convert(
        host=args.ib_host, port=args.ib_port, client_id=args.ib_client_id,
        usd_amount=args.usd_amount, apply=args.apply,
    ))


if __name__ == "__main__":
    raise SystemExit(main())
