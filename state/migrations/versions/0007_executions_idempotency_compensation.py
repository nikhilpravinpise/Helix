"""executions idempotency + compensation linkage

Subphase 1.10 columns on `executions`:

- `idempotency_key` - sha256-free opaque key derived by the write path as
  `{incident_pk}:{fingerprint}:{attempt}:{kind}` where kind is
  `remediation` or `comp:{failed execution id}`. The unique index
  `ux_executions_idempotency` makes "the same logical write is recorded
  once" a database guarantee: a retried or resumed run re-creating the
  row hits the index instead of double-writing. The runner/node lookup
  treats an existing key as "already attempted" and replays the recorded
  outcome rather than re-firing the transport.
- `compensates_id` - self-FK marking a row as a COMPENSATION for the
  referenced (verification-failed) execution. NULL on ordinary
  remediation rows. This is how provenance and the outstanding-debt
  check tell a compensating write apart from a new remediation attempt,
  and how "the debt is paid" is derived: a successful executions row
  whose compensates_id points at the failed row.

Must stay in step with state/models.py.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-01
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("executions", sa.Column("idempotency_key", sa.String(length=128), nullable=True))
    op.add_column(
        "executions",
        sa.Column(
            "compensates_id",
            sa.Integer(),
            sa.ForeignKey("executions.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    # Backfill: pre-1.10 rows predate keying; the key is derivable for
    # remediation rows so the index covers them too (NULLs would exempt
    # them from uniqueness - every real row must carry a key). The suffix
    # is written as ':' || 'remediation' because alembic's op.execute
    # parses a bare ':word' as a bind parameter.
    op.execute(
        "UPDATE executions SET idempotency_key = "
        "incident_id || ':' || fingerprint || ':' || attempt || ':' || 'remediation' "
        "WHERE idempotency_key IS NULL"
    )
    op.alter_column("executions", "idempotency_key", nullable=False)
    op.create_index(
        "ux_executions_idempotency",
        "executions",
        ["idempotency_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ux_executions_idempotency", table_name="executions")
    op.drop_column("executions", "compensates_id")
    op.drop_column("executions", "idempotency_key")
