"""Team invitations

An invite to join the team, like a GitHub collaborator invite: pending until
it is accepted by signing in, cancelled, or expired (7 days). Must stay in
step with state/models.py.

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-15
"""

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "invitations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("login", sa.String(length=128), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("email", sa.String(), nullable=True),
        sa.Column("invited_by", sa.String(length=128), nullable=False),
        sa.Column("token", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("role IN ('admin', 'member')", name="ck_invitations_role"),
        sa.UniqueConstraint("token", name="uq_invitations_token"),
    )


def downgrade() -> None:
    op.drop_table("invitations")
