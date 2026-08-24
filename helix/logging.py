"""Structured JSON logging with an incident-scoped correlation id.

Configure once at process start via configure_logging(). Use
bind_correlation_id()/clear_correlation_id() to attach an incident id to
every log line emitted within that scope.
"""

from contextvars import ContextVar

import structlog

_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)


def _add_correlation_id(logger, method_name, event_dict):
    correlation_id = _correlation_id.get()
    if correlation_id is not None:
        event_dict["correlation_id"] = correlation_id
    return event_dict


def configure_logging() -> None:
    """Configure structlog for JSON output. Safe to call multiple times."""
    structlog.configure(
        processors=[
            _add_correlation_id,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(20),  # INFO
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def bind_correlation_id(correlation_id: str) -> None:
    """Attach an incident-scoped correlation id to all subsequent log lines
    emitted in this context."""
    _correlation_id.set(correlation_id)


def clear_correlation_id() -> None:
    """Remove the correlation id from this context."""
    _correlation_id.set(None)


def get_logger(*args, **kwargs):
    return structlog.get_logger(*args, **kwargs)
