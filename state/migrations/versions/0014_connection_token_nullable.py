"""Connections without a token

A GitHub App installation holds no long-lived credential: Helix mints a
short-lived token from the app's key when it needs one, so the connection row
carries only the installation id (in `settings`). `access_token` becomes
nullable for that. Slack and Jira rows still always store a token. Must stay
in step with state/models.py.

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-15
"""

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("connections", "access_token", existing_type=sa.Text(), nullable=True)


def downgrade() -> None:
    # A tokenless row cannot satisfy NOT NULL; it is only a GitHub App
    # installation, which can be installed again.
    op.execute("DELETE FROM connections WHERE access_token IS NULL")
    op.alter_column("connections", "access_token", existing_type=sa.Text(), nullable=False)
