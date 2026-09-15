"""validations

Subphase 1.7 table: one row per validate-node pass - the action + params
checked, the candidate row they came from, the verdict (pass | fail |
not_validatable), the reason, and the strategy's check-by-check details.
`fingerprint` (sha256 of action | canonical params) is the candidate's
identity for repeat-action prevention: the `fail` rows are the persisted
exclusion list the replan consults. Must stay in step with
state/models.py.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-16
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from state.models import ValidationOutcome

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

_OUTCOMES = [o.value for o in ValidationOutcome]


def upgrade() -> None:
    op.create_table(
        "validations",
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
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("params", postgresql.JSONB(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "outcome",
            sa.Enum(*_OUTCOMES, name="validation_outcome", native_enum=False, length=16),
            nullable=False,
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("details", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_validations_incident_id", "validations", ["incident_id"])
    # The exclusion list is "which fingerprints failed validation for this
    # incident" - served by this composite without a separate table.
    op.create_index("ix_validations_incident_outcome", "validations", ["incident_id", "outcome"])


def downgrade() -> None:
    op.drop_index("ix_validations_incident_outcome", table_name="validations")
    op.drop_index("ix_validations_incident_id", table_name="validations")
    op.drop_table("validations")
