"""Signal validation, fingerprint and dedup tests.

Fingerprint/signature logic is pure (no DB). The dedup tests are
integration-marked against compose Postgres - including the subphase exit
criterion: the same signal three times produces one incident with three
attached signals.
"""

import json

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from events.schemas import EventSource, EventType
from helix.cli import main as helix_main
from state import models
from state.db import session_scope
from state.intake import (
    Signal,
    SignalSource,
    fault_signature,
    fingerprint,
    intake_signal,
)
from state.machine import IncidentStatus, transition

K8S_SIGNAL = {
    "source": "kubernetes",
    "service": "payment-api",
    "environment": "sandbox",
    "severity": "high",
    "signature": {"waiting_reason": "CrashLoopBackOff", "container": "payment-api"},
    "raw": {"pod": "payment-api-7c886558c8-f29pk", "namespace": "payment-api"},
}

JENKINS_SIGNAL = {
    "source": "jenkins",
    "service": "payment-api",
    "signature": {"job": "payment-api", "stage": "Test"},
    "raw": {"job": "payment-api", "build_number": 42},
}


class TestCanonicalEvent:
    """E1: intake normalizes every signal into the canonical envelope in
    process, and an unnameable signal still opens an incident."""

    def test_intake_returns_the_canonical_event(self, db_session):
        result = intake_signal(db_session, K8S_SIGNAL)
        event = result.event
        assert event is not None
        assert event.event_type is EventType.POD_FAILED
        assert event.source is EventSource.KUBERNETES
        assert event.service == "payment-api"
        assert event.environment == "sandbox"
        assert event.actor == "system"

    def test_correlation_id_is_the_incident_it_landed_on(self, db_session):
        """CONTEXT.md section 7 shows correlation_id carrying the incident
        id, so it can only be filled once dedup has resolved which
        incident the signal belongs to."""
        result = intake_signal(db_session, K8S_SIGNAL)
        assert result.event.correlation_id == result.incident_id

    def test_a_deduped_signal_correlates_to_the_same_incident(self, db_session):
        first = intake_signal(db_session, K8S_SIGNAL)
        second = intake_signal(db_session, dict(K8S_SIGNAL))
        assert second.created is False
        assert second.event.correlation_id == first.incident_id

    def test_source_specific_detail_lands_in_metadata(self, db_session):
        """The envelope stays stable across sources because everything
        source-shaped goes to metadata - including severity, which
        CONTEXT.md section 7 has no field for."""
        event = intake_signal(db_session, K8S_SIGNAL).event
        assert event.metadata["severity"] == "high"
        assert event.metadata["signature"] == K8S_SIGNAL["signature"]
        assert event.metadata["raw"] == K8S_SIGNAL["raw"]

    def test_jenkins_signal_becomes_a_build_failure(self, db_session):
        event = intake_signal(db_session, JENKINS_SIGNAL).event
        assert event.event_type is EventType.BUILD_FAILED
        assert event.source is EventSource.JENKINS

    def test_unclassifiable_signal_still_opens_an_incident(self, db_session):
        """Refusing it would drop a real failure on the floor, which is the
        one thing an incident system must not do. The incident is created
        and the event is simply absent."""
        odd = {**K8S_SIGNAL, "signature": {}, "raw": {}}
        result = intake_signal(db_session, odd)
        assert result.created is True
        assert result.incident_id.startswith("INC-")
        assert result.event is None

    def test_creation_transition_records_the_event_type(self, db_session):
        """The classification is visible in provenance rather than only in
        a return value that nothing persists yet."""
        result = intake_signal(db_session, K8S_SIGNAL)
        incident = db_session.scalar(
            select(models.Incident).where(models.Incident.incident_id == result.incident_id)
        )
        assert "pod.failed" in incident.transitions[0].cause

    def test_creation_transition_records_an_unclassified_signal(self, db_session):
        odd = {**K8S_SIGNAL, "signature": {}, "raw": {}}
        result = intake_signal(db_session, odd)
        incident = db_session.scalar(
            select(models.Incident).where(models.Incident.incident_id == result.incident_id)
        )
        assert "unclassified" in incident.transitions[0].cause


class TestSignalValidation:
    """The pydantic contract - malformed payloads are rejected before any
    DB work happens."""

    def test_valid_signal_with_defaults(self):
        signal = Signal.model_validate({"source": "jenkins", "service": "payment-api"})
        assert signal.source == SignalSource.JENKINS
        assert signal.environment == "sandbox"
        assert signal.severity == "medium"
        assert signal.signature == {}
        assert signal.raw == {}

    def test_full_signal(self):
        signal = Signal.model_validate(K8S_SIGNAL)
        assert signal.severity == "high"
        assert signal.signature["waiting_reason"] == "CrashLoopBackOff"

    @pytest.mark.parametrize(
        "payload",
        [
            {},  # missing source + service
            {"source": "kubernetes"},  # missing service
            {"service": "payment-api"},  # missing source
            {"source": "pagerduty", "service": "payment-api"},  # unknown source
            {"source": "jenkins", "service": ""},  # empty service
            {"source": "jenkins", "service": "x", "environment": "qa"},  # bad taxonomy
            {"source": "jenkins", "service": "x", "signature": "not-a-dict"},
        ],
    )
    def test_malformed_signals_rejected(self, payload):
        with pytest.raises(ValidationError):
            Signal.model_validate(payload)


class TestFaultSignature:
    """Per-source projection: only the documented keys identify a fault."""

    def test_kubernetes_projects_waiting_reason_and_container(self):
        signal = Signal.model_validate(K8S_SIGNAL)
        assert fault_signature(signal) == {
            "waiting_reason": "CrashLoopBackOff",
            "container": "payment-api",
        }

    def test_jenkins_projects_job_and_stage(self):
        signal = Signal.model_validate(JENKINS_SIGNAL)
        assert fault_signature(signal) == {"job": "payment-api", "stage": "Test"}

    def test_kubernetes_projects_termination_reason(self):
        """`terminated_reason` separates crashloop from oom-killed; without
        it both are CrashLoopBackOff on the same container."""
        signal = Signal.model_validate(
            {
                "source": "kubernetes",
                "service": "payment-api",
                "signature": {
                    "waiting_reason": "CrashLoopBackOff",
                    "container": "payment-api",
                    "terminated_reason": "OOMKilled",
                    "exit_code": 137,
                },
            }
        )
        assert fault_signature(signal) == {
            "waiting_reason": "CrashLoopBackOff",
            "container": "payment-api",
            "terminated_reason": "OOMKilled",
        }

    def test_signal_without_termination_reason_is_unaffected(self):
        """ImagePullBackOff carries no termination reason - the projection
        takes only keys that are present, so its fingerprint is unchanged."""
        signal = Signal.model_validate(
            {
                "source": "kubernetes",
                "service": "payment-api",
                "signature": {
                    "waiting_reason": "ImagePullBackOff",
                    "container": "payment-api",
                    "image": "payment-api:doesnotexist",
                },
            }
        )
        assert fault_signature(signal) == {
            "waiting_reason": "ImagePullBackOff",
            "container": "payment-api",
        }

    def test_unknown_keys_fall_back_to_whole_signature(self):
        # A k8s signal carrying none of the documented keys: whole
        # signature is used - deterministic, biased to separate incidents.
        signal = Signal.model_validate(
            {"source": "kubernetes", "service": "x", "signature": {"pod": "p1"}}
        )
        assert fault_signature(signal) == {"pod": "p1"}

    def test_github_uses_whole_signature(self):
        signature = {"check": "ci", "conclusion": "failure"}
        signal = Signal.model_validate({"source": "github", "service": "x", "signature": signature})
        assert fault_signature(signal) == signature


class TestFingerprint:
    """Pure dedup-fingerprint logic."""

    def test_same_signal_same_fingerprint(self):
        a = Signal.model_validate(K8S_SIGNAL)
        b = Signal.model_validate(dict(K8S_SIGNAL))
        assert fingerprint(a) == fingerprint(b)
        assert len(fingerprint(a)) == 64  # full sha256 hex

    def test_dict_key_order_does_not_matter(self):
        a = Signal.model_validate(K8S_SIGNAL)
        reordered = dict(K8S_SIGNAL)
        reordered["signature"] = {"container": "payment-api", "waiting_reason": "CrashLoopBackOff"}
        b = Signal.model_validate(reordered)
        assert fingerprint(a) == fingerprint(b)

    def test_volatile_fields_do_not_change_fingerprint(self):
        # The pod name suffix changes on every trigger of the same fault;
        # it must not split the incident.
        a = Signal.model_validate(K8S_SIGNAL)
        noisy = dict(K8S_SIGNAL)
        noisy["signature"] = {
            "waiting_reason": "CrashLoopBackOff",
            "container": "payment-api",
            "pod": "payment-api-9f8d7c6b5-x1y2z",  # volatile extra key
        }
        noisy["raw"] = {"pod": "payment-api-9f8d7c6b5-x1y2z", "ts": "2026-09-15T01:00:00Z"}
        b = Signal.model_validate(noisy)
        assert fingerprint(a) == fingerprint(b)

    def test_different_fault_different_fingerprint(self):
        a = Signal.model_validate(K8S_SIGNAL)
        other = dict(K8S_SIGNAL)
        other["signature"] = {
            "waiting_reason": "CreateContainerConfigError",
            "container": "payment-api",
        }
        b = Signal.model_validate(other)
        assert fingerprint(a) != fingerprint(b)

    @pytest.mark.parametrize(
        "field,value",
        [
            ("source", "jenkins"),
            ("service", "downstream-stub"),
            ("environment", "staging"),
        ],
    )
    def test_different_identity_different_fingerprint(self, field, value):
        a = Signal.model_validate(K8S_SIGNAL)
        changed = dict(K8S_SIGNAL)
        changed[field] = value
        b = Signal.model_validate(changed)
        assert fingerprint(a) != fingerprint(b)


def _count(session, model) -> int:
    return session.scalar(select(func.count(model.id)))


@pytest.mark.integration
class TestIntakeDedup:
    """DB-backed dedup behavior - the subphase's literal exit criterion."""

    def test_same_fingerprint_three_times_one_incident_three_signals(self, pg):
        for _ in range(3):
            with session_scope(pg) as session:
                result = intake_signal(session, K8S_SIGNAL)
        assert result.created is False  # the last call attached

        with session_scope(pg) as session:
            assert _count(session, models.Incident) == 1
            assert _count(session, models.Signal) == 3
            incident = session.scalar(select(models.Incident))
            assert len(incident.signals) == 3
            assert incident.status == IncidentStatus.RECEIVED
            # Exactly one transitions row: the creation record. Attaching a
            # signal is not a status change.
            assert len(incident.transitions) == 1
            assert incident.transitions[0].from_status is None
            assert incident.transitions[0].to_status == IncidentStatus.RECEIVED

    def test_crashloop_and_oom_do_not_share_an_incident(self, pg):
        """Two distinct faults with distinct optimal actions must not
        collide in dedup. They previously fingerprinted identically, so an
        OOM signal arriving while a crashloop incident was open attached to
        it and inherited the crashloop diagnosis - reapply_manifest instead
        of rollback_deployment. Stale open incidents from killed runs are
        the documented trigger, so the open-incident path is what matters
        here, not the terminal one."""
        crashloop = {
            "source": "kubernetes",
            "service": "payment-api",
            "signature": {
                "waiting_reason": "CrashLoopBackOff",
                "container": "payment-api",
                "terminated_reason": "Error",
                "exit_code": 1,
            },
        }
        oom = {
            "source": "kubernetes",
            "service": "payment-api",
            "signature": {
                "waiting_reason": "CrashLoopBackOff",
                "container": "payment-api",
                "terminated_reason": "OOMKilled",
                "exit_code": 137,
            },
        }
        with session_scope(pg) as session:
            first = intake_signal(session, crashloop)
        with session_scope(pg) as session:
            second = intake_signal(session, oom)

        assert first.created is True
        assert second.created is True, "OOM attached to the still-open crashloop incident"
        assert first.incident_id != second.incident_id
        with session_scope(pg) as session:
            assert _count(session, models.Incident) == 2

    def test_terminal_incident_same_fingerprint_new_incident(self, pg):
        with session_scope(pg) as session:
            first = intake_signal(session, K8S_SIGNAL)
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == first.incident_id)
            )
            # Drive it to a terminal status through the machine.
            transition(incident, IncidentStatus.DIAGNOSING, cause="start", session=session)
            transition(
                incident,
                IncidentStatus.REMEDIATION_PENDING,
                cause="hypothesis",
                session=session,
            )
            transition(incident, IncidentStatus.ESCALATED, cause="no safe action", session=session)

        with session_scope(pg) as session:
            second = intake_signal(session, K8S_SIGNAL)
        assert second.created is True
        assert second.incident_id != first.incident_id

        with session_scope(pg) as session:
            assert _count(session, models.Incident) == 2
            assert _count(session, models.Signal) == 2

    def test_open_incident_attaches_until_terminal(self, pg):
        with session_scope(pg) as session:
            first = intake_signal(session, K8S_SIGNAL)
        with session_scope(pg) as session:
            again = intake_signal(session, K8S_SIGNAL)
        assert again.created is False
        assert again.incident_id == first.incident_id

    def test_cli_intake_file_writes_same_rows_as_intake(self, pg, tmp_path, capsys):
        """`helix intake --file` goes through the same intake_signal() the
        API uses - the rows it writes must be identical in shape."""
        path = tmp_path / "signal.json"
        path.write_text(json.dumps(JENKINS_SIGNAL))
        assert helix_main(["intake", "--file", str(path)]) == 0
        printed = capsys.readouterr().out.strip()
        assert printed.startswith("INC-")

        with session_scope(pg) as session:
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == printed)
            )
            assert incident is not None
            assert incident.service == "payment-api"
            assert incident.status == IncidentStatus.RECEIVED
            assert len(incident.signals) == 1
            assert incident.signals[0].source == "jenkins"
            assert incident.signals[0].signature == {"job": "payment-api", "stage": "Test"}
            assert len(incident.transitions) == 1

    def test_cli_intake_rejects_malformed_signal(self, pg, tmp_path, capsys):
        path = tmp_path / "bad.json"
        path.write_text(json.dumps({"source": "kubernetes"}))  # missing service
        assert helix_main(["intake", "--file", str(path)]) == 2
        with session_scope(pg) as session:
            assert _count(session, models.Incident) == 0
