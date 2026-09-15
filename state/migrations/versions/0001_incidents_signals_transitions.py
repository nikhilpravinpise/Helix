"""incidents, signals, transitions

Initial incident-state schema for subphase 1.4 (CONTEXT.md section 13).
Must stay in step with state/models.py - env.py imports that metadata, so
`alembic check` / autogenerate will flag any drift.

Revision ID: 0001
Revises: -
Create Date: 2026-09-15
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from state.machine import IncidentStatus

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

# Stored values are the enum's lowercase values ("received", ...), matching
# what the model's values_callable maps. Non-native enum: a VARCHAR, not a
# PG ENUM type, so adding a status later does not require ALTER TYPE.
_STATUSES = [s.value for s in IncidentStatus]


def _status_type() -> sa.Enum:
    # One instance per column - a shared Enum instance across columns gets
    # tangled in SQLAlchemy's schema-type bookkeeping.
    return sa.Enum(*_STATUSES, name="incident_status", native_enum=False, length=32)


def upgrade() -> None:
    op.create_table(
        "incidents",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("incident_id", sa.String(length=32), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("service", sa.String(length=128), nullable=False),
        sa.Column("environment", sa.String(length=32), nullable=False),
        sa.Column("severity", sa.String(length=32), nullable=False),
        sa.Column("status", _status_type(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("linked_pr", sa.String(length=256), nullable=True),
        sa.Column("linked_deployment", sa.String(length=256), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_incidents_incident_id", "incidents", ["incident_id"], unique=True)
    # The dedup lookup filters fingerprint + open status; one composite
    # index serves both it and plain fingerprint lookups (leftmost prefix).
    op.create_index("ix_incidents_fingerprint_status", "incidents", ["fingerprint", "status"])

    op.create_table(
        "signals",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "incident_id",
            sa.Integer(),
            sa.ForeignKey("incidents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("environment", sa.String(length=32), nullable=False),
        sa.Column("severity", sa.String(length=32), nullable=False),
        sa.Column("signature", postgresql.JSONB(), nullable=False),
        sa.Column("raw", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_signals_incident_id", "signals", ["incident_id"])

    op.create_table(
        "transitions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "incident_id",
            sa.Integer(),
            sa.ForeignKey("incidents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # NULL only on the creation row (NULL -> received) that
        # machine.record_creation writes; real transitions always carry
        # both ends.
        sa.Column("from_status", _status_type(), nullable=True),
        sa.Column("to_status", _status_type(), nullable=False),
        sa.Column("cause", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_transitions_incident_id", "transitions", ["incident_id"])


def downgrade() -> None:
    op.drop_index("ix_transitions_incident_id", table_name="transitions")
    op.drop_table("transitions")
    op.drop_index("ix_signals_incident_id", table_name="signals")
    op.drop_table("signals")
    op.drop_index("ix_incidents_fingerprint_status", table_name="incidents")
    op.drop_index("ix_incidents_incident_id", table_name="incidents")
    op.drop_table("incidents")
