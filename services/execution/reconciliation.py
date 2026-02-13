from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from shared.logging import get_logger

logger = get_logger("reconciliation")


@dataclass
class ReconciliationResult:
    """Result of reconciling IB positions against DB positions."""

    matched: list[dict[str, Any]]
    discrepancies: list[dict[str, Any]]
    severity: Literal["ok", "minor", "major"]


class PositionReconciler:
    """Reconciles Interactive Brokers positions against the database.

    Classification rules:
    - Matching positions: severity "ok"
    - Quantity off by < 5%: "minor" — auto-correct DB
    - Position missing on either side, or quantity off by >= 5%: "major" — alert + halt
    """

    MINOR_THRESHOLD_PCT: float = 5.0

    def reconcile(
        self,
        ib_positions: dict[str, int],
        db_positions: dict[str, int],
    ) -> ReconciliationResult:
        """Reconcile IB positions against DB positions.

        Args:
            ib_positions: Map of ticker -> quantity from Interactive Brokers.
            db_positions: Map of ticker -> quantity from the database.

        Returns:
            ReconciliationResult with matched, discrepancies, and severity.
        """
        matched: list[dict[str, Any]] = []
        discrepancies: list[dict[str, Any]] = []
        has_major = False
        has_minor = False

        all_tickers = set(ib_positions.keys()) | set(db_positions.keys())

        for ticker in sorted(all_tickers):
            ib_qty = ib_positions.get(ticker)
            db_qty = db_positions.get(ticker)

            # Position missing in IB
            if ib_qty is None:
                discrepancies.append(
                    {
                        "ticker": ticker,
                        "type": "missing_in_ib",
                        "ib_quantity": None,
                        "db_quantity": db_qty,
                        "auto_correct": False,
                    }
                )
                has_major = True
                logger.warning(
                    "Position missing in IB",
                    ticker=ticker,
                    db_quantity=db_qty,
                )
                continue

            # Position missing in DB
            if db_qty is None:
                discrepancies.append(
                    {
                        "ticker": ticker,
                        "type": "missing_in_db",
                        "ib_quantity": ib_qty,
                        "db_quantity": None,
                        "auto_correct": False,
                    }
                )
                has_major = True
                logger.warning(
                    "Position missing in DB",
                    ticker=ticker,
                    ib_quantity=ib_qty,
                )
                continue

            # Both exist — check quantity
            if ib_qty == db_qty:
                matched.append(
                    {
                        "ticker": ticker,
                        "quantity": ib_qty,
                    }
                )
                continue

            # Calculate percentage difference relative to IB (source of truth)
            diff_pct = abs(ib_qty - db_qty) / max(ib_qty, 1) * 100.0

            if diff_pct < self.MINOR_THRESHOLD_PCT:
                discrepancies.append(
                    {
                        "ticker": ticker,
                        "type": "quantity_mismatch",
                        "ib_quantity": ib_qty,
                        "db_quantity": db_qty,
                        "diff_pct": diff_pct,
                        "auto_correct": True,
                    }
                )
                has_minor = True
                logger.info(
                    "Minor quantity mismatch — auto-correcting",
                    ticker=ticker,
                    ib_quantity=ib_qty,
                    db_quantity=db_qty,
                    diff_pct=diff_pct,
                )
            else:
                discrepancies.append(
                    {
                        "ticker": ticker,
                        "type": "quantity_mismatch",
                        "ib_quantity": ib_qty,
                        "db_quantity": db_qty,
                        "diff_pct": diff_pct,
                        "auto_correct": False,
                    }
                )
                has_major = True
                logger.warning(
                    "Major quantity mismatch",
                    ticker=ticker,
                    ib_quantity=ib_qty,
                    db_quantity=db_qty,
                    diff_pct=diff_pct,
                )

        # Determine overall severity
        if has_major:
            severity: Literal["ok", "minor", "major"] = "major"
        elif has_minor:
            severity = "minor"
        else:
            severity = "ok"

        return ReconciliationResult(
            matched=matched,
            discrepancies=discrepancies,
            severity=severity,
        )
