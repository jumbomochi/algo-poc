from __future__ import annotations

import time

from prometheus_client.registry import CollectorRegistry

from shared.heartbeat import (
    HeartbeatAgeCollector,
    heartbeat_age_seconds,
    register_heartbeat_collector,
    write_heartbeat,
)


def test_write_heartbeat_creates_file(tmp_path):
    path = tmp_path / "heartbeat"
    write_heartbeat(path)
    assert path.exists()


def test_write_heartbeat_creates_missing_parent_dirs(tmp_path):
    path = tmp_path / "nested" / "dir" / "heartbeat"
    write_heartbeat(path)
    assert path.exists()


def test_heartbeat_age_seconds_is_near_zero_right_after_write(tmp_path):
    path = tmp_path / "heartbeat"
    write_heartbeat(path)
    assert 0.0 <= heartbeat_age_seconds(path) < 5.0


def test_heartbeat_age_seconds_reflects_elapsed_time(tmp_path, monkeypatch):
    path = tmp_path / "heartbeat"
    write_heartbeat(path)

    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 120)
    assert heartbeat_age_seconds(path) >= 120.0


def test_heartbeat_age_seconds_is_infinite_when_missing(tmp_path):
    path = tmp_path / "never_written"
    assert heartbeat_age_seconds(path) == float("inf")


def test_write_heartbeat_can_be_called_repeatedly(tmp_path):
    path = tmp_path / "heartbeat"
    for _ in range(3):
        write_heartbeat(path)
    assert path.exists()
    assert heartbeat_age_seconds(path) < 5.0


class TestHeartbeatAgeCollector:
    """CRITICAL fix: the metrics HTTP server runs on its own background
    thread (prometheus_client.start_http_server), independent of a service's
    asyncio main loop. If that main loop wedges on blocking I/O (the known
    IB stuck-modal class), the metrics thread keeps answering scrapes
    (up==1) — ServiceMetricsEndpointDown never fires. This collector's
    collect() re-reads the heartbeat file's mtime fresh on every scrape, so
    the reported age keeps climbing even while the loop that's supposed to
    refresh it is stuck — that's what a HeartbeatStale alert can catch.
    """

    def _collect_value(self, collector: HeartbeatAgeCollector) -> float:
        family = next(iter(collector.collect()))
        sample = next(iter(family.samples))
        return sample.value

    def test_collector_reports_near_zero_age_right_after_a_write(self, tmp_path):
        path = tmp_path / "heartbeat"
        write_heartbeat(path)
        collector = HeartbeatAgeCollector(path)
        assert self._collect_value(collector) < 5.0

    def test_collector_reports_growing_age_when_file_stops_being_touched(
        self, tmp_path, monkeypatch
    ):
        path = tmp_path / "heartbeat"
        write_heartbeat(path)
        collector = HeartbeatAgeCollector(path)

        real_time = time.time
        monkeypatch.setattr(time, "time", lambda: real_time() + 600)
        # No write_heartbeat() call in between — simulates a wedged main
        # loop. The collector must still reflect that staleness, because it
        # recomputes from the file's mtime on every collect(), not from
        # something the (stuck) main loop last reported.
        assert self._collect_value(collector) >= 600.0

    def test_collector_reports_infinite_age_when_never_written(self, tmp_path):
        path = tmp_path / "never_written"
        collector = HeartbeatAgeCollector(path)
        assert self._collect_value(collector) == float("inf")

    def test_metric_family_name_is_algo_heartbeat_age_seconds(self, tmp_path):
        path = tmp_path / "heartbeat"
        write_heartbeat(path)
        collector = HeartbeatAgeCollector(path)
        family = next(iter(collector.collect()))
        assert family.name == "algo_heartbeat_age_seconds"


class TestRegisterHeartbeatCollector:
    def test_register_heartbeat_collector_exposes_the_gauge(self, tmp_path):
        from prometheus_client import generate_latest

        path = tmp_path / "heartbeat"
        write_heartbeat(path)
        registry = CollectorRegistry()

        register_heartbeat_collector(path, registry=registry)

        output = generate_latest(registry).decode()
        assert "algo_heartbeat_age_seconds" in output

    def test_register_heartbeat_collector_is_idempotent(self, tmp_path):
        path = tmp_path / "heartbeat"
        write_heartbeat(path)
        registry = CollectorRegistry()

        # Must not raise, and must not silently double-register (which
        # would emit the same metric name twice in one scrape — invalid
        # exposition format that would make Prometheus reject the scrape).
        register_heartbeat_collector(path, registry=registry)
        register_heartbeat_collector(path, registry=registry)

        from prometheus_client import generate_latest

        output = generate_latest(registry).decode()
        # A single registration's exposition block mentions the metric name
        # exactly 3 times: "# HELP ...", "# TYPE ...", and the sample line
        # itself. A silent double-registration would double every count.
        assert output.count("algo_heartbeat_age_seconds") == 3
