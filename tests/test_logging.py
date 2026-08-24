"""The correlation id bound for an incident must appear on every log line
emitted in that context, and must not leak once cleared."""

import json

import structlog

from helix.logging import bind_correlation_id, clear_correlation_id, configure_logging


def test_correlation_id_appears_on_log_lines(capsys):
    configure_logging()
    bind_correlation_id("incident-123")
    try:
        structlog.get_logger().info("evidence collected")
    finally:
        clear_correlation_id()

    captured = json.loads(capsys.readouterr().out)
    assert captured["correlation_id"] == "incident-123"
    assert captured["event"] == "evidence collected"


def test_correlation_id_absent_after_clear(capsys):
    configure_logging()
    bind_correlation_id("incident-456")
    clear_correlation_id()
    structlog.get_logger().info("no incident scope")

    captured = json.loads(capsys.readouterr().out)
    assert "correlation_id" not in captured
