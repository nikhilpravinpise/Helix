"""Vendor payload to canonical event adapters (subphase E3).

Driven off the captured fixtures wherever one exists, so these assert
against payload shapes a real cluster and a real GitHub actually produced
rather than against shapes convenient to the adapter.

The exception is Elasticsearch: `fixtures/faults/*` stores logs as
pre-formatted `<timestamp> [<pod>] <message>` strings, which have no
document id, so there is no captured raw `_search` response to drive the
ELK adapter from. Those tests build the response shape that
`integrations.elk` queries for and say so, rather than pretending to
replay a capture.
"""

import json
from pathlib import Path

import pytest

from events.adapters import elk, github, jenkins, kubernetes
from events.schemas import EventSource, EventType, derive_event_id

FIXTURES = Path("fixtures/faults")


def fixture(relative: str) -> dict:
    return json.loads((FIXTURES / relative).read_text(encoding="utf-8"))


class TestDerivedIds:
    """The E3 idempotency requirement lives or dies here."""

    def test_same_natural_key_gives_the_same_id(self):
        assert derive_event_id("jenkins", "payment-api", 2) == derive_event_id(
            "jenkins", "payment-api", 2
        )

    def test_different_key_gives_a_different_id(self):
        assert derive_event_id("jenkins", "payment-api", 2) != derive_event_id(
            "jenkins", "payment-api", 3
        )

    def test_source_is_part_of_the_key(self):
        """Two vendors may well number things identically; the id must not
        collide because of it."""
        assert derive_event_id("jenkins", "x", 1) != derive_event_id("github", "x", 1)

    def test_shape_matches_a_generated_id(self):
        derived = derive_event_id("kubernetes", "uid", "c", 0)
        assert derived.startswith("evt_")
        assert len(derived) == len("evt_") + 12


class TestKubernetesAdapter:
    def test_crashloop_pod_becomes_pod_failed(self):
        payload = fixture("cluster/crashloop.json")["evidence"]["pods_payment_api"]
        events = kubernetes.pod_events(payload)
        assert [e.event_type for e in events] == [EventType.POD_FAILED]
        meta = events[0].metadata
        assert meta["waiting_reason"] == "CrashLoopBackOff"
        assert meta["terminated_reason"] == "Error"
        assert events[0].source is EventSource.KUBERNETES

    def test_oom_is_distinguishable_from_an_ordinary_crashloop(self):
        """Both are CrashLoopBackOff; only the termination reason separates
        them, and losing it is what collided the two in Phase 1 dedup."""
        crash = kubernetes.pod_events(
            fixture("cluster/crashloop.json")["evidence"]["pods_payment_api"]
        )
        oom = kubernetes.pod_events(
            fixture("cluster/oom-killed.json")["evidence"]["pods_payment_api"]
        )
        assert crash[0].metadata["terminated_reason"] == "Error"
        assert oom[0].metadata["terminated_reason"] == "OOMKilled"
        assert crash[0].event_id != oom[0].event_id

    def test_running_but_not_ready_pod_is_service_unhealthy(self):
        """The captured evidence behind the E1 taxonomy decision: the new
        ReplicaSet's pod is up with Ready false while the old one serves."""
        payload = fixture("cluster/config-mismatch.json")["evidence"]["pods_payment_api"]
        events = kubernetes.pod_events(payload)
        assert [e.event_type for e in events] == [EventType.SERVICE_UNHEALTHY]
        assert events[0].metadata["phase"] == "Running"

    def test_a_healthy_pod_produces_nothing(self):
        """Adapters emit failures, not a heartbeat per object. This fixture's
        pod is Ready: True - the sandbox declares no readiness probe, so its
        downstream being unreachable never reaches the pod's conditions."""
        payload = fixture("cluster/dependency-failure.json")["evidence"]["pods_payment_api"]
        assert kubernetes.pod_events(payload) == []

    def test_terminating_pods_are_not_failures(self):
        payload = {
            "items": [
                {
                    "metadata": {
                        "name": "p",
                        "uid": "u",
                        "deletionTimestamp": "2026-01-01T00:00:00Z",
                    },
                    "status": {
                        "phase": "Running",
                        "containerStatuses": [
                            {"name": "c", "state": {"waiting": {"reason": "CrashLoopBackOff"}}}
                        ],
                    },
                }
            ]
        }
        assert kubernetes.pod_events(payload) == []

    def test_only_warning_events_are_emitted(self):
        payload = fixture("cluster/crashloop.json")["evidence"]["events"]
        emitted = kubernetes.object_events(payload)
        assert emitted, "expected at least one Warning in the captured events"
        types = {item.get("type") for item in payload["items"]}
        assert "Normal" in types, "fixture should contain Normal events to be filtered out"
        assert len(emitted) < len(payload["items"])

    def test_event_id_is_stable_across_repeated_reads(self):
        payload = fixture("cluster/crashloop.json")["evidence"]["pods_payment_api"]
        assert [e.event_id for e in kubernetes.pod_events(payload)] == [
            e.event_id for e in kubernetes.pod_events(payload)
        ]

    @pytest.mark.parametrize("reason", ["ContainerCreating", "PodInitializing"])
    def test_a_pod_that_is_still_starting_is_not_a_failure(self, reason):
        """Every pod passes through these waiting reasons on a normal start.
        A healthy `rollout restart` of payment-api, sampled 60 times, made
        this adapter emit 12 pod.failed events for ContainerCreating - which
        a live watcher would have turned into an incident per deploy,
        including every deploy Helix makes to fix something."""
        pod = {
            "metadata": {"name": "payment-api-new", "uid": "u-1", "labels": {"app": "payment-api"}},
            "status": {
                "phase": "Pending",
                "containerStatuses": [
                    {
                        "name": "payment-api",
                        "state": {"waiting": {"reason": reason}},
                        "restartCount": 0,
                    }
                ],
            },
        }
        assert kubernetes.pod_events({"items": [pod]}) == []


class TestJenkinsAdapter:
    @pytest.mark.parametrize(
        "name", ["test-regression", "dependency-failure", "docker-build-failure"]
    )
    def test_captured_ci_failures_become_build_failed(self, name):
        event = jenkins.build_event(fixture(f"ci/{name}.json"))
        assert event.event_type is EventType.BUILD_FAILED
        assert event.source is EventSource.JENKINS
        assert event.metadata["job"] == "payment-api"

    def test_console_output_is_carried_as_evidence(self):
        event = jenkins.build_event(fixture("ci/test-regression.json"))
        assert event.metadata["console_output"]

    def test_a_short_console_log_is_not_clipped(self):
        event = jenkins.build_event(fixture("ci/dependency-failure.json"))
        assert event.metadata["console_truncated"] is False
        assert (
            event.metadata["console_output"]
            == fixture("ci/dependency-failure.json")["evidence"]["console_output"]
        )

    def test_a_clipped_log_keeps_both_ends_and_says_so(self):
        """A build log carries evidence at both ends - the failure detail
        early, the verdict last - so clipping toward either one alone
        discards half the diagnosis."""
        event = jenkins.build_event(
            {
                "job": "j",
                "number": 1,
                "result": "FAILURE",
                "console_output": "HEAD-MARKER" + ("x" * 10_000) + "TAIL-MARKER",
            }
        )
        output = event.metadata["console_output"]
        assert event.metadata["console_truncated"] is True
        assert output.startswith("HEAD-MARKER")
        assert output.endswith("TAIL-MARKER")
        assert "characters elided" in output

    def test_the_captured_failure_verdict_survives_clipping(self):
        """test-regression's log is 5214 characters, so it is clipped; the
        assertion is near the front and `Finished: FAILURE` is at the very
        end, and both have to come through."""
        event = jenkins.build_event(fixture("ci/test-regression.json"))
        output = event.metadata["console_output"]
        assert event.metadata["console_truncated"] is True
        assert "999" in output, "the failing assertion was clipped away"
        assert "Finished: FAILURE" in output, "the build verdict was clipped away"

    def test_success_is_a_build_passed(self):
        event = jenkins.build_event({"job": "j", "number": 4, "result": "SUCCESS"})
        assert event.event_type is EventType.BUILD_PASSED

    def test_unstable_counts_as_a_failure(self):
        """Jenkins uses UNSTABLE for a build whose tests failed, which is
        the test-regression scenario exactly."""
        event = jenkins.build_event({"job": "j", "number": 5, "result": "UNSTABLE"})
        assert event.event_type is EventType.BUILD_FAILED

    def test_aborted_and_running_builds_produce_nothing(self):
        assert jenkins.build_event({"job": "j", "number": 6, "result": "ABORTED"}) is None
        assert jenkins.build_event({"job": "j", "number": 7, "result": None}) is None

    def test_redelivery_yields_the_same_event_id(self):
        payload = fixture("ci/test-regression.json")
        assert jenkins.build_event(payload).event_id == jenkins.build_event(payload).event_id

    def test_a_different_build_gets_a_different_id(self):
        base = {"job": "payment-api", "result": "FAILURE"}
        assert (
            jenkins.build_event({**base, "number": 2}).event_id
            != jenkins.build_event({**base, "number": 3}).event_id
        )


class TestGitHubAdapter:
    def test_failing_check_run_becomes_build_failed(self):
        payload = fixture("github/failed-check.json")["evidence"]["github"]["check_runs"]
        events = github.check_run_events(payload)
        assert [e.event_type for e in events] == [EventType.BUILD_FAILED]
        assert events[0].metadata["check"] == "payment-api-ci"

    def test_changes_requested_becomes_pr_rejected(self):
        payload = fixture("github/rejected-pr.json")["evidence"]["github"]["pr_reviews"]
        events = github.review_events(payload)
        assert [e.event_type for e in events] == [EventType.PR_REJECTED]
        assert events[0].metadata["pr"] == 17

    def test_the_reviewer_is_the_actor(self):
        """Almost every Phase 1 event is machine-caused; a review is not."""
        payload = fixture("github/rejected-pr.json")["evidence"]["github"]["pr_reviews"]
        assert github.review_events(payload)[0].actor == "reviewer-sandbox"

    def test_diverged_branch_becomes_branch_diverged(self):
        payload = fixture("github/stale-branch.json")["evidence"]["github"]["branch_state"]
        events = github.branch_state_events(payload)
        assert [e.event_type for e in events] == [EventType.BRANCH_DIVERGED]
        assert events[0].metadata["behind_by"] == 14

    def test_a_branch_merely_ahead_is_not_an_event(self):
        """Every branch with unmerged work is ahead; that is not a fault."""
        assert github.branch_state_events({"name": "b", "sha": "s", "comparison": "ahead"}) == []

    def test_commented_reviews_are_not_decisions(self):
        payload = {"number": 1, "items": [{"id": 1, "state": "COMMENTED", "user": "x"}]}
        assert github.review_events(payload) == []

    def test_incomplete_check_runs_are_skipped(self):
        payload = {"ref": "abc", "items": [{"name": "ci", "status": "in_progress"}]}
        assert github.check_run_events(payload) == []

    def test_redelivery_yields_the_same_ids(self):
        payload = fixture("github/rejected-pr.json")["evidence"]["github"]["pr_reviews"]
        assert [e.event_id for e in github.review_events(payload)] == [
            e.event_id for e in github.review_events(payload)
        ]

    def test_pr_opened_actor_is_the_author_not_the_sender(self):
        """GitHub's pull_request webhook carries the author at
        `pull_request.user.login` and the trigger at `sender.login`; the
        event's actor must be the author - Review's self-review exclusion
        keys on it. A bare `payload.user` never existed in this schema."""
        payload = {
            "action": "opened",
            "number": 7,
            "pull_request": {"user": {"login": "dana"}},
            "sender": {"login": "webhook-bot"},
            "head": {"ref": "feature/x", "sha": "abc123"},
            "base": {"ref": "main"},
        }
        events = github.pull_request_events(payload)
        assert [e.event_type for e in events] == [EventType.PR_OPENED]
        assert events[0].actor == "dana"

    def test_a_real_pull_request_webhook_is_read_from_its_pull_request_block(self):
        """GitHub's pull_request webhook nests title, head and base inside
        `pull_request`; the top level carries only action, number,
        repository and sender. Fed a real PR object wrapped that way (from
        VrajVed/helix-sandbox#17), the adapter read branch, sha and title as
        None - and because the event id keys on the head sha, every push to
        a PR derived the same id and was dropped as a duplicate."""

        def hook(sha):
            return {
                "action": "synchronize",
                "number": 17,
                "pull_request": {
                    "number": 17,
                    "title": "Helix remediation for INC-aba102e5",
                    "user": {"login": "VrajVed"},
                    "head": {"ref": "helix/INC-aba102e5", "sha": sha},
                    "base": {"ref": "main"},
                },
                "repository": {"full_name": "VrajVed/helix-sandbox"},
                "sender": {"login": "VrajVed"},
            }

        first = github.pull_request_events(hook("a" * 40))[0]
        assert first.metadata["branch"] == "helix/INC-aba102e5"
        assert first.metadata["sha"] == "a" * 40
        assert first.metadata["base"] == "main"
        assert first.metadata["title"] == "Helix remediation for INC-aba102e5"
        second = github.pull_request_events(hook("b" * 40))[0]
        assert first.event_id != second.event_id

    def test_pr_actor_falls_back_to_sender_when_no_pr_block(self):
        """Webhook shapes that flatten `pull_request` (the benchmark's own
        event dicts, some proxy payloads) still carry a real actor via
        sender - never silently "system"."""
        payload = {
            "action": "synchronize",
            "number": 7,
            "sender": {"login": "dana"},
            "head": {"ref": "feature/x", "sha": "def456"},
            "base": {"ref": "main"},
        }
        assert github.pull_request_events(payload)[0].actor == "dana"

    def test_pr_updated_redelivery_with_new_sha_is_a_new_event(self):
        """A synchronize to a new head sha is a distinct update, not a
        duplicate - the derived id keys on it."""
        base = {"action": "synchronize", "number": 7, "sender": {"login": "d"}}
        e1 = github.pull_request_events({**base, "head": {"sha": "aaa"}})
        e2 = github.pull_request_events({**base, "head": {"sha": "bbb"}})
        assert e1[0].event_type is EventType.PR_UPDATED
        assert e1[0].event_id != e2[0].event_id


class TestElkAdapter:
    """Built against the `_search` response shape `integrations.elk` queries
    for - the fixtures store formatted log strings, which carry no document
    id to key an event on."""

    @staticmethod
    def response(*messages, index="helix-logs-2026.08.28"):
        return {
            "hits": {
                "hits": [
                    {
                        "_index": index,
                        "_id": f"doc{i}",
                        "_source": {
                            "@timestamp": "2026-08-28T12:09:36.962Z",
                            "message": message,
                            "kubernetes": {"pod": {"name": "payment-api-abc"}},
                        },
                    }
                    for i, message in enumerate(messages)
                ]
            }
        }

    def test_error_lines_become_service_unhealthy(self):
        captured = "ERROR:payment_api:startup failed: invalid configuration"
        events = elk.log_events(self.response(captured))
        assert [e.event_type for e in events] == [EventType.SERVICE_UNHEALTHY]
        assert events[0].source is EventSource.ELK
        assert events[0].metadata["pod"] == "payment-api-abc"

    def test_info_lines_produce_nothing(self):
        """An event per INFO line would be a firehose with no signal in it."""
        assert elk.log_events(self.response('INFO: "GET /health HTTP/1.1" 200 OK')) == []

    def test_an_explicit_level_field_wins_over_the_message(self):
        payload = self.response("some message")
        payload["hits"]["hits"][0]["_source"]["level"] = "error"
        assert len(elk.log_events(payload)) == 1

    def test_document_id_makes_redelivery_idempotent(self):
        payload = self.response("ERROR: boom")
        assert [e.event_id for e in elk.log_events(payload)] == [
            e.event_id for e in elk.log_events(payload)
        ]

    def test_empty_response_is_not_an_error(self):
        assert elk.log_events({"hits": {"hits": []}}) == []
        assert elk.log_events({}) == []


class TestNoVendorShapeEscapes:
    """The E3 exit criterion's second half: no vendor payload shape reaches
    an agent. Every adapter output is a canonical envelope, and vendor
    detail is confined to `metadata`."""

    def all_events(self):
        return [
            *kubernetes.pod_events(
                fixture("cluster/crashloop.json")["evidence"]["pods_payment_api"]
            ),
            *kubernetes.object_events(fixture("cluster/crashloop.json")["evidence"]["events"]),
            jenkins.build_event(fixture("ci/test-regression.json")),
            *github.check_run_events(
                fixture("github/failed-check.json")["evidence"]["github"]["check_runs"]
            ),
            *github.review_events(
                fixture("github/rejected-pr.json")["evidence"]["github"]["pr_reviews"]
            ),
            *github.branch_state_events(
                fixture("github/stale-branch.json")["evidence"]["github"]["branch_state"]
            ),
        ]

    def test_every_adapter_emits_the_canonical_envelope(self):
        canonical = {
            "schema_version",
            "event_id",
            "event_type",
            "source",
            "timestamp",
            "correlation_id",
            "service",
            "environment",
            "actor",
            "metadata",
        }
        for event in self.all_events():
            assert set(event.model_dump().keys()) == canonical

    def test_no_vendor_key_reaches_the_envelope(self):
        """`containerStatuses`, `involvedObject`, `conclusion` and friends
        may appear inside metadata as values or documented keys, but never
        as an envelope field."""
        vendor_keys = {"containerStatuses", "involvedObject", "lastState", "_source", "hits"}
        for event in self.all_events():
            assert not vendor_keys & set(event.model_dump().keys())

    def test_every_event_declares_a_taxonomy_type(self):
        for event in self.all_events():
            assert event.event_type in set(EventType)

    def test_secrets_in_vendor_text_are_redacted(self):
        """Events are a second path into agent context, so the guarantee
        subphase 1.3 made about evidence has to hold here too."""
        event = jenkins.build_event(
            {
                "job": "payment-api",
                "number": 9,
                "result": "FAILURE",
                "console_output": "using Authorization: Bearer sk-live-not-a-real-key",
            }
        )
        assert "sk-live-not-a-real-key" not in json.dumps(event.metadata)


@pytest.mark.integration
class TestAdaptedEventsReachKafka:
    """The E3 exit criterion's first half: every Phase 1 failure arrives as
    a canonical event on Kafka. Needs a broker - `bash scripts/kafka-up.sh`.
    """

    def test_every_captured_fault_round_trips_through_the_broker(self):
        import uuid as _uuid

        from confluent_kafka.admin import AdminClient, NewTopic

        from events.broker import EventConsumer, EventProducer

        bootstrap = "127.0.0.1:9092"
        try:
            AdminClient({"bootstrap.servers": bootstrap}).list_topics(timeout=5)
        except Exception:
            pytest.skip("kafka not reachable - run scripts/kafka-up.sh")

        suffix = _uuid.uuid4().hex[:8]
        topic, dlq = f"test.e3.{suffix}", f"test.e3dlq.{suffix}"
        admin = AdminClient({"bootstrap.servers": bootstrap})
        for future in admin.create_topics(
            [
                NewTopic(topic, num_partitions=1, replication_factor=1),
                NewTopic(dlq, num_partitions=1, replication_factor=1),
            ]
        ).values():
            future.result(timeout=15)

        try:
            produced = TestNoVendorShapeEscapes().all_events()
            assert len(produced) > 1, "expected events from several vendors"
            assert {e.source for e in produced} >= {
                EventSource.KUBERNETES,
                EventSource.JENKINS,
                EventSource.GITHUB,
            }

            producer = EventProducer(bootstrap_servers=bootstrap, topic=topic)
            producer.publish_many(produced)
            producer.flush()

            seen: list = []
            consumer = EventConsumer(
                group_id=f"e3-{suffix}",
                bootstrap_servers=bootstrap,
                topic=topic,
                dlq_topic=dlq,
                from_beginning=True,
            )
            try:
                consumer.consume(seen.append, max_events=len(produced))
            finally:
                consumer.close()

            # Identity survives the broker, so a consumer can deduplicate on
            # event_id alone - which is what E3's idempotency buys E4.
            assert {e.event_id for e in seen} == {e.event_id for e in produced}
            assert sorted(seen, key=lambda e: e.event_id) == sorted(
                produced, key=lambda e: e.event_id
            )
        finally:
            for future in admin.delete_topics([topic, dlq]).values():
                try:
                    future.result(timeout=15)
                except Exception:
                    pass
