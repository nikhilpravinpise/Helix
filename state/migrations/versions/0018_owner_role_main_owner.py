"""Owner role and the main owner

Roles become owner > admin > member. `people.main_owner` marks the one owner
nobody else can remove or demote; a partial unique index allows at most one.
Today's admins become owners, and so do pending admin invites. The main
owner is VrajVed (the owner decided this) if present, else the earliest-created
owner, ties broken by login. Must stay in step with state/models.py.

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-15
"""

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

_OLD = "role IN ('admin', 'member')"
_NEW = "role IN ('owner', 'admin', 'member')"


def _swap_checks(check: str) -> None:
    with op.batch_alter_table("people") as batch:
        batch.drop_constraint("ck_people_role", type_="check")
        batch.create_check_constraint("ck_people_role", check)
    with op.batch_alter_table("invitations") as batch:
        batch.drop_constraint("ck_invitations_role", type_="check")
        batch.create_check_constraint("ck_invitations_role", check)


def upgrade() -> None:
    _swap_checks(_NEW)
    with op.batch_alter_table("people") as batch:
        batch.add_column(
            sa.Column("main_owner", sa.Boolean(), server_default=sa.false(), nullable=False)
        )
    op.create_index(
        "uq_people_main_owner",
        "people",
        ["main_owner"],
        unique=True,
        postgresql_where=sa.text("main_owner"),
        sqlite_where=sa.text("main_owner"),
    )
    conn = op.get_bind()
    conn.execute(sa.text("UPDATE people SET role = 'owner' WHERE role = 'admin'"))
    conn.execute(sa.text("UPDATE invitations SET role = 'owner' WHERE role = 'admin'"))
    main = conn.execute(
        sa.text("SELECT login FROM people WHERE login = 'VrajVed' AND role = 'owner'")
    ).scalar()
    if main is None:
        main = conn.execute(
            sa.text("SELECT login FROM people WHERE role = 'owner' ORDER BY created_at, login")
        ).scalar()
    if main is not None:
        conn.execute(
            sa.text("UPDATE people SET main_owner = :yes WHERE login = :login"),
            {"yes": True, "login": main},
        )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("UPDATE people SET role = 'admin' WHERE role = 'owner'"))
    conn.execute(sa.text("UPDATE invitations SET role = 'admin' WHERE role = 'owner'"))
    op.drop_index("uq_people_main_owner", table_name="people")
    with op.batch_alter_table("people") as batch:
        batch.drop_column("main_owner")
    _swap_checks(_OLD)
