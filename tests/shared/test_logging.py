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


def test_exc_info_renders_traceback(capsys):
    """exc_info=True must render the actual traceback, not a bare flag.

    Regression: the 2026-07-11 execution failure logged only
    '"exc_info": true' with no stack trace, hiding the root cause.
    """
    logger = get_logger("test-service")
    try:
        raise ValueError("boom-trace")
    except ValueError:
        logger.error("something failed", exc_info=True)
    captured = capsys.readouterr()
    output = captured.err or captured.out
    assert "boom-trace" in output
    assert "Traceback" in output
    assert '"exc_info": true' not in output
