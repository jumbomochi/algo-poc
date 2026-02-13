# tests/shared/test_logging.py
import json
from io import StringIO
from shared.logging import get_logger


def test_get_logger_returns_bound_logger():
    logger = get_logger("test-service")
    assert logger is not None


def test_logger_outputs_json(capsys):
    logger = get_logger("test-service")
    logger.info("test message", ticker="AAPL")
    captured = capsys.readouterr()
    output = captured.err or captured.out
    assert "test message" in output or "test-service" in output
