"""The canonical EngineeringEvent envelope (subphase E1)."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from events.schemas import (
    SCHEMA_VERSION,
    EngineeringEvent,
    EventSource,
    EventType,
    new_event_id,
)

MINIMAL = {
    "event_type": EventType.POD_FAILED,
    "source": EventSource.KUBERNETES,
    "service": "payment-api",
    "environment": "sandbox",
}


class TestTaxonomy:
    """The decisions recorded in events/schemas.py, asserted so they
    cannot drift back silently."""

    def test_service_unhealthy_is_in_the_taxonomy(self):
        """Accepted: the recorded dependency-failure fault leaves the pod
        Running and failing readiness, which neither pod.failed nor
        deployment.failed describes honestly."""
        assert EventType.SERVICE_UNHEALTHY == "service.unhealthy"

    def test_branch_diverged_is_in_the_taxonomy(self):
        """Added: stale-branch had no label at all."""
        assert EventType.BRANCH_DIVERGED == "branch.diverged"

    def test_rollout_failed_was_rejected(self):
        """deployment.failed covers both scenarios that would want it, and
        CONTEXT.md section 7 says expand only on a real need."""
        assert "rollout.failed" not in {t.value for t in EventType}

    def test_context_taxonomy_is_present_in_full(self):
        """The starting taxonomy is CONTEXT.md section 7's, so dropping a
        member is as much a change as adding one."""
        required = {
            "push.created",
            "pr.opened",
            "pr.updated",
            "pr.approved",
            "pr.rejected",
            "pr.merged",
            "build.started",
            "build.passed",
            "build.failed",
            "deployment.started",
            "deployment.failed",
            "deployment.succeeded",
            "pod.failed",
            "incident.created",
            "remediation.requested",
            "approval.requested",
            "remediation.completed",
            "incident.resolved",
        }
        assert required <= {t.value for t in EventType}


class TestEnvelope:
    def test_defaults_fill_the_generated_fields(self):
        event = EngineeringEvent(**MINIMAL)
        assert event.schema_version == SCHEMA_VERSION
        assert event.event_id.startswith("evt_")
        assert event.actor == "system"
        assert event.correlation_id is None
        assert event.metadata == {}
        assert event.timestamp.tzinfo is not None, "timestamps must be tz-aware"

    def test_event_ids_are_unique(self):
        assert len({new_event_id() for _ in range(100)}) == 100

    def test_metadata_is_open(self):
        """MINOR-compatible change per the compatibility rule: a new
        metadata key must not require a schema bump."""
        event = EngineeringEvent(**MINIMAL, metadata={"anything": {"nested": [1, 2]}})
        assert event.metadata["anything"]["nested"] == [1, 2]

    def test_envelope_is_closed(self):
        """A misspelled top-level field is a producer bug, and accepting it
        as free-form data would hide it."""
        with pytest.raises(ValidationError):
            EngineeringEvent(**MINIMAL, servcie="payment-api")

    def test_unknown_event_type_is_rejected(self):
        with pytest.raises(ValidationError):
            EngineeringEvent(**{**MINIMAL, "event_type": "pod.exploded"})

    def test_unknown_source_is_rejected(self):
        with pytest.raises(ValidationError):
            EngineeringEvent(**{**MINIMAL, "source": "gitlab"})

    @pytest.mark.parametrize("field", ["service", "environment", "actor"])
    def test_identity_fields_reject_empty_strings(self, field):
        with pytest.raises(ValidationError):
            EngineeringEvent(**{**MINIMAL, field: ""})

    def test_helix_is_a_valid_source(self):
        """E5 emits incident.created and friends, which have no vendor
        origin."""
        event = EngineeringEvent(
            **{**MINIMAL, "source": EventSource.HELIX, "event_type": EventType.INCIDENT_CREATED}
        )
        assert event.source == EventSource.HELIX

    def test_round_trips_through_json(self):
        """The envelope has to survive a broker in E2, so it must
        serialize and come back equal."""
        event = EngineeringEvent(
            **MINIMAL,
            correlation_id="INC-1042",
            timestamp=datetime(2026, 9, 19, 12, 0, tzinfo=UTC),
            metadata={"severity": "high"},
        )
        assert EngineeringEvent.model_validate_json(event.model_dump_json()) == event
