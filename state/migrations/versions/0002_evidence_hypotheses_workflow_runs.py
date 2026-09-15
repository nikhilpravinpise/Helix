"""evidence_items, hypotheses, workflow_runs

Subphase 1.5 tables: every collected Evidence lands in `evidence_items`
(the reconstructable evidence chain), Diagnosis Agent output lands in
`hypotheses`, and `workflow_runs` is the single-active-run guard (D13
substrate, hardened in 1.10). Must stay in step with state/models.py.

The partial unique index on workflow_runs makes "at most one running run
per incident" a database guarantee, not a check-then-act - two concurrent
transactions cannot both insert a running row.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-16
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from state.models import RunStatus

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

_RUN_STATUSES = [s.value for s in RunStatus]


def upgrade() -> None:
    op.create_table(
        "evidence_items",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "incident_id",
            sa.Integer(),
            sa.ForeignKey("incidents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("evidence_id", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("service", sa.String(length=128), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("truncated", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("total", sa.Integer(), nullable=True),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
    )
    # Unique (incident_id, evidence_id): re-collecting identical content is
    # idempotent, and the leftmost prefix serves incident_id lookups.
    op.create_index(
        "ix_evidence_items_incident_evidence",
        "evidence_items",
        ["incident_id", "evidence_id"],
        unique=True,
    )

    op.create_table(
        "hypotheses",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "incident_id",
            sa.Integer(),
            sa.ForeignKey("incidents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("claim", sa.Text(), nullable=False),
        sa.Column("evidence_refs", postgresql.JSONB(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("contradicting_signals", postgresql.JSONB(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_hypotheses_incident_id", "hypotheses", ["incident_id"])

    op.create_table(
        "workflow_runs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "incident_id",
            sa.Integer(),
            sa.ForeignKey("incidents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(*_RUN_STATUSES, name="run_status", native_enum=False, length=16),
            nullable=False,
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_workflow_runs_incident_id", "workflow_runs", ["incident_id"])
    # The single-active-run guard's hard backstop (D13/1.10): at most one
    # `running` row per incident, enforced by the database itself.
    op.create_index(
        "ux_workflow_runs_one_running",
        "workflow_runs",
        ["incident_id"],
        unique=True,
        postgresql_where=sa.text("status = 'running'"),
        sqlite_where=sa.text("status = 'running'"),
    )


def downgrade() -> None:
    op.drop_index("ux_workflow_runs_one_running", table_name="workflow_runs")
    op.drop_index("ix_workflow_runs_incident_id", table_name="workflow_runs")
    op.drop_table("workflow_runs")
    op.drop_index("ix_hypotheses_incident_id", table_name="hypotheses")
    op.drop_table("hypotheses")
    op.drop_index("ix_evidence_items_incident_evidence", table_name="evidence_items")
    op.drop_table("evidence_items")
