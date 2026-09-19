"""workflow_runs.stale_reason

Orphaned-run recovery. `run_incident` closes its `workflow_runs` row from
inside the running process (`finish_run`); a hard kill of that process
(SIGTERM, SIGKILL, an OOM kill, a CI/harness subprocess timeout) never
reaches `finish_run`, so the row stays `running` forever and the
single-active-run guard refuses every later `helix run` for that incident
with RunConflictError - there was no recovery path anywhere.

`stale_reason` is the recorded rule an operator can inspect after the
explicit `helix reclaim` command declares a `running` row stale (its
`started_at` older than HELIX_STALE_RUN_TIMEOUT) and marks it `failed`.
NULL means the row ended normally through `finish_run` or was never
reclaimed.

Must stay in step with state/models.py.

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-20
"""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("workflow_runs", sa.Column("stale_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("workflow_runs", "stale_reason")
