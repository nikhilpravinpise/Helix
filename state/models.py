"""SQLAlchemy models for Helix incident state (CONTEXT.md section 13).

Three tables land in subphase 1.4: `incidents` (the canonical active-state
record), `signals` (every raw intake payload attached to an incident) and
`transitions` (the persisted status-change audit written by
state/machine.py). Later subphases add their own tables alongside these -
the schema grows with the code that uses it.

Notes on the choices made here:

- `incidents.fingerprint` is the dedup key computed by state/intake.py; it
  is indexed with `status` because dedup looks for an OPEN incident
  carrying a given fingerprint.
- `status` columns are non-native enums (VARCHAR holding the
  IncidentStatus value). The edge map in machine.py is the authoritative
  enforcement; a DB-level CHECK would just force migration churn when
  Phase 2 statuses arrive.
- `signature`/`raw` are JSONB on Postgres (the real store) with a plain
  JSON variant so in-memory SQLite can still host contract tests.
- `linked_pr`/`linked_deployment` are strings: they hold external
  identifiers (PR number or URL, deployment/build id), not local FKs.
- `transitions.from_status` is nullable so the creation record
  (NULL -> received) can live in the same table as real transitions.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Integer, String, Text, func
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
