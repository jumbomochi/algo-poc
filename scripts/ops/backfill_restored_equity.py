#!/usr/bin/env python3
"""Repair the equity SERIES after a restored exit.

KAN-85, slice two. ``restore_missed_exit.py`` fixes the ledger — trade,
realised P&L, cash, intent — and cannot fix ``equity_snapshots``, which is a
*recorded* series: one row written per day from the cash and market value as
they stood that morning (``paper_state.py::record_equity_snapshot`` takes all
three as arguments). Correcting today's cash does not reach back into it.

That gap is visible exactly where it matters. The divergence monitor reads
``equity_snapshots`` (divergence_monitor.py:410), so after both 2026-09-16
restores ``sector_rotation`` still measured **-34.0%** against its shadow, and
the next morning's snapshot would have jumped **+5,524.99** in one day with no
trade dated to it — the same "step change with no trade" signature the
write-offs produced, only upward.

WHY THIS IS RECOMPUTATION, NOT FABRICATION
------------------------------------------
The rows carry ``cash`` and ``market_value`` separately, and across the whole
affected span ``sector_rotation``'s cash is frozen at 8,746.46::

    2026-08-28  equity 16,114.83  cash 8,746.46  mkt 7,368.37
    2026-08-29  equity 12,637.84  cash 8,746.46  mkt 3,891.38   <- PANW
    2026-09-12  equity 12,617.06  cash 8,746.46  mkt 3,870.60
    2026-09-15  equity 10,374.54  cash 8,746.46  mkt 1,628.08   <- LLY

Every drop came through ``market_value``, because ``_apply_action`` never
touched cash. So the corrected equity is just ``cash + market_value`` with the
proceeds applied from the date the book learned of each sale. ``market_value``
is never modified and no figure is invented.

WHY THE PROCEEDS LAND ON THE WRITE-OFF DATE, NOT THE SALE DATE
--------------------------------------------------------------
Economically the cash arrived when IB sold. But between the sale and the
write-off the book still carried the position at a mark, which is a reasonable
stand-in for the proceeds, and moving the cash earlier would ALSO require
removing that position's mark from every intervening row — and per-position
daily marks were never stored. Applying the proceeds where the mark left is the
only choice the recorded data supports.

That leaves a small residual step on each date (-289.45 for PANW, +94.93 for
LLY) and those are **real**: the difference between the stale mark the book was
carrying and the price it actually sold at. They belong in the series.

THE INVARIANT THAT MAKES THIS SAFE
-----------------------------------
The adjustments must reconcile exactly to ``portfolio_config.cash`` as it now
stands. If they do not, a date or an amount is mis-stated and the backfill is
refused. That check is what stops this being a way to paint any series you
like, and it is why a second run is refused too — the series already
reconciles, so re-applying would double the proceeds.

Every applied backfill writes a JSON artifact carrying both sides of every
changed row. Rewriting gate evidence silently has the same problem as
reconstructing a trade silently.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from shared.config import load_config  # noqa: E402
from shared.models.equity_snapshot import EquitySnapshot  # noqa: E402
from shared.models.portfolio_config import PortfolioConfig  # noqa: E402

CONFIRMATION = "BACKFILL RESTORED EQUITY"

#: Cash reconciles to the cent, so anything looser would let a real mistake
#: through as a rounding difference.
CASH_TOLERANCE = 0.005

DEFAULT_ARTIFACT_DIR = _REPO_ROOT / "output" / "reconciliation"


class BackfillRefusedError(RuntimeError):
    """The backfill cannot be made safely."""


@dataclass(frozen=True)
class Adjustment:
    """Cash the book should have held from ``effective_from`` onward."""

    effective_from: date
    amount: float
    note: str = ""


@dataclass(frozen=True)
class BackfillRow:
    date: date
    market_value: float
    old_cash: float
    old_equity: float
    new_cash: float
    new_equity: float

    @property
    def changed(self) -> bool:
        return abs(self.new_equity - self.old_equity) > CASH_TOLERANCE


@dataclass(frozen=True)
class BackfillPlan:
    portfolio: str
    adjustments: tuple[Adjustment, ...]
    rows: tuple[BackfillRow, ...] = field(default=())
    final_cash: float = 0.0
    config_cash: float = 0.0

    @property
    def reconciles(self) -> bool:
        return abs(self.final_cash - self.config_cash) <= CASH_TOLERANCE

    @property
    def changed_rows(self) -> tuple[BackfillRow, ...]:
        return tuple(row for row in self.rows if row.changed)


def plan_backfill(
    session: Session,
    *,
    portfolio: str,
    adjustments: list[Adjustment] | tuple[Adjustment, ...],
) -> BackfillPlan:
    """Recompute the series. Mutates nothing."""
    if not adjustments:
        raise BackfillRefusedError(
            "no adjustments supplied; there is nothing to backfill"
        )

    config = session.scalar(
        select(PortfolioConfig).where(PortfolioConfig.portfolio == portfolio)
    )
    if config is None:
        raise BackfillRefusedError(f"unknown portfolio {portfolio!r}")

    snapshots = session.scalars(
        select(EquitySnapshot)
        .where(EquitySnapshot.portfolio == portfolio)
        .order_by(EquitySnapshot.date)
    ).all()
    if not snapshots:
        raise BackfillRefusedError(f"no equity snapshots for {portfolio!r}")

    ordered = tuple(sorted(adjustments, key=lambda a: a.effective_from))
    rows: list[BackfillRow] = []
    for snap in snapshots:
        applied = sum(
            adj.amount for adj in ordered if snap.date >= adj.effective_from
        )
        new_cash = float(snap.cash) + applied
        rows.append(BackfillRow(
            date=snap.date,
            market_value=float(snap.market_value),
            old_cash=float(snap.cash),
            old_equity=float(snap.equity),
            new_cash=new_cash,
            new_equity=new_cash + float(snap.market_value),
        ))

    final_cash = rows[-1].new_cash
    plan = BackfillPlan(
        portfolio=portfolio,
        adjustments=ordered,
        rows=tuple(rows),
        final_cash=final_cash,
        config_cash=float(config.cash),
    )
    if not plan.reconciles:
        raise BackfillRefusedError(
            f"the adjustments do not reconcile: they bring the last snapshot's "
            f"cash to {final_cash:,.2f}, but portfolio_config.cash is "
            f"{float(config.cash):,.2f}. A date or an amount is mis-stated, or "
            f"this series has already been backfilled -- either way nothing is "
            f"written. The corrected series must end where the ledger actually "
            f"stands."
        )
    return plan


def apply_backfill(
    session: Session,
    plan: BackfillPlan,
    *,
    confirm: str,
    artifact_dir: Path | str = DEFAULT_ARTIFACT_DIR,
) -> Path:
    """Rewrite the series and record both sides of every changed row."""
    if confirm != CONFIRMATION:
        raise BackfillRefusedError(
            f"exact confirmation required: expected {CONFIRMATION!r}"
        )

    by_date = {row.date: row for row in plan.rows}
    snapshots = session.scalars(
        select(EquitySnapshot)
        .where(EquitySnapshot.portfolio == plan.portfolio)
        .order_by(EquitySnapshot.date)
    ).all()
    for snap in snapshots:
        row = by_date.get(snap.date)
        if row is None:
            continue
        snap.cash = row.new_cash
        snap.equity = row.new_equity
    session.commit()

    # The audit trail. Written after the commit so it can never claim a change
    # that did not land, and carrying both sides so the rewrite is reviewable
    # long after the reasoning has left anyone's head.
    directory = Path(artifact_dir)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = directory / f"equity-backfill-{stamp}.json"
    path.write_text(json.dumps({
        "portfolio": plan.portfolio,
        "written_at": datetime.now(timezone.utc).isoformat(),
        "reason": (
            "equity_snapshots is a recorded series and does not follow a "
            "restored exit; cash never received the proceeds because "
            "reconcile_paper.py::_apply_action does not touch it. Recomputed "
            "as cash + market_value with the proceeds applied from the date "
            "the write-off left market_value. market_value is unmodified."
        ),
        "final_cash": plan.final_cash,
        "portfolio_config_cash": plan.config_cash,
        "adjustments": [
            {
                "effective_from": adj.effective_from.isoformat(),
                "amount": adj.amount,
                "note": adj.note,
            }
            for adj in plan.adjustments
        ],
        "rows": [
            {
                "date": row.date.isoformat(),
                "market_value": row.market_value,
                "old_cash": row.old_cash,
                "new_cash": row.new_cash,
                "old_equity": row.old_equity,
                "new_equity": row.new_equity,
                "changed": row.changed,
            }
            for row in plan.rows
        ],
    }, indent=2) + "\n")
    return path


def _parse_adjustment(raw: str) -> Adjustment:
    parts = raw.split(":", 2)
    if len(parts) < 2:
        raise argparse.ArgumentTypeError(
            f"expected DATE:AMOUNT[:NOTE], got {raw!r}"
        )
    return Adjustment(
        effective_from=date.fromisoformat(parts[0]),
        amount=float(parts[1]),
        note=parts[2] if len(parts) > 2 else "",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Backfill equity_snapshots after a restored exit."
    )
    parser.add_argument("--portfolio", required=True)
    parser.add_argument(
        "--adjustment", action="append", type=_parse_adjustment, default=[],
        metavar="DATE:AMOUNT[:NOTE]",
        help="cash the book should have held from DATE onward, repeatable; "
             "must reconcile to portfolio_config.cash or the run is refused",
    )
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--artifact-dir", default=str(DEFAULT_ARTIFACT_DIR))
    parser.add_argument(
        "--apply", action="store_true",
        help="Rewrite the rows (default: dry-run diff only).",
    )
    args = parser.parse_args(argv)

    url = args.database_url or load_config("config/default.yaml").database.url
    engine = create_engine(url)
    with Session(engine) as session:
        plan = plan_backfill(
            session, portfolio=args.portfolio, adjustments=args.adjustment
        )
        print(f"  {plan.portfolio}: {len(plan.changed_rows)} of "
              f"{len(plan.rows)} rows change")
        for adj in plan.adjustments:
            print(f"    from {adj.effective_from}  {adj.amount:+,.2f}  {adj.note}")
        print()
        print(f"    {'date':12}{'old equity':>13}{'new equity':>13}{'step':>11}")
        previous: float | None = None
        for row in plan.rows:
            step = "" if previous is None else f"{row.new_equity - previous:+,.2f}"
            print(f"    {row.date.isoformat():12}{row.old_equity:>13,.2f}"
                  f"{row.new_equity:>13,.2f}{step:>11}")
            previous = row.new_equity
        print()
        print(f"  reconciles to portfolio_config.cash {plan.config_cash:,.2f}")
        if not args.apply:
            print("\nDry-run only. Re-run with --apply to rewrite the rows.")
            return 0
        if not sys.stdin.isatty():
            raise BackfillRefusedError("--apply requires an interactive TTY")
        answer = input(f"\nType {CONFIRMATION} to rewrite these rows: ")
        path = apply_backfill(
            session, plan, confirm=answer.strip(), artifact_dir=args.artifact_dir
        )
        print(f"Rewritten. Audit artifact: {path}")
        return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
