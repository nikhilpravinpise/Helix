"""SQLAlchemy models for Helix incident state (CONTEXT.md section 13).

Subphase 1.4 landed `incidents` (the canonical active-state record),
`signals` (every raw intake payload attached to an incident) and
`transitions` (the persisted status-change audit written by
state/machine.py). Subphase 1.5 adds `evidence_items` (every collected
Evidence, so the full evidence chain is a DB query), `hypotheses` (the
Diagnosis Agent's recorded claims) and `workflow_runs` (the
single-active-run guard, hardened in 1.10). Later subphases add their own
tables alongside these - the schema grows with the code that uses it.

Notes on the choices made here:

- `incidents.fingerprint` is the dedup key computed by state/intake.py; it
  is indexed with `status` because dedup looks for an OPEN incident
  carrying a given fingerprint.
- `status` columns are non-native enums (VARCHAR holding the
  IncidentStatus value). The edge map in machine.py is the authoritative
  enforcement; a DB-level CHECK would just force migration churn when
  Phase 2 statuses arrive.
- `signature`/`raw`/`payload` are JSONB on Postgres (the real store) with
  a plain JSON variant so in-memory SQLite can still host contract tests.
- `linked_pr`/`linked_deployment` are strings: they hold external
  identifiers (PR number or URL, deployment/build id), not local FKs.
- `transitions.from_status` is nullable so the creation record
  (NULL -> received) can live in the same table as real transitions.
- `evidence_items` carries `service`/`truncated`/`total` beyond the
  minimal column list so a stored row reconstructs the Evidence object
  faithfully; `redacted` is omitted because build_evidence guarantees it
  is always True.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from state.machine import IncidentStatus


class Base(DeclarativeBase):
    """Declarative base for the incident-state schema."""


def _status_type() -> Enum:
    """IncidentStatus as stored strings ("received", ...). native_enum=False
    keeps it a VARCHAR on Postgres and portable to SQLite; validate_strings
    lets columns accept and return the enum either way."""
    return Enum(
        IncidentStatus,
        native_enum=False,
        validate_strings=True,
        values_callable=lambda e: [m.value for m in e],
        name="incident_status",
        length=32,
    )


def _json_doc() -> JSON:
    """JSONB on Postgres, plain JSON on SQLite (test engines)."""
    return JSONB().with_variant(JSON(), "sqlite")


class Incident(Base):
    """The canonical incident record. One row per deduplicated failure."""

    __tablename__ = "incidents"
    __table_args__ = (Index("ix_incidents_fingerprint_status", "fingerprint", "status"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    incident_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    service: Mapped[str] = mapped_column(String(128))
    environment: Mapped[str] = mapped_column(String(32), default="sandbox")
    severity: Mapped[str] = mapped_column(String(32), default="medium")
    status: Mapped[IncidentStatus] = mapped_column(_status_type(), default=IncidentStatus.RECEIVED)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    linked_pr: Mapped[str | None] = mapped_column(String(256))
    linked_deployment: Mapped[str | None] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    signals: Mapped[list["Signal"]] = relationship(back_populates="incident", order_by="Signal.id")
    transitions: Mapped[list["Transition"]] = relationship(
        back_populates="incident", order_by="Transition.id"
    )
    evidence_items: Mapped[list["EvidenceItem"]] = relationship(
        back_populates="incident", order_by="EvidenceItem.id"
    )
    hypotheses: Mapped[list["Hypothesis"]] = relationship(
        back_populates="incident", order_by="Hypothesis.id"
    )
    workflow_runs: Mapped[list["WorkflowRun"]] = relationship(
        back_populates="incident", order_by="WorkflowRun.id"
    )


class Signal(Base):
    """One intake payload attached to an incident. `signature` holds the
    fault-identifying fields the dedup fingerprint is computed over; `raw`
    keeps the full payload for evidence and debugging."""

    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    incident_id: Mapped[int] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), index=True
    )
    source: Mapped[str] = mapped_column(String(32))
    environment: Mapped[str] = mapped_column(String(32))
    severity: Mapped[str] = mapped_column(String(32))
    signature: Mapped[dict[str, Any]] = mapped_column(_json_doc())
    raw: Mapped[dict[str, Any]] = mapped_column(_json_doc())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    incident: Mapped[Incident] = relationship(back_populates="signals")


class Transition(Base):
    """A persisted status change. from_status is NULL only on the creation
    row (NULL -> received) written by machine.record_creation."""

    __tablename__ = "transitions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    incident_id: Mapped[int] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), index=True
    )
    from_status: Mapped[IncidentStatus | None] = mapped_column(_status_type(), nullable=True)
    to_status: Mapped[IncidentStatus] = mapped_column(_status_type())
    cause: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    incident: Mapped[Incident] = relationship(back_populates="transitions")


class EvidenceItem(Base):
    """One collected Evidence persisted for an incident (subphase 1.5).

    `evidence_id` is the content hash (`ev_*`) the evidence layer stamps;
    hypotheses cite it and the 1.9 provenance record resolves it. Ordering
    by `id` replays the collection order - "the full evidence chain is
    reconstructable from stored state" is this table, not a memory
    artifact. Unique per incident so re-collecting identical content is
    idempotent.
    """

    __tablename__ = "evidence_items"
    __table_args__ = (
        Index(
            "ix_evidence_items_incident_evidence",
            "incident_id",
            "evidence_id",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # No single-column index here: the composite unique index's leftmost
    # prefix already serves incident_id lookups.
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"))
    evidence_id: Mapped[str] = mapped_column(String(32))
    source: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(32))
    service: Mapped[str] = mapped_column(String(128))
    summary: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(_json_doc())
    origin: Mapped[str] = mapped_column(String(16))
    truncated: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    total: Mapped[int | None] = mapped_column(Integer)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    incident: Mapped[Incident] = relationship(back_populates="evidence_items")


class Hypothesis(Base):
    """A recorded diagnosis: claim, cited evidence refs, confidence and
    the signals that contradict it. `attempt` is the incident's
    attempt_count at write time, so rediagnosis rows coexist."""

    __tablename__ = "hypotheses"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    incident_id: Mapped[int] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), index=True
    )
    claim: Mapped[str] = mapped_column(Text)
    evidence_refs: Mapped[list[str]] = mapped_column(_json_doc())
    confidence: Mapped[float] = mapped_column(Float)
    contradicting_signals: Mapped[list[str]] = mapped_column(_json_doc())
    attempt: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    incident: Mapped[Incident] = relationship(back_populates="hypotheses")


class RunStatus(StrEnum):
    """Workflow-run lifecycle (workflow_runs.status). `suspended` is the
    D13 state: the checkpoint holds the graph state and any process can
    resume the run. `terminal` means the run reached its current end
    state (for 1.5: a hypothesis at remediation_pending; later:
    recovered/escalated)."""

    RUNNING = "running"
    SUSPENDED = "suspended"
    TERMINAL = "terminal"
    FAILED = "failed"


def _run_status_type() -> Enum:
    """RunStatus as stored strings; same non-native-enum rationale as the
    incident status type."""
    return Enum(
        RunStatus,
        native_enum=False,
        validate_strings=True,
        values_callable=lambda e: [m.value for m in e],
        name="run_status",
        length=16,
    )


class WorkflowRun(Base):
    """One invocation of the workflow graph for an incident.

    The single-active-run guard: `start_run` in agent/runner.py refuses
    while a `running` row exists for the incident, and the partial unique
    index `ux_workflow_runs_one_running` (migration 0002) makes that
    enforceable under concurrency - two transactions cannot both insert a
    running row. Full lifecycle hardening (stale-run reclamation) is 1.10.
    """

    __tablename__ = "workflow_runs"
    __table_args__ = (
        # The single-active-run guard's hard backstop: at most one
        # `running` row per incident, enforced by the database itself.
        # Declared here (not only in migration 0002) so create_all-based
        # test databases enforce it too.
        Index(
            "ux_workflow_runs_one_running",
            "incident_id",
            unique=True,
            postgresql_where=text("status = 'running'"),
            sqlite_where=text("status = 'running'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    incident_id: Mapped[int] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[RunStatus] = mapped_column(_run_status_type(), default=RunStatus.RUNNING)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    incident: Mapped[Incident] = relationship(back_populates="workflow_runs")
