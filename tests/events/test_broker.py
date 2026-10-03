"""Kafka producer/consumer wrappers (subphase E2).

Unit tier covers the pure parts - keying and the wire format. The
integration tier is the subphase's literal exit criterion and needs a real
broker (`bash scripts/kafka-up.sh`): events produced are consumed
reliably, a broker restart loses nothing, and a malformed record lands in
the dead letter topic instead of stalling the consumer.

Every integration test gets its own pair of topics. Sharing `helix.events`
across tests would make each one's result depend on what the previous test
left behind, and on a topic with seven-day retention that is a debugging
problem nobody should inherit.
"""

import json
import subprocess
import uuid

import pytest

from events.broker import (
    DeadLetter,
    EventConsumer,
    EventProducer,
    deserialize,
    json_or_none,
    partition_key,
    publish_raw,
    read_dead_letters,
    serialize,
    topic_config,
)
from events.schemas import EngineeringEvent, EventSource, EventType

BOOTSTRAP = "127.0.0.1:9092"


def make_event(**overrides) -> EngineeringEvent:
    base = {
        "event_type": EventType.POD_FAILED,
        "source": EventSource.KUBERNETES,
        "service": "payment-api",
        "environment": "sandbox",
    }
    return EngineeringEvent(**{**base, **overrides})


class TestPartitionKey:
    """Ordering within an incident depends entirely on this."""

    def test_correlation_id_is_the_key(self):
        assert partition_key(make_event(correlation_id="INC-1042")) == "INC-1042"

    def test_service_is_the_fallback(self):
        """Keying None would round-robin an incident's pre-correlation
        events across partitions and lose their relative order."""
        assert partition_key(make_event(correlation_id=None)) == "payment-api"

    def test_same_incident_always_keys_the_same(self):
        a = make_event(correlation_id="INC-1", event_type=EventType.POD_FAILED)
        b = make_event(correlation_id="INC-1", event_type=EventType.INCIDENT_RESOLVED)
        assert partition_key(a) == partition_key(b)


class TestWireFormat:
    def test_round_trip_preserves_the_event(self):
        event = make_event(correlation_id="INC-7", metadata={"severity": "high"})
        assert deserialize(serialize(event)) == event

    def test_malformed_payload_does_not_deserialize(self):
        with pytest.raises(Exception):
            deserialize(b"{not json")

    def test_valid_json_that_is_not_an_event_does_not_deserialize(self):
        """The poison case that matters most: well-formed JSON carrying the
        wrong shape, which a bare json.loads would happily accept."""
        with pytest.raises(Exception):
            deserialize(json.dumps({"hello": "world"}).encode())

    def test_json_or_none_tolerates_garbage(self):
        assert json_or_none(b"{not json") is None
        assert json_or_none(b'{"a": 1}') == {"a": 1}


class TestDeadLetterRecord:
    def test_text_decodes_leniently(self):
        """A dead letter may not be valid UTF-8 - rendering it must not
        raise, or inspecting the DLQ becomes impossible."""
        dl = DeadLetter(
            payload=b"\xff\xfe bad",
            error="x",
            source_topic="t",
            source_partition=0,
            source_offset=1,
        )
        assert "bad" in dl.text


# --- Integration tier ---------------------------------------------------------


def _broker_up() -> bool:
    try:
        from confluent_kafka.admin import AdminClient

        AdminClient({"bootstrap.servers": BOOTSTRAP}).list_topics(timeout=5)
        return True
    except Exception:
        return False


@pytest.fixture
def topics():
    """A unique events/DLQ topic pair, created and torn down per test."""
    from confluent_kafka.admin import AdminClient, NewTopic

    if not _broker_up():
        pytest.skip("kafka not reachable - run scripts/kafka-up.sh")

    suffix = uuid.uuid4().hex[:8]
    events, dlq = f"test.events.{suffix}", f"test.dlq.{suffix}"
    admin = AdminClient({"bootstrap.servers": BOOTSTRAP})
    for future in admin.create_topics(
        [
            NewTopic(events, num_partitions=3, replication_factor=1),
            NewTopic(dlq, num_partitions=1, replication_factor=1),
        ]
    ).values():
        future.result(timeout=15)
    yield events, dlq
    for future in admin.delete_topics([events, dlq]).values():
        try:
            future.result(timeout=15)
        except Exception:
            pass  # teardown must not fail a passing test


def drain(topic, dlq, group=None, handler=None, max_events=None, expect=None):
    """Consume `topic` from the beginning, collecting events."""
    seen = []
    consumer = EventConsumer(
        group_id=group or f"g-{uuid.uuid4().hex[:8]}",
        bootstrap_servers=BOOTSTRAP,
        topic=topic,
        dlq_topic=dlq,
        from_beginning=True,
    )
    try:
        consumer.consume(handler or seen.append, max_events=max_events or expect)
    finally:
        consumer.close()
    return seen


@pytest.mark.integration
class TestDelivery:
    """The exit criterion: produced events are consumed reliably."""

    def test_every_produced_event_is_consumed(self, topics):
        events_topic, dlq = topics
        sent = [make_event(correlation_id=f"INC-{i}") for i in range(10)]
        producer = EventProducer(bootstrap_servers=BOOTSTRAP, topic=events_topic)
        producer.publish_many(sent)
        producer.flush()

        got = drain(events_topic, dlq, expect=10)
        assert len(got) == 10
        assert {e.event_id for e in got} == {e.event_id for e in sent}

    def test_event_survives_the_round_trip_intact(self, topics):
        events_topic, dlq = topics
        sent = make_event(correlation_id="INC-42", metadata={"severity": "high", "n": [1, 2]})
        producer = EventProducer(bootstrap_servers=BOOTSTRAP, topic=events_topic)
        producer.publish(sent)
        producer.flush()

        got = drain(events_topic, dlq, expect=1)
        assert got == [sent]

    def test_one_incident_keeps_its_order(self, topics):
        """Same key means one partition, and Kafka orders within a
        partition - that is the whole reason for keying on correlation_id."""
        events_topic, dlq = topics
        sent = [make_event(correlation_id="INC-order", metadata={"seq": i}) for i in range(20)]
        producer = EventProducer(bootstrap_servers=BOOTSTRAP, topic=events_topic)
        producer.publish_many(sent)
        producer.flush()

        got = drain(events_topic, dlq, expect=20)
        assert [e.metadata["seq"] for e in got] == list(range(20))


@pytest.mark.integration
class TestPoisonMessages:
    """The exit criterion: a malformed event lands in the DLQ instead of
    stalling the consumer."""

    def test_malformed_record_goes_to_the_dlq_and_good_events_still_flow(self, topics):
        events_topic, dlq = topics
        producer = EventProducer(bootstrap_servers=BOOTSTRAP, topic=events_topic)
        # Same key so all three land on one partition: if the poison
        # record blocked, the event behind it could not arrive.
        first = make_event(correlation_id="INC-poison", metadata={"seq": 0})
        producer.publish(first)
        producer.flush()
        publish_raw(
            b"{not an event", key="INC-poison", bootstrap_servers=BOOTSTRAP, topic=events_topic
        )
        second = make_event(correlation_id="INC-poison", metadata={"seq": 1})
        producer.publish(second)
        producer.flush()

        got = drain(events_topic, dlq, expect=2)
        assert [e.metadata["seq"] for e in got] == [0, 1], (
            "consumer stalled behind the poison record"
        )

        letters = read_dead_letters(
            bootstrap_servers=BOOTSTRAP, dlq_topic=dlq, group_id=f"dlq-{uuid.uuid4().hex[:8]}"
        )
        assert len(letters) == 1
        assert letters[0].payload == b"{not an event"
        assert letters[0].source_topic == events_topic

    def test_dead_letter_records_why_it_failed(self, topics):
        events_topic, dlq = topics
        publish_raw(
            json.dumps({"wrong": "shape"}).encode(),
            key="k",
            bootstrap_servers=BOOTSTRAP,
            topic=events_topic,
        )
        drain(events_topic, dlq, max_events=0)

        letters = read_dead_letters(
            bootstrap_servers=BOOTSTRAP, dlq_topic=dlq, group_id=f"dlq-{uuid.uuid4().hex[:8]}"
        )
        assert len(letters) == 1
        assert "ValidationError" in letters[0].error
        # The original bytes are kept verbatim - re-encoding would destroy
        # the evidence of what actually arrived.
        assert json_or_none(letters[0].payload) == {"wrong": "shape"}

    def test_reading_the_dlq_does_not_consume_it(self, topics):
        """Inspection must be repeatable: a second reader has to see the
        same records."""
        events_topic, dlq = topics
        publish_raw(b"garbage", key="k", bootstrap_servers=BOOTSTRAP, topic=events_topic)
        drain(events_topic, dlq, max_events=0)

        first = read_dead_letters(bootstrap_servers=BOOTSTRAP, dlq_topic=dlq)
        second = read_dead_letters(bootstrap_servers=BOOTSTRAP, dlq_topic=dlq)
        assert len(first) == 1
        assert [d.payload for d in first] == [d.payload for d in second]


@pytest.mark.integration
class TestOffsets:
    """At-least-once: commit after handling, never before."""

    def test_a_group_resumes_where_it_left_off(self, topics):
        events_topic, dlq = topics
        producer = EventProducer(bootstrap_servers=BOOTSTRAP, topic=events_topic)
        producer.publish_many([make_event(correlation_id=f"INC-{i}") for i in range(6)])
        producer.flush()

        group = f"resume-{uuid.uuid4().hex[:8]}"
        first = drain(events_topic, dlq, group=group, max_events=3)
        assert len(first) == 3

        second = drain(events_topic, dlq, group=group, expect=3)
        assert len(second) == 3
        assert not {e.event_id for e in first} & {e.event_id for e in second}

    def test_separate_groups_each_see_everything(self, topics):
        events_topic, dlq = topics
        producer = EventProducer(bootstrap_servers=BOOTSTRAP, topic=events_topic)
        producer.publish_many([make_event(correlation_id=f"INC-{i}") for i in range(4)])
        producer.flush()

        a = drain(events_topic, dlq, expect=4)
        b = drain(events_topic, dlq, expect=4)
        assert {e.event_id for e in a} == {e.event_id for e in b}

    def test_a_failing_handler_does_not_commit_its_offset(self, topics):
        """The event is well-formed and simply was not processed, so it
        must be redelivered rather than silently dropped."""
        events_topic, dlq = topics
        producer = EventProducer(bootstrap_servers=BOOTSTRAP, topic=events_topic)
        sent = make_event(correlation_id="INC-boom")
        producer.publish(sent)
        producer.flush()

        group = f"boom-{uuid.uuid4().hex[:8]}"
        with pytest.raises(RuntimeError, match="handler exploded"):
            drain(
                events_topic,
                dlq,
                group=group,
                handler=lambda _e: (_ for _ in ()).throw(RuntimeError("handler exploded")),
                max_events=1,
            )

        redelivered = drain(events_topic, dlq, group=group, expect=1)
        assert [e.event_id for e in redelivered] == [sent.event_id]


@pytest.mark.integration
class TestDurability:
    """The exit criterion: a broker restart loses nothing."""

    def test_events_survive_a_broker_restart(self, topics):
        events_topic, dlq = topics
        sent = [make_event(correlation_id=f"INC-{i}") for i in range(5)]
        producer = EventProducer(bootstrap_servers=BOOTSTRAP, topic=events_topic)
        producer.publish_many(sent)
        producer.flush()  # acks=all, so these are on disk before we restart

        subprocess.run(["docker", "restart", "helix-kafka"], check=True, capture_output=True)
        deadline = __import__("time").time() + 120
        while __import__("time").time() < deadline:
            if _broker_up():
                break
        else:
            pytest.fail("broker did not come back within 120s")

        got = drain(events_topic, dlq, expect=5)
        assert {e.event_id for e in got} == {e.event_id for e in sent}


@pytest.mark.integration
class TestDeclaredTopics:
    """kafka-up.sh's topic design, read back off the broker rather than
    trusted from the script's own output."""

    def test_declared_topics_exist_with_their_partition_counts(self):
        if not _broker_up():
            pytest.skip("kafka not reachable - run scripts/kafka-up.sh")
        config = topic_config(bootstrap_servers=BOOTSTRAP)
        assert config["helix.events"]["exists"]
        assert config["helix.events"]["partitions"] == 3
        assert config["helix.events.dlq"]["exists"]
        assert config["helix.events.dlq"]["partitions"] == 1


class TestFollowMode:
    """`helix consume --follow` has to outlive a quiet topic: E2 bounded the
    loop at three idle polls and left the daemon to E4, which never built
    it, so a consumer exited within seconds of the topic going quiet."""

    def _consumer_over(self, polls):
        class Msg:
            def __init__(self, payload):
                self._payload = payload

            def error(self):
                return None

            def value(self):
                return self._payload

        class Inner:
            def __init__(self):
                self._polls = list(polls)

            def poll(self, timeout):
                item = self._polls.pop(0)
                return None if item is None else Msg(serialize(item))

            def assignment(self):
                return ["partition-0"]

            def commit(self, msg, asynchronous=False):
                pass

        consumer = EventConsumer.__new__(EventConsumer)
        consumer._consumer = Inner()
        return consumer

    def test_bounded_mode_stops_when_the_topic_goes_quiet(self):
        late = make_event(correlation_id="INC-late")
        consumer = self._consumer_over([None, None, None, None, None, late])
        assert consumer.consume(lambda e: None, max_events=1, timeout=0) == 0

    def test_follow_mode_outlasts_a_quiet_topic(self):
        late = make_event(correlation_id="INC-late")
        consumer = self._consumer_over([None, None, None, None, None, late])
        got = []
        assert consumer.consume(got.append, max_events=1, timeout=0, idle_rounds=None) == 1
        assert got == [late]
