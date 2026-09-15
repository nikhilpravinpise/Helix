"""approvals + executions

Subphase 1.8 tables. `approvals`: one row per approval-gated action -
created `pending` in the same transaction as the validating ->
awaiting_approval transition, decided via `helix approve`/`helix reject`
or the API, and consulted again by the executor before any gated write
runs (defense in depth). The partial unique index ux_approvals_one_pending
keeps at most one pending decision per incident so "the pending approval"
is unambiguous. `executions`: the audit record - one row per attempt with
the action + params, the policy decision, the approval ref and approver,
timestamps, result and the transport's output payload (for fixture-mode
fakes that payload IS the recorded intended write the benchmark scores).
Must stay in step with state/models.py.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-17
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from state.models import ApprovalStatus, ExecutionResult

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

_APPROVAL_STATUSES = [s.value for s in ApprovalStatus]
_EXECUTION_RESULTS = [r.value for r in ExecutionResult]


def upgrade() -> None:
    op.create_table(
        "approvals",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "incident_id",
            sa.Integer(),
            sa.ForeignKey("incidents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "candidate_id",
            sa.Integer(),
            sa.ForeignKey("candidates.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("params", postgresql.JSONB(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.Enum(*_APPROVAL_STATUSES, name="approval_status", native_enum=False, length=16),
            nullable=False,
        ),
        sa.Column("decided_by", sa.String(length=128), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_approvals_incident_id", "approvals", ["incident_id"])
    op.create_index(
        "ux_approvals_one_pending",
        "approvals",
        ["incident_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )

    op.create_table(
        "executions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "incident_id",
            sa.Integer(),
            sa.ForeignKey("incidents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "candidate_id",
            sa.Integer(),
            sa.ForeignKey("candidates.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "approval_id",
            sa.Integer(),
            sa.ForeignKey("approvals.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("params", postgresql.JSONB(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("policy_decision", sa.String(length=32), nullable=False),
        sa.Column("verification_required", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("decided_by", sa.String(length=128), nullable=True),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column(
            "result",
            sa.Enum(*_EXECUTION_RESULTS, name="execution_result", native_enum=False, length=16),
            nullable=False,
        ),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("output", postgresql.JSONB(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_executions_incident_id", "executions", ["incident_id"])


def downgrade() -> None:
    op.drop_index("ix_executions_incident_id", table_name="executions")
    op.drop_table("executions")
    op.drop_index("ux_approvals_one_pending", table_name="approvals")
    op.drop_index("ix_approvals_incident_id", table_name="approvals")
    op.drop_table("approvals")
