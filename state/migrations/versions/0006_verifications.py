"""verifications

Subphase 1.9 table. `verifications`: one row per verify-node run - the
post-action desired-state record. `checks` is a JSONB list of per-check
{name, status, detail, evidence_id} where status is pass|fail|skipped|
error; `outcome` is the binary verdict (a failed or un-runnable check can
never count toward recovery). `window_seconds`/`poll_seconds` record the
stability window sampled, `started_at`/`finished_at` the wall-clock span.
`needs_compensation` marks a failed verification on a compensable action -
the compensation hook is 1.10; this flag is the recorded debt.
Must stay in step with state/models.py.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-18
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from state.models import VerificationOutcome

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

_VERIFICATION_OUTCOMES = [o.value for o in VerificationOutcome]


def upgrade() -> None:
    op.create_table(
        "verifications",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "incident_id",
            sa.Integer(),
            sa.ForeignKey("incidents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "execution_id",
            sa.Integer(),
            sa.ForeignKey("executions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "candidate_id",
            sa.Integer(),
            sa.ForeignKey("candidates.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("params", postgresql.JSONB(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "outcome",
            sa.Enum(
                *_VERIFICATION_OUTCOMES,
                name="verification_outcome",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("checks", postgresql.JSONB(), nullable=False),
        sa.Column("window_seconds", sa.Float(), nullable=False),
        sa.Column("poll_seconds", sa.Float(), nullable=False),
        sa.Column(
            "needs_compensation",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_verifications_incident_id", "verifications", ["incident_id"])


def downgrade() -> None:
    op.drop_index("ix_verifications_incident_id", table_name="verifications")
    op.drop_table("verifications")
