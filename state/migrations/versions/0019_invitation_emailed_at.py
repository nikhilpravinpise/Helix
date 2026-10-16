"""Invitation emailed_at

When Helix last emailed the invite link (integrations/email.py); NULL when it
never did or the send failed. Must stay in step with state/models.py.

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-16
"""

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("invitations") as batch:
        batch.add_column(sa.Column("emailed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("invitations") as batch:
        batch.drop_column("emailed_at")
