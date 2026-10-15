"""Pull request bot settings

A single row (id 1) holding what a developer configured for the bot that
opens fix PRs: real PRs on/off, repo, base branch, draft, daily limit. Every
column is nullable - NULL means "use the server default". Must stay in step
with state/models.py.

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-15
"""

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pr_bot_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("real_prs", sa.Boolean(), nullable=True),
        sa.Column("repo", sa.String(length=256), nullable=True),
        sa.Column("base_branch", sa.String(length=256), nullable=True),
        sa.Column("draft", sa.Boolean(), nullable=True),
        sa.Column("daily_limit", sa.Integer(), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_table("pr_bot_settings")
