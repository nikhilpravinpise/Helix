"""event_records.published_at

Subphase E5. Helix emits its own lifecycle events by writing an
`event_records` row inside the same transaction as the state change they
describe, which makes the history store a transactional outbox: the event
and the state change commit or roll back together and can never disagree.

`published_at` is what the outbox drain (`events.emit`) marks once a row
has reached Kafka. NULL means unpublished. Without it the drain would have
to republish the whole history every run - harmless, since every event id
is stable and consumers deduplicate on it, but unbounded work.

Nullable with no default, so every row already in the table is treated as
unpublished: the events consumed in E4 were never produced by Helix, and
claiming otherwise would be a lie in the data.

Must stay in step with state/models.py.

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-09
"""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "event_records", sa.Column("published_at", sa.DateTime(timezone=True), nullable=True)
    )
    # The drain scans for unpublished rows, so that is the query worth an
    # index rather than the column as a whole.
    op.create_index(
        "ix_event_records_unpublished",
        "event_records",
        ["published_at"],
        postgresql_where=sa.text("published_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_event_records_unpublished", table_name="event_records")
    op.drop_column("event_records", "published_at")
