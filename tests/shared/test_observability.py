# tests/shared/test_observability.py
from __future__ import annotations

import prometheus_client

from shared.observability import create_counter, create_gauge, create_histogram


def _reset_registry() -> None:
    """Clear the default Prometheus collector registry between tests."""
    collectors = list(prometheus_client.REGISTRY._names_to_collectors.values())
    for collector in collectors:
        try:
            prometheus_client.REGISTRY.unregister(collector)
        except Exception:
            pass


class TestCounter:
    def setup_method(self) -> None:
        _reset_registry()

    def test_counter_increments(self) -> None:
        counter = create_counter(
            "test_messages_total", "Total test messages processed"
        )
        assert counter._value.get() == 0.0

        counter.inc()
        assert counter._value.get() == 1.0

        counter.inc(5)
        assert counter._value.get() == 6.0


class TestHistogram:
    def setup_method(self) -> None:
        _reset_registry()

    def test_histogram_observes(self) -> None:
        histogram = create_histogram(
            "test_request_duration_seconds", "Test request duration"
        )

        histogram.observe(0.5)
        histogram.observe(1.2)
        histogram.observe(0.3)

        # The sum of all observed values should equal 2.0
        assert histogram._sum.get() == 2.0


class TestGauge:
    def setup_method(self) -> None:
        _reset_registry()

    def test_gauge_sets_value(self) -> None:
        gauge = create_gauge(
            "test_active_connections", "Test active connections"
        )
        assert gauge._value.get() == 0.0

        gauge.set(42)
        assert gauge._value.get() == 42.0

        gauge.inc()
        assert gauge._value.get() == 43.0

        gauge.dec(3)
        assert gauge._value.get() == 40.0
