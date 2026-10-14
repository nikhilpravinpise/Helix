"""Elasticsearch log documents to canonical events (subphase E3).

Consumes a raw Elasticsearch `_search` response and emits
`service.unhealthy` for error-severity log lines. Knowledge of `hits.hits`,
`_source` and `@timestamp` stops here.

The raw response is the input rather than the formatted lines
`integrations.elk` produces, for one reason: `_id`. It is the only stable
identity a log document has, and without it there is no natural key to
derive an idempotent event id from.

## Why error logs are `service.unhealthy`

A log line is not inherently an event, so the mapping needs stating. An
error-severity line from a running service is that service reporting it
cannot do its job - which is what `service.unhealthy` means, and is the
one thing `pod.failed` and `deployment.failed` cannot say. Levels below
error are not emitted at all: a warning is not a failure, and an event per
INFO line would be a firehose with no signal in it.

## On volume

One event per error document, deliberately. A crash-looping pod emits many
error lines and this will emit many events, which looks wasteful until you
consider the alternative: collapsing them here would mean inventing a
natural key spanning a set of documents, and that key would change as the
set grew, so a redelivery would produce a different event id and break the
idempotency this subphase exists to provide. Collapsing many events into
one incident is E4's job, and it has the correlation machinery to do it
properly.
"""

from typing import Any

from events.adapters._common import vendor_event, vendor_time
from events.schemas import EngineeringEvent, EventSource, EventType, derive_event_id

#: Error-severity markers, matched case-insensitively against the message.
#: Python's logging, uvicorn and the JVM all spell at least one of these.
_ERROR_MARKERS = ("error", "critical", "fatal", "exception", "traceback")

#: Messages are evidence and belong in the event, but a stack trace can be
#: arbitrarily long; the full document stays in Elasticsearch.
_MESSAGE_LIMIT = 2000


def _is_error(message: str, level: str | None) -> bool:
    """Prefer an explicit level field; fall back to scanning the message.

    Filebeat ships the payment-api's stdout as an unparsed `message`, so in
    this sandbox there is usually no level field at all and the fallback is
    the only thing that works.
    """
    if level:
        return level.strip().lower() in {"error", "critical", "fatal"}
    lowered = message.lower()
    return any(marker in lowered for marker in _ERROR_MARKERS)


def _pod_of(source: dict[str, Any]) -> str | None:
    return ((source.get("kubernetes") or {}).get("pod") or {}).get("name")


def _service_of(source: dict[str, Any], default: str) -> str:
    kubernetes = source.get("kubernetes") or {}
    labels = kubernetes.get("labels") or {}
    container = kubernetes.get("container") or {}
    return labels.get("app") or container.get("name") or default


def log_events(
    response: dict[str, Any],
    service: str = "payment-api",
    environment: str = "sandbox",
) -> list[EngineeringEvent]:
    """Canonical events for the error-severity documents in an ES response."""
    out: list[EngineeringEvent] = []
    for hit in ((response.get("hits") or {}).get("hits")) or []:
        source = hit.get("_source") or {}
        message = str(source.get("message") or "")
        if not message or not _is_error(message, source.get("log.level") or source.get("level")):
            continue
        out.append(
            vendor_event(
                # The document id: stable, and what a re-read of the same
                # index returns unchanged.
                event_id=derive_event_id("elk", hit.get("_index"), hit.get("_id")),
                occurred=vendor_time(source.get("@timestamp")),
                event_type=EventType.SERVICE_UNHEALTHY,
                source=EventSource.ELK,
                service=_service_of(source, service),
                environment=environment,
                metadata={
                    "message": message[:_MESSAGE_LIMIT],
                    "message_truncated": len(message) > _MESSAGE_LIMIT,
                    "pod": _pod_of(source),
                    "logged_at": source.get("@timestamp"),
                    "index": hit.get("_index"),
                    "document_id": hit.get("_id"),
                },
            )
        )
    return out
