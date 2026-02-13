from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from services.execution.order_manager import OrderManager, PartialFillDecision


class TestPartialFills:
    def _make_manager(self) -> OrderManager:
        return OrderManager(
            executor=AsyncMock(),
            redis_client=AsyncMock(),
            db_session=MagicMock(),
        )

    def test_sixty_percent_filled_accepted(self):
        """60% fill is above the 40% minimum -> accept."""
        mgr = self._make_manager()
        decision = mgr.handle_partial_fill(
            order_id="order-001",
            filled_quantity=60,
            total_quantity=100,
            min_viable_fill_pct=40.0,
        )
        assert decision.action == "accept"
        assert decision.filled_pct == pytest.approx(60.0)
        assert "accept" in decision.message.lower() or "undersized" in decision.message.lower()

    def test_thirty_percent_filled_flagged(self):
        """30% fill is below the 40% minimum -> flag for review."""
        mgr = self._make_manager()
        decision = mgr.handle_partial_fill(
            order_id="order-001",
            filled_quantity=30,
            total_quantity=100,
            min_viable_fill_pct=40.0,
        )
        assert decision.action == "flag_for_review"
        assert decision.filled_pct == pytest.approx(30.0)
        assert "review" in decision.message.lower()

    def test_hundred_percent_filled_accepted(self):
        """100% fill -> accept (fully filled)."""
        mgr = self._make_manager()
        decision = mgr.handle_partial_fill(
            order_id="order-001",
            filled_quantity=100,
            total_quantity=100,
            min_viable_fill_pct=40.0,
        )
        assert decision.action == "accept"
        assert decision.filled_pct == pytest.approx(100.0)

    def test_exactly_at_threshold_accepted(self):
        """Exactly at min viable fill pct -> accept."""
        mgr = self._make_manager()
        decision = mgr.handle_partial_fill(
            order_id="order-001",
            filled_quantity=40,
            total_quantity=100,
            min_viable_fill_pct=40.0,
        )
        assert decision.action == "accept"
        assert decision.filled_pct == pytest.approx(40.0)

    def test_partial_fill_decision_dataclass(self):
        """PartialFillDecision should be a proper dataclass."""
        decision = PartialFillDecision(
            action="accept",
            filled_pct=75.0,
            message="Accepted as undersized position",
        )
        assert decision.action == "accept"
        assert decision.filled_pct == 75.0
        assert decision.message == "Accepted as undersized position"

    def test_just_below_threshold_flagged(self):
        """39.9% fill is just below 40% minimum -> flag for review."""
        mgr = self._make_manager()
        decision = mgr.handle_partial_fill(
            order_id="order-001",
            filled_quantity=399,
            total_quantity=1000,
            min_viable_fill_pct=40.0,
        )
        assert decision.action == "flag_for_review"
        assert decision.filled_pct == pytest.approx(39.9)
