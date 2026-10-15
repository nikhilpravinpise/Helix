"""Connected accounts

One row per provider (`slack`, `jira`) holding the account the team
connected, the scopes granted, the access and refresh tokens encrypted at
rest (Fernet, state/connections.py) and a small JSON of provider settings
(Slack team/channel, Jira cloud id/site/project). Must stay in step with
state/models.py.

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-15
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "connections",
        sa.Column("provider", sa.String(length=16), primary_key=True),
        sa.Column("account", sa.String(length=256), nullable=False),
        sa.Column("scopes", postgresql.JSONB().with_variant(sa.JSON(), "sqlite"), nullable=False),
        sa.Column("access_token", sa.Text(), nullable=False),
        sa.Column("refresh_token", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("settings", postgresql.JSONB().with_variant(sa.JSON(), "sqlite"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_table("connections")
