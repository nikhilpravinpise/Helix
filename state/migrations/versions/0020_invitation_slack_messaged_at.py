"""Invitation slack_messaged_at

When Helix last DMed the invite link in Slack; NULL when it never did or the
DM failed. A nullable column with no default, so it is safe on a live
database. Must stay in step with state/models.py.

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-17
"""

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("invitations") as batch:
        batch.add_column(sa.Column("slack_messaged_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("invitations") as batch:
        batch.drop_column("slack_messaged_at")
