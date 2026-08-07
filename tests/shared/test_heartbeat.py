from __future__ import annotations

import time

from shared.heartbeat import heartbeat_age_seconds, write_heartbeat


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
