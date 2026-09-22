"""End-to-end change-review lifecycle tests (subphase 2.1/2.5/2.6/2.9):
drives the real graph through `run_incident`, exactly like the Phase 1
unhappy-path tests drive the recovery loop - SQLite state, fixture
evidence, no LLM calls (Triage/Review are pure code).
"""

from datetime import UTC, datetime, timedelta

from fakes import FakeLLM, make_deps
from sqlalchemy import select

from agent.runner import run_incident
from events.ingest import ingest_event
from events.schemas import EngineeringEvent, EventSource, EventType
from state import models
from state.machine import IncidentStatus
from state.reviews import pending_for_incident, record_decision


def _open_pr(sessions, *, pr: int = 42, branch: str = "feature/fix-config") -> str:
    with sessions() as session:
        event = EngineeringEvent(
            event_type=EventType.PR_OPENED,
            source=EventSource.GITHUB,
            service="payment-api",
            environment="sandbox",
            actor="dana",
            metadata={
                "pr": pr,
                "title": "Fix payment config",
                "branch": branch,
                "sha": "a" * 40,
                "base": "main",
                "files": ["sandbox/payment-api/k8s/02-configmap.yaml"],
                "action": "opened",
            },
        )
        result = ingest_event(session, event)
        session.commit()
        return result.incident_id


def _push_updated(
    sessions, incident_id: str, *, pr: int = 42, branch: str = "feature/fix-config"
) -> None:
    with sessions() as session:
        incident = session.scalar(
            select(models.Incident).where(models.Incident.incident_id == incident_id)
        )
        event = EngineeringEvent(
            event_type=EventType.PR_UPDATED,
            source=EventSource.GITHUB,
            service="payment-api",
            environment="sandbox",
            correlation_id=incident_id,
            actor="dana",
            metadata={"pr": pr, "branch": branch, "sha": "b" * 40, "action": "synchronize"},
        )
        ingest_event(session, event)
        session.commit()
        assert incident is not None


def _deploy_event(sessions, incident_id: str, event_type: EventType) -> None:
    with sessions() as session:
        event = EngineeringEvent(
            event_type=event_type,
            source=EventSource.JENKINS,
            service="payment-api",
            environment="sandbox",
            correlation_id=incident_id,
            metadata={},
        )
        ingest_event(session, event)
        session.commit()


def _drive(deps, incident_id: str, *, arm: str = "A", max_hops: int = 10):
    """Repeatedly call `run_incident` until the status stops changing -
    the same "advance one hop, call again" idiom the benchmark harness's
    own `_drive` uses, since Phase 2 statuses are each their own stop
    exactly like Phase 1's awaiting_approval."""
    from agent.runner import run_incident as _run

    last = None
    result = None
    for _ in range(max_hops):
        result = _run(incident_id, deps=deps, arm=arm)
        if result.status == last:
            break
        last = result.status
    return result


class TestChangeLifecycleHappyPath:
    def test_pr_opened_triages_and_assigns_owner(self, sqlite_sessions, store) -> None:
        deps, _ = make_deps(sqlite_sessions, FakeLLM(), "cluster/crashloop", store)
        incident_id = _open_pr(sqlite_sessions)

        result = run_incident(incident_id, deps=deps, arm="A")
        assert result.status == IncidentStatus.TRIAGED
        # TRIAGED -> OWNER_ASSIGNED -> (review node runs) -> REVIEW_PENDING
        # all land in this one call: OWNER_ASSIGNED has a router case (the
        # review node), so the graph keeps going instead of stopping -
        # only TRIAGED itself (no router case) is a real per-invocation
        # stop, matching how DIAGNOSING keeps going but AWAITING_APPROVAL
        # does not.
        result = run_incident(incident_id, deps=deps, arm="A")
        assert result.status == IncidentStatus.REVIEW_PENDING

        with sqlite_sessions() as session:
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == incident_id)
            )
            assert incident.kind == models.IncidentKind.CHANGE
            assert incident.owner == "alice"
            transitions = [t.to_status.value for t in incident.transitions]
            assert transitions == [
                "received",
                "triaged",
                "owner_assigned",
                "review_pending",
            ]
            triage = session.scalars(
                select(models.TriageResult).where(models.TriageResult.incident_id == incident.id)
            ).all()
            assert len(triage) == 1
            assert triage[0].owner == "alice"
            pending = pending_for_incident(session, incident.id)
            assert pending is not None
            assert pending.reviewer != "dana"  # never self-review

    def test_approved_review_advances_to_deploying(self, sqlite_sessions, store) -> None:
        deps, _ = make_deps(sqlite_sessions, FakeLLM(), "cluster/crashloop", store)
        incident_id = _open_pr(sqlite_sessions)
        _drive(deps, incident_id)

        with sqlite_sessions() as session:
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == incident_id)
            )
            pending = pending_for_incident(session, incident.id)
            record_decision(session, pending, True, "bob")
            session.commit()

        result = _drive(deps, incident_id)
        assert result.status == IncidentStatus.DEPLOYING

    def test_deploy_success_recovers_the_change_incident(self, sqlite_sessions, store) -> None:
        deps, _ = make_deps(sqlite_sessions, FakeLLM(), "cluster/crashloop", store)
        incident_id = _open_pr(sqlite_sessions)
        _drive(deps, incident_id)
        with sqlite_sessions() as session:
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == incident_id)
            )
            record_decision(session, pending_for_incident(session, incident.id), True, "bob")
            session.commit()
        _drive(deps, incident_id)  # -> deploying

        _deploy_event(sqlite_sessions, incident_id, EventType.BUILD_PASSED)
        result = run_incident(incident_id, deps=deps, arm="A")

        assert result.status == IncidentStatus.RECOVERED

    def test_deploy_failure_hands_off_to_the_recovery_loop(self, sqlite_sessions, store) -> None:
        deps, _ = make_deps(sqlite_sessions, FakeLLM(), "cluster/crashloop", store)
        incident_id = _open_pr(sqlite_sessions)
        _drive(deps, incident_id)
        with sqlite_sessions() as session:
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == incident_id)
            )
            record_decision(session, pending_for_incident(session, incident.id), True, "bob")
            session.commit()
        _drive(deps, incident_id)  # -> deploying

        _deploy_event(sqlite_sessions, incident_id, EventType.BUILD_FAILED)
        result = run_incident(incident_id, deps=deps, arm="A")

        # The SAME Ops loop a recovery incident uses now owns the run:
        # arm A's rules_diagnosis ran (an unmatched github-only signature
        # honestly escalates rather than fabricating a diagnosis - the
        # handoff wiring is what this test proves, not rule coverage).
        assert result.status in (IncidentStatus.ESCALATED, IncidentStatus.FAILED)
        assert result.hypothesis is not None


class TestChangeLifecycleRejectionLoop:
    def test_rejected_review_parks_at_fix_required_then_re_reviews_on_new_commit(
        self, sqlite_sessions, store
    ) -> None:
        deps, _ = make_deps(sqlite_sessions, FakeLLM(), "cluster/crashloop", store)
        incident_id = _open_pr(sqlite_sessions)
        _drive(deps, incident_id)

        with sqlite_sessions() as session:
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == incident_id)
            )
            pending = pending_for_incident(session, incident.id)
            first_reviewer = pending.reviewer
            record_decision(session, pending, False, first_reviewer, note="fix the URL")
            session.commit()

        result = _drive(deps, incident_id)
        assert result.status == IncidentStatus.FIX_REQUIRED

        # No new commit yet - the run stays parked, not looping forever.
        result = run_incident(incident_id, deps=deps, arm="A")
        assert result.status == IncidentStatus.FIX_REQUIRED

        _push_updated(sqlite_sessions, incident_id)
        result = _drive(deps, incident_id)
        assert result.status == IncidentStatus.REVIEW_PENDING

        with sqlite_sessions() as session:
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == incident_id)
            )
            pending = pending_for_incident(session, incident.id)
            assert pending is not None
            # The rejecting reviewer is excluded from the re-request.
            assert pending.reviewer != first_reviewer


class TestChangeLifecycleDuplicateDetection:
    def test_second_incident_same_branch_is_flagged_duplicate_and_escalates(
        self, sqlite_sessions, store
    ) -> None:
        deps, _ = make_deps(sqlite_sessions, FakeLLM(), "cluster/crashloop", store)
        # Build two independent CHANGE incidents on the same branch
        # directly - bypassing events.ingest's own token correlation
        # (which would otherwise fold a same-service PR within the
        # correlation window into ONE incident before Triage ever runs),
        # to exercise Triage's own residual duplicate-detection path.
        with sqlite_sessions() as session:
            original = models.Incident(
                incident_id="INC-orig0001",
                fingerprint="o" * 64,
                service="payment-api",
                environment="sandbox",
                severity="medium",
                status=IncidentStatus.REVIEW_PENDING,
                kind=models.IncidentKind.CHANGE,
            )
            session.add(original)
            session.flush()
            session.add(
                models.EventRecord(
                    incident_id=original.id,
                    event_id="evt_orig0001",
                    event_type=EventType.PR_OPENED.value,
                    source=EventSource.GITHUB.value,
                    service="payment-api",
                    environment="sandbox",
                    occurred_at=datetime.now(UTC) - timedelta(minutes=5),
                    envelope={
                        "actor": "dana",
                        "metadata": {"branch": "feature/fix-config", "pr": 41, "files": []},
                    },
                )
            )
            duplicate = models.Incident(
                incident_id="INC-dup00001",
                fingerprint="d" * 64,
                service="payment-api",
                environment="sandbox",
                severity="medium",
                status=IncidentStatus.RECEIVED,
                kind=models.IncidentKind.CHANGE,
            )
            session.add(duplicate)
            session.flush()
            session.add(
                models.EventRecord(
                    incident_id=duplicate.id,
                    event_id="evt_dup00001",
                    event_type=EventType.PR_OPENED.value,
                    source=EventSource.GITHUB.value,
                    service="payment-api",
                    environment="sandbox",
                    occurred_at=datetime.now(UTC),
                    envelope={
                        "actor": "erin",
                        "metadata": {"branch": "feature/fix-config", "pr": 43, "files": []},
                    },
                )
            )
            session.commit()

        result = run_incident("INC-dup00001", deps=deps, arm="A")

        assert result.status == IncidentStatus.ESCALATED
        with sqlite_sessions() as session:
            dup = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == "INC-dup00001")
            )
            assert dup.duplicate_of_id is not None
            assert dup.duplicate_of.incident_id == "INC-orig0001"


class TestChangeLifecycleUnknownService:
    def test_unregistered_service_falls_back_to_the_commit_author(
        self, sqlite_sessions, store
    ) -> None:
        """A service outside the registry still resolves an owner from a
        real commit author - that is 2.5's declared "commit history"
        signal, not a guess."""
        deps, _ = make_deps(sqlite_sessions, FakeLLM(), "cluster/crashloop", store)
        with sqlite_sessions() as session:
            event = EngineeringEvent(
                event_type=EventType.PR_OPENED,
                source=EventSource.GITHUB,
                service="unregistered-service",
                environment="sandbox",
                actor="frank",
                metadata={"pr": 99, "branch": "feature/x", "files": ["unregistered/x.py"]},
            )
            result = ingest_event(session, event)
            session.commit()
            incident_id = result.incident_id

        result = run_incident(incident_id, deps=deps, arm="A")
        assert result.status == IncidentStatus.TRIAGED
        with sqlite_sessions() as session:
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == incident_id)
            )
            triage = incident.triage_results[0]
            assert triage.owner == "frank"
            assert triage.owner_source == "commit_history"

    def test_unregistered_service_with_no_real_actor_escalates_without_guessing(
        self, sqlite_sessions, store
    ) -> None:
        """No registry entry AND no real actor (the "system" sentinel for
        a machine-originated event, events/schemas.py) - genuinely
        unknown ownership must escalate, never silently resolve to an
        owner literally named "system"."""
        deps, _ = make_deps(sqlite_sessions, FakeLLM(), "cluster/crashloop", store)
        with sqlite_sessions() as session:
            event = EngineeringEvent(
                event_type=EventType.PR_OPENED,
                source=EventSource.GITHUB,
                service="unregistered-service",
                environment="sandbox",
                metadata={"pr": 100, "branch": "feature/y", "files": ["unregistered/y.py"]},
            )
            result = ingest_event(session, event)
            session.commit()
            incident_id = result.incident_id

        result = run_incident(incident_id, deps=deps, arm="A")
        assert result.status == IncidentStatus.ESCALATED
