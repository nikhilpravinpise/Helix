"""Kafka producer and consumer wrappers for canonical events (E2).

The transport, and nothing else: this module moves `EngineeringEvent`
envelopes onto a topic and back off it. It does not know what an incident
is, and nothing in Phase 1 is rewired to use it yet - that is E3 and E4.

## Topics

Declared by `scripts/kafka-up.sh`, not auto-created, so partition count
and retention are design rather than default.

| Topic | Partitions | Retention |
|---|---|---|
| `helix.events` | 3 | 7 days |
| `helix.events.dlq` | 1 | 30 days |

## Partitioning

The key is `correlation_id`, falling back to `service` when the event has
none. Kafka guarantees ordering within a partition only, so keying on the
incident is what makes "the events for this incident arrive in order"
true while still allowing a consumer group to scale past one member. The
fallback matters: keying `None` would round-robin an incident's
pre-correlation events across partitions and lose their order relative to
each other.

The DLQ retains longer than the events topic on purpose. A dead letter is
evidence, and it is worth keeping past the point where the event that
caused it has aged out of the main topic.

## Delivery

At-least-once, in both directions:

- The producer waits for all in-sync replicas (`acks=all`) and enables
  idempotence, so a retry inside the client cannot silently duplicate a
  record.
- The consumer disables auto-commit and commits only after the handler
  returns. A crash between handling and committing redelivers the event,
  which is the "at least" half - handlers must therefore be idempotent.
  Committing before handling would make it at-most-once and lose events
  on a crash, which is the wrong trade for failure signals.

## Poison messages

A record whose bytes are not a valid `EngineeringEvent` is produced to the
DLQ with the failure recorded in its headers, and then committed. It can
never succeed on redelivery, so blocking the partition on it would stall
every well-formed event behind it.

A handler that raises is treated differently: the offset is not committed
and the exception propagates. That event is well-formed and was simply
not processed, so discarding it would lose a real failure. Bounding the
retries is E4's problem, where a real consumer service exists to own the
budget.
"""

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from confluent_kafka import OFFSET_BEGINNING, Consumer, KafkaError, Producer
from pydantic import ValidationError

from events.schemas import EngineeringEvent
from helix.config import get_settings

EVENTS_TOPIC = "helix.events"
DLQ_TOPIC = "helix.events.dlq"


def partition_key(event: EngineeringEvent) -> str:
    """The key every event is produced under. See Partitioning above."""
    return event.correlation_id or event.service


class EventPublishError(RuntimeError):
    """A record was not acknowledged by the broker. Raised rather than
    logged: the caller still holds the event and is the only one able to
    decide whether to retry or fail the operation that produced it."""


@dataclass
class DeadLetter:
    """One poison record, as read back off the DLQ."""

    payload: bytes
    error: str
    source_topic: str
    source_partition: int
    source_offset: int

    @property
    def text(self) -> str:
        return self.payload.decode("utf-8", errors="replace")


class EventProducer:
    """Publishes canonical events to `helix.events`.

    Not thread-safe by intent - `confluent_kafka.Producer` is, but the
    delivery-error capture below is not, and a shared producer across
    threads would attribute failures to the wrong caller.
    """

    def __init__(self, bootstrap_servers: str | None = None, topic: str = EVENTS_TOPIC) -> None:
        self.topic = topic
        self._errors: list[str] = []
        self._producer = Producer(
            {
                "bootstrap.servers": bootstrap_servers or get_settings().kafka_bootstrap_servers,
                # Durability over throughput: a failure signal that the
                # broker never persisted is a lost incident.
                "acks": "all",
                "enable.idempotence": True,
                "retries": 5,
                "linger.ms": 5,
            }
        )

    def _on_delivery(self, err: KafkaError | None, _msg: Any) -> None:
        if err is not None:
            self._errors.append(str(err))

    def publish(self, event: EngineeringEvent) -> None:
        """Queue `event`. Delivery is asynchronous - call `flush()` before
        treating it as durable."""
        self._producer.produce(
            self.topic,
            key=partition_key(event).encode(),
            value=event.model_dump_json().encode(),
            on_delivery=self._on_delivery,
        )
        # Serve delivery callbacks already waiting, so errors surface at
        # the next flush rather than accumulating unseen.
        self._producer.poll(0)

    def publish_many(self, events: list[EngineeringEvent]) -> None:
        for event in events:
            self.publish(event)

    def flush(self, timeout: float = 10.0) -> None:
        """Block until every queued record is acknowledged.

        Raises if any record failed, or if the queue did not drain inside
        `timeout` - an un-drained queue means those events are not durable,
        and returning normally would say otherwise.
        """
        remaining = self._producer.flush(timeout)
        if remaining:
            raise EventPublishError(f"{remaining} record(s) unacknowledged after {timeout}s")
        if self._errors:
            errors, self._errors = self._errors, []
            raise EventPublishError("; ".join(errors))


class EventConsumer:
    """Consumes canonical events, routing undeserializable ones to the DLQ.

    `group_id` is the consumer group: two consumers sharing one keep
    distinct partitions, two with different ids each see every event.
    """

    def __init__(
        self,
        group_id: str,
        bootstrap_servers: str | None = None,
        topic: str = EVENTS_TOPIC,
        dlq_topic: str = DLQ_TOPIC,
        from_beginning: bool = False,
    ) -> None:
        servers = bootstrap_servers or get_settings().kafka_bootstrap_servers
        self.topic = topic
        self.dlq_topic = dlq_topic
        self._dlq = Producer({"bootstrap.servers": servers, "acks": "all"})
        self._consumer = Consumer(
            {
                "bootstrap.servers": servers,
                "group.id": group_id,
                # Commit only after the handler returns - see Delivery.
                "enable.auto.commit": False,
                # A brand new group reads the whole retained log rather
                # than only what arrives after it subscribes; an existing
                # group always resumes from its committed offsets.
                "auto.offset.reset": "earliest" if from_beginning else "latest",
            }
        )
        self._consumer.subscribe([topic])

    def _to_dlq(self, msg: Any, error: str) -> None:
        """Publish a poison record verbatim, with the reason in headers.

        The original bytes are kept exactly as received - a dead letter is
        for investigation, and re-encoding it would destroy the evidence
        of what actually arrived.
        """
        self._dlq.produce(
            self.dlq_topic,
            key=msg.key(),
            value=msg.value(),
            headers=[
                ("error", error.encode()),
                ("source_topic", str(msg.topic()).encode()),
                ("source_partition", str(msg.partition()).encode()),
                ("source_offset", str(msg.offset()).encode()),
            ],
        )
        self._dlq.flush(10.0)

    def consume(
        self,
        handler: Callable[[EngineeringEvent], None],
        max_events: int | None = None,
        timeout: float = 1.0,
        idle_rounds: int | None = 3,
        join_timeout: float = 30.0,
    ) -> int:
        """Poll and dispatch until `max_events` are handled or the topic
        goes quiet for `idle_rounds` consecutive polls. Returns the number
        of events handed to `handler`.

        Bounded by default: a test harness needs to know when it has
        drained. `idle_rounds=None` is follow mode - poll until
        `max_events` or an interrupt - which is what `helix consume
        --follow` runs as the long-lived daemon E2 left to E4.

        A poll before the group has been assigned any partition does not
        count toward `idle_rounds`. "The topic went quiet" is only
        meaningful once there is something to be quiet about, and a fresh
        consumer group spends its first seconds joining and rebalancing -
        counting those as idle makes a cold consumer conclude an empty
        topic and return zero. `join_timeout` bounds that wait so a topic
        that never assigns still terminates.
        """
        handled = 0
        idle = 0
        joined_by = time.monotonic() + join_timeout
        while max_events is None or handled < max_events:
            msg = self._consumer.poll(timeout)
            if msg is None:
                if not self._consumer.assignment() and time.monotonic() < joined_by:
                    continue
                idle += 1
                if idle_rounds is not None and idle >= idle_rounds:
                    break
                continue
            idle = 0
            if msg.error():
                # Partition EOF is normal, not a failure.
                if msg.error().code() == KafkaError._PARTITION_EOF:
                    continue
                raise RuntimeError(f"consume failed: {msg.error()}")

            try:
                event = EngineeringEvent.model_validate_json(msg.value())
            except (ValidationError, ValueError, UnicodeDecodeError) as exc:
                # Poison: it will never deserialize, so park it and move
                # on rather than blocking the partition behind it.
                self._to_dlq(msg, f"{type(exc).__name__}: {exc}")
                self._consumer.commit(msg, asynchronous=False)
                continue

            # Commit only after the handler returns. An exception here
            # leaves the offset uncommitted on purpose, so the event is
            # redelivered rather than lost.
            handler(event)
            self._consumer.commit(msg, asynchronous=False)
            handled += 1
        return handled

    def close(self) -> None:
        self._consumer.close()

    def __enter__(self) -> "EventConsumer":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def read_dead_letters(
    bootstrap_servers: str | None = None,
    dlq_topic: str = DLQ_TOPIC,
    group_id: str = "helix-dlq-reader",
    timeout: float = 1.0,
    idle_rounds: int = 3,
) -> list[DeadLetter]:
    """Drain the DLQ from the beginning, without committing offsets.

    Reading dead letters is an inspection, so it must not consume them:
    a later reader has to see the same records.
    """
    servers = bootstrap_servers or get_settings().kafka_bootstrap_servers
    consumer = Consumer(
        {
            "bootstrap.servers": servers,
            "group.id": group_id,
            "enable.auto.commit": False,
            "auto.offset.reset": "earliest",
        }
    )

    def _rewind(c: Consumer, partitions: list[Any]) -> None:
        for p in partitions:
            p.offset = OFFSET_BEGINNING
        c.assign(partitions)

    consumer.subscribe([dlq_topic], on_assign=_rewind)
    out: list[DeadLetter] = []
    idle = 0
    try:
        while idle < idle_rounds:
            msg = consumer.poll(timeout)
            if msg is None:
                idle += 1
                continue
            idle = 0
            if msg.error():
                continue
            headers = {k: v for k, v in (msg.headers() or [])}
            out.append(
                DeadLetter(
                    payload=msg.value(),
                    error=_header(headers, "error"),
                    source_topic=_header(headers, "source_topic"),
                    source_partition=int(_header(headers, "source_partition") or -1),
                    source_offset=int(_header(headers, "source_offset") or -1),
                )
            )
    finally:
        consumer.close()
    return out


def _header(headers: dict[str, bytes], name: str) -> str:
    value = headers.get(name)
    return value.decode("utf-8", errors="replace") if value else ""


def publish_raw(
    payload: bytes,
    key: str,
    bootstrap_servers: str | None = None,
    topic: str = EVENTS_TOPIC,
) -> None:
    """Produce arbitrary bytes to `topic`.

    Exists so tests can inject a malformed record - the typed producer
    cannot, because it serializes a validated model. A real producer of
    unvalidated bytes would be a defect, which is why this is not a method
    on `EventProducer`.
    """
    producer = Producer(
        {"bootstrap.servers": bootstrap_servers or get_settings().kafka_bootstrap_servers}
    )
    producer.produce(topic, key=key.encode(), value=payload)
    if producer.flush(10.0):
        raise EventPublishError("raw record unacknowledged")


def topic_config(bootstrap_servers: str | None = None) -> dict[str, dict[str, Any]]:
    """Partition counts for the declared topics, read from the broker.

    Used by the tests that assert kafka-up.sh's topic design actually
    landed, rather than trusting the script's own output.
    """
    from confluent_kafka.admin import AdminClient

    admin = AdminClient(
        {"bootstrap.servers": bootstrap_servers or get_settings().kafka_bootstrap_servers}
    )
    metadata = admin.list_topics(timeout=10)
    out = {}
    for name in (EVENTS_TOPIC, DLQ_TOPIC):
        topic = metadata.topics.get(name)
        out[name] = {
            "exists": topic is not None,
            "partitions": len(topic.partitions) if topic else 0,
        }
    return out


def serialize(event: EngineeringEvent) -> bytes:
    """The wire form, exposed so tests can assert the round trip without
    standing up a broker."""
    return event.model_dump_json().encode()


def deserialize(payload: bytes) -> EngineeringEvent:
    return EngineeringEvent.model_validate_json(payload)


def json_or_none(payload: bytes) -> dict[str, Any] | None:
    """Best-effort decode for inspecting a dead letter's payload."""
    try:
        return json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        return None
