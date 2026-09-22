"""Phase 2 change-review lifecycle and integration state

Subphase 2.1: `Incident.kind` selects which lifecycle an incident follows
(`recovery` - Phase 1 unchanged; `change` - the new CONTEXT.md section 14
review chain), plus the provider-neutral `owner`/`priority`/
`duplicate_of_id` columns. Subphases 2.3/2.4/2.5/2.6 add their own state
tables: `triage_results`, `review_requests`, `work_items` +
`work_item_comments`, `knowledge_pages`, and `integration_outbox` - the
durable send-queue every Slack/Jira/Notion side effect goes through, on
the same outbox principle `event_records.published_at` already uses for
Kafka. Must stay in step with state/models.py.

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-01
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from state.models import ApprovalStatus, IncidentKind, OutboxStatus

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

_INCIDENT_KINDS = [k.value for k in IncidentKind]
_APPROVAL_STATUSES = [s.value for s in ApprovalStatus]
_OUTBOX_STATUSES = [s.value for s in OutboxStatus]


def upgrade() -> None:
    op.add_column(
        "incidents",
        sa.Column(
            "kind",
            sa.Enum(*_INCIDENT_KINDS, name="incident_kind", native_enum=False, length=16),
            nullable=False,
            server_default=IncidentKind.RECOVERY.value,
        ),
    )
    op.add_column("incidents", sa.Column("owner", sa.String(length=128), nullable=True))
    op.add_column("incidents", sa.Column("priority", sa.String(length=16), nullable=True))
    op.add_column(
        "incidents",
        sa.Column(
            "duplicate_of_id",
            sa.Integer(),
            sa.ForeignKey("incidents.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )

    op.create_table(
        "triage_results",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "incident_id",
            sa.Integer(),
            sa.ForeignKey("incidents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("affected_service", sa.String(length=128), nullable=False),
        sa.Column("blast_radius", sa.String(length=16), nullable=False),
        sa.Column(
            "duplicate_of_id",
            sa.Integer(),
            sa.ForeignKey("incidents.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("owner", sa.String(length=128), nullable=True),
        sa.Column("owner_source", sa.String(length=16), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_triage_results_incident_id", "triage_results", ["incident_id"])

    op.create_table(
        "review_requests",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "incident_id",
            sa.Integer(),
            sa.ForeignKey("incidents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reviewer", sa.String(length=128), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(*_APPROVAL_STATUSES, name="approval_status", native_enum=False, length=16),
            nullable=False,
        ),
        sa.Column("decided_by", sa.String(length=128), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_review_requests_incident_id", "review_requests", ["incident_id"])
    op.create_index(
        "ux_review_requests_one_pending",
        "review_requests",
        ["incident_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )

    op.create_table(
        "work_items",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "incident_id",
            sa.Integer(),
            sa.ForeignKey("incidents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("external_id", sa.String(length=64), nullable=False),
        sa.Column("external_key", sa.String(length=32), nullable=True),
        sa.Column("external_url", sa.String(length=512), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("assignee", sa.String(length=128), nullable=True),
        sa.Column("priority", sa.String(length=16), nullable=True),
        sa.Column("project", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_work_items_incident_id", "work_items", ["incident_id"])
    op.create_index(
        "ux_work_items_one_per_provider", "work_items", ["incident_id", "provider"], unique=True
    )

    op.create_table(
        "work_item_comments",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "work_item_id",
            sa.Integer(),
            sa.ForeignKey("work_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("author", sa.String(length=128), nullable=False, server_default="helix"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_work_item_comments_work_item_id", "work_item_comments", ["work_item_id"])

    op.create_table(
        "knowledge_pages",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "incident_id",
            sa.Integer(),
            sa.ForeignKey("incidents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(length=16), nullable=False, server_default="notion"),
        sa.Column("external_id", sa.String(length=64), nullable=False),
        sa.Column("external_url", sa.String(length=512), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_knowledge_pages_incident_id", "knowledge_pages", ["incident_id"])
    op.create_index(
        "ux_knowledge_pages_one_per_incident", "knowledge_pages", ["incident_id"], unique=True
    )

    op.create_table(
        "integration_outbox",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "incident_id",
            sa.Integer(),
            sa.ForeignKey("incidents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("integration", sa.String(length=16), nullable=False),
        sa.Column("op", sa.String(length=32), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(*_OUTBOX_STATUSES, name="outbox_status", native_enum=False, length=16),
            nullable=False,
            server_default=OutboxStatus.PENDING.value,
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_integration_outbox_incident_id", "integration_outbox", ["incident_id"])
    op.create_index(
        "ix_integration_outbox_status_integration", "integration_outbox", ["status", "integration"]
    )


def downgrade() -> None:
    op.drop_index("ix_integration_outbox_status_integration", table_name="integration_outbox")
    op.drop_index("ix_integration_outbox_incident_id", table_name="integration_outbox")
    op.drop_table("integration_outbox")

    op.drop_index("ux_knowledge_pages_one_per_incident", table_name="knowledge_pages")
    op.drop_index("ix_knowledge_pages_incident_id", table_name="knowledge_pages")
    op.drop_table("knowledge_pages")

    op.drop_index("ix_work_item_comments_work_item_id", table_name="work_item_comments")
    op.drop_table("work_item_comments")

    op.drop_index("ux_work_items_one_per_provider", table_name="work_items")
    op.drop_index("ix_work_items_incident_id", table_name="work_items")
    op.drop_table("work_items")

    op.drop_index("ux_review_requests_one_pending", table_name="review_requests")
    op.drop_index("ix_review_requests_incident_id", table_name="review_requests")
    op.drop_table("review_requests")

    op.drop_index("ix_triage_results_incident_id", table_name="triage_results")
    op.drop_table("triage_results")

    op.drop_column("incidents", "duplicate_of_id")
    op.drop_column("incidents", "priority")
    op.drop_column("incidents", "owner")
    op.drop_column("incidents", "kind")
