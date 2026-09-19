"""event records + correlation tokens

Subphase E4 storage:

- `event_records` - one row per canonical event consumed from the stream,
  with a UNIQUE index on `event_id`. Since E3 derives event ids from the
  vendor payload's natural key, a redelivered webhook carries the same id,
  and this index is what makes replay a no-op at the database level rather
  than by convention. `envelope` keeps the event verbatim so E5's history
  and replay have the whole thing, not just the fields E4 reads.
- `correlation_tokens` - (token, incident) pairs, indexed by token. An
  arriving event extracts its tokens and looks them up here to find an
  incident an earlier event from a different system already opened. This
  is what closes the same-source-only dedup limitation carried since 1.4.

Must stay in step with state/models.py.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-08
"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "event_records",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("incident_id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("service", sa.String(length=128), nullable=False),
        sa.Column("environment", sa.String(length=32), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("envelope", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["incident_id"], ["incidents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_event_records_incident_id", "event_records", ["incident_id"])
    op.create_index("ux_event_records_event_id", "event_records", ["event_id"], unique=True)

    op.create_table(
        "correlation_tokens",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("incident_id", sa.Integer(), nullable=False),
        sa.Column("token", sa.String(length=256), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["incident_id"], ["incidents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_correlation_tokens_incident_id", "correlation_tokens", ["incident_id"])
    op.create_index("ix_correlation_tokens_token", "correlation_tokens", ["token"])
    op.create_index(
        "ux_correlation_tokens_pair", "correlation_tokens", ["token", "incident_id"], unique=True
    )


def downgrade() -> None:
    op.drop_table("correlation_tokens")
    op.drop_table("event_records")
