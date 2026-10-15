"""Slack member id on invitations

The Slack member id Helix found for the invited email, copied to the person
when the invite is accepted. Must stay in step with state/models.py.

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-15
"""

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("invitations", sa.Column("slack_id", sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column("invitations", "slack_id")
