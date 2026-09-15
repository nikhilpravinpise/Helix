"""candidates

Subphase 1.6 table: the Recovery Planner's full ranked candidate list per
attempt - every proposed action, its rank, rationale, cited evidence refs,
score and score breakdown - so the provenance timeline's "candidates"
stage is a DB query. Must stay in step with state/models.py.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-16
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "candidates",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "incident_id",
            sa.Integer(),
            sa.ForeignKey("incidents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("params", postgresql.JSONB(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("evidence_refs", postgresql.JSONB(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("risk_tier", sa.String(length=16), nullable=False),
        sa.Column("selected", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("components", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_candidates_incident_id", "candidates", ["incident_id"])


def downgrade() -> None:
    op.drop_index("ix_candidates_incident_id", table_name="candidates")
    op.drop_table("candidates")
