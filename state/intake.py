"""Signal intake: validation, dedup fingerprinting, and incident creation.

`intake_signal()` is the single shared path used by BOTH the API endpoints
(helix/api.py) and `helix intake`, so the benchmark harness can drive
intake without the API server running (D6, push model).

Deduplication: a signal's fingerprint is sha256 over
`source | service | environment | canonical(fault_signature)`. A signal
matching an OPEN (non-terminal) incident's fingerprint attaches as another
`signals` row; otherwise a new incident is created. A signal matching only
a terminal incident's fingerprint starts a NEW incident - a recovered or
failed failure surfacing again is a new occurrence, not a continuation.

KNOWN LIMITATION: dedup on THIS path is same-source only. One underlying
failure surfacing as both a Jenkins and a Kubernetes signal produces two
incidents. Subphase E4 lifts that, but for the event stream rather than
here: `events.ingest` correlates across sources by shared token, and the
signal path is left alone deliberately so Phase 1 behaviour stays
unchanged. Also, two signals arriving concurrently on different sessions
can race past the open-incident lookup and create two incidents -
bounded-concurrency handling is 1.10.
"""

import hashlib
import json
import secrets
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from events.classify import UnclassifiedSignal, classify
from events.schemas import EngineeringEvent, EventSource, EventType
from state import machine, models
from state.machine import IncidentStatus


class SignalSource(StrEnum):
    """The vendor domain a failure signal came from."""

    KUBERNETES = "kubernetes"
    JENKINS = "jenkins"
    GITHUB = "github"


class Signal(BaseModel):
    """One failure signal delivered to intake.

    `signature` carries the fault-identifying fields the fingerprint is
    computed over (see `_FAULT_KEYS`); `raw` keeps the full payload for
    later evidence collection and debugging.

    `environment` is the incident's TARGET environment - deliberately
    distinct from HELIX_ENVIRONMENT, which is Helix's own runtime mode.
    """

    source: SignalSource
    service: str = Field(min_length=1, max_length=128)
    environment: Literal["sandbox", "staging", "production"] = "sandbox"
    severity: str = Field(default="medium", min_length=1, max_length=32)
    signature: dict[str, Any] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict)


class IntakeResult(BaseModel):
    """What intake did with a signal: the incident it created or attached
    to, the persisted signal row id, and the canonical event the signal
    was normalized into.

    The event is carried here rather than stored: E1 keeps it in process
    (this is what "pass them in process" means), E2 gives it a producer to
    publish to, and E5 gives it a durable history store. Returning it now
    is what lets those land without reworking intake again.
    """

    incident_id: str
    created: bool
    signal_id: int
    #: None when no rule names the signal - see intake_signal.
    event: EngineeringEvent | None = None


#: The signature keys that identify a fault, per source. Only these fields
#: feed the fingerprint, so volatile fields elsewhere in `signature` (pod
#: name suffixes, build numbers) cannot split one failure into multiple
#: incidents. kubernetes: pod waiting reason + container name + how the
#: container last terminated; jenkins: job + failing stage.
#:
#: `terminated_reason` is load-bearing, not decoration. Without it
#: `crashloop` and `oom-killed` are indistinguishable - both are
#: CrashLoopBackOff on container payment-api, and only the termination
#: reason (Error vs OOMKilled) separates them. They fingerprinted
#: identically, so an OOM signal arriving while a crashloop incident was
#: still open attached to it instead of opening its own, and was then
#: diagnosed as a crashloop: reapply_manifest rather than the
#: rollback_deployment an OOM needs. Stale open incidents from killed runs
#: are a documented occurrence, which is exactly when this fires.
#:
#: Signals that carry no `terminated_reason` (ImagePullBackOff,
#: CreateContainerConfigError) are unaffected: the projection only takes
#: keys that are present.
_FAULT_KEYS: dict[SignalSource, tuple[str, ...]] = {
    SignalSource.KUBERNETES: ("waiting_reason", "container", "terminated_reason"),
    SignalSource.JENKINS: ("job", "stage"),
}


def fault_signature(signal: Signal) -> dict[str, Any]:
    """The fault-identifying subset of `signal.signature` used for dedup.

    For sources with documented fault keys this projects onto exactly those
    keys. If none of the documented keys are present - or the source has no
    documented set (github) - the whole signature is used, which stays
    deterministic while erring toward separate incidents over wrong merges.
    """
    keys = _FAULT_KEYS.get(signal.source)
    if keys is not None:
        projected = {k: signal.signature[k] for k in keys if k in signal.signature}
        if projected:
            return projected
    return signal.signature


def fingerprint(signal: Signal) -> str:
    """sha256 over `source | service | environment | canonical(signature)`.

    Canonical means sorted keys and compact separators, so dict ordering in
    the emitter cannot change the fingerprint. The full hex digest is kept;
    storage is cheap and collisions must be essentially impossible.
    """
    canonical = json.dumps(
        fault_signature(signal), sort_keys=True, separators=(",", ":"), default=str
    )
    material = f"{signal.source.value}|{signal.service}|{signal.environment}|{canonical}"
    return hashlib.sha256(material.encode()).hexdigest()


def build_event(
    signal: Signal, event_type: EventType, correlation_id: str | None = None
) -> EngineeringEvent:
    """Normalize a Phase 1 signal into the canonical envelope.

    Everything source-specific goes to `metadata` - the signature, the raw
    vendor payload, and `severity`, which the CONTEXT.md section 7
    envelope has no field for. This is the boundary the E3 adapters will
    take over: they will build events from vendor payloads directly, and
    `Signal` stops being the intermediate shape.
    """
    return EngineeringEvent(
        event_type=event_type,
        source=EventSource(signal.source.value),
        correlation_id=correlation_id,
        service=signal.service,
        environment=signal.environment,
        metadata={
            "severity": signal.severity,
            "signature": signal.signature,
            "raw": signal.raw,
        },
    )


def _new_incident_id() -> str:
    """`INC-<8 hex>` - random rather than sequential so ids never depend on
    DB sequences (keeps SQLite test engines equal to Postgres). The unique
    index on incident_id is the collision guard."""
    return f"INC-{secrets.token_hex(4)}"


def find_open_incident(session: Session, fp: str) -> models.Incident | None:
    """The oldest open incident carrying fingerprint `fp`, if any."""
    return session.scalar(
        select(models.Incident)
        .where(models.Incident.fingerprint == fp)
        .where(models.Incident.status.not_in(machine.TERMINAL_STATUSES))
        .order_by(models.Incident.created_at, models.Incident.id)
        .limit(1)
    )


def intake_signal(session: Session, payload: Signal | dict[str, Any]) -> IntakeResult:
    """Validate `payload` as a Signal and record it.

    Attaches to the open incident carrying the same fingerprint, else
    creates a new incident in RECEIVED plus its creation transition row.
    Flushes but never commits - the caller owns the transaction boundary
    (state.db.session_scope), so signal, incident and transition rows land
    atomically.
    """
    signal = payload if isinstance(payload, Signal) else Signal.model_validate(payload)
    # An unclassifiable signal still opens an incident. Refusing it at the
    # front door would drop a real failure on the floor, which is the one
    # thing an incident recovery system must not do, and it matches how
    # the rest of Helix treats a signature it does not recognise: the
    # rules arm escalates on an unmatched signature rather than rejecting
    # it. The miss is recorded in the creation transition instead, so it
    # shows up in provenance rather than vanishing.
    try:
        event_type: EventType | None = classify(signal.source.value, signal.signature)
    except UnclassifiedSignal:
        event_type = None
    fp = fingerprint(signal)
    incident = find_open_incident(session, fp)
    created = incident is None
    if created:
        incident = models.Incident(
            incident_id=_new_incident_id(),
            fingerprint=fp,
            service=signal.service,
            environment=signal.environment,
            severity=signal.severity,
            status=IncidentStatus.RECEIVED,
        )
        session.add(incident)
        session.flush()  # assigns incident.id for the FK rows below
        machine.record_creation(
            incident,
            cause=(
                f"intake signal from {signal.source.value} "
                f"({event_type.value if event_type else 'unclassified'})"
            ),
            session=session,
        )
    row = models.Signal(
        incident_id=incident.id,
        source=signal.source.value,
        environment=signal.environment,
        severity=signal.severity,
        signature=signal.signature,
        raw=signal.raw,
    )
    session.add(row)
    session.flush()
    return IntakeResult(
        incident_id=incident.incident_id,
        created=created,
        signal_id=row.id,
        event=(
            build_event(signal, event_type, correlation_id=incident.incident_id)
            if event_type
            else None
        ),
    )
