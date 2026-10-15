"""People and services

The team and its services move out of config/service_registry.yaml into
Postgres. `people` is keyed by GitHub login with an admin or member role;
`services` holds owners, reviewers, dependencies and the ordered path rules.
Must stay in step with state/models.py.

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-15
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def _json_doc() -> sa.types.TypeEngine:
    return postgresql.JSONB().with_variant(sa.JSON(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "people",
        sa.Column("login", sa.String(length=128), primary_key=True),
        sa.Column("name", sa.String(), server_default="", nullable=False),
        sa.Column("email", sa.String(), nullable=True),
        sa.Column("slack_id", sa.String(length=32), nullable=True),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("last_signed_in_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("role IN ('admin', 'member')", name="ck_people_role"),
    )
    op.create_table(
        "services",
        sa.Column("name", sa.String(length=128), primary_key=True),
        sa.Column("team", sa.String(), server_default="", nullable=False),
        sa.Column("repo", sa.String(), server_default="", nullable=False),
        sa.Column("default_owner", sa.String(), nullable=True),
        sa.Column("owners", _json_doc(), nullable=False),
        sa.Column("default_reviewers", _json_doc(), nullable=False),
        sa.Column("depends_on", _json_doc(), nullable=False),
        sa.Column("path_owners", _json_doc(), nullable=False),
        sa.Column("namespace", sa.String(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_table("services")
    op.drop_table("people")
