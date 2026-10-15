"""Migration 0018: owner role and the main owner (future_plans.md "Roles:
owners, admins, members")."""

import importlib.util
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from state import models

VERSIONS = Path(__file__).resolve().parents[2] / "state/migrations/versions"


def _load(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, VERSIONS / file)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    return mig


def _run(conn, mig, step: str):
    with Operations.context(MigrationContext.configure(conn)):
        getattr(mig, step)()


@pytest.fixture
def conn():
    """A SQLite database migrated up to 0017 and nothing more."""
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        for name, file in [
            ("m15", "0015_people_services.py"),
            ("m16", "0016_invitations.py"),
            ("m17", "0017_invitation_slack_id.py"),
        ]:
            _run(connection, _load(name, file), "upgrade")
        yield connection


@pytest.fixture
def mig():
    return _load("m18", "0018_owner_role_main_owner.py")


def _person(conn, login, role, created):
    conn.execute(
        text("INSERT INTO people (login, name, role, created_at) VALUES (:l, :l, :r, :c)"),
        {"l": login, "r": role, "c": datetime(2026, 1, created, tzinfo=UTC).isoformat()},
    )


def _invite(conn, login, role, token):
    conn.execute(
        text(
            "INSERT INTO invitations (login, role, invited_by, token, created_at, expires_at)"
            " VALUES (:l, :r, 'x', :t, :n, :n)"
        ),
        {"l": login, "r": role, "t": token, "n": datetime(2026, 1, 1, tzinfo=UTC).isoformat()},
    )


def _roles(conn):
    return dict(conn.execute(text("SELECT login, role FROM people")).all())


def _mains(conn):
    return [r[0] for r in conn.execute(text("SELECT login FROM people WHERE main_owner = 1"))]


def test_revision_chain_and_columns_match_the_model(conn, mig):
    assert (mig.revision, mig.down_revision) == ("0018", "0017")
    _run(conn, mig, "upgrade")
    for model in (models.Person, models.Invitation):
        cols = {c["name"] for c in inspect(conn).get_columns(model.__table__.name)}
        assert cols == set(model.__table__.columns.keys())


def test_admins_become_owners_and_vrajved_is_the_main_owner(conn, mig):
    _person(conn, "nikhil", "admin", 1)
    _person(conn, "VrajVed", "admin", 5)
    _person(conn, "kai", "member", 2)
    _invite(conn, "pending-admin", "admin", "t1")
    _invite(conn, "pending-member", "member", "t2")
    _run(conn, mig, "upgrade")
    assert _roles(conn) == {"nikhil": "owner", "VrajVed": "owner", "kai": "member"}
    assert _mains(conn) == ["VrajVed"]
    invites = dict(conn.execute(text("SELECT login, role FROM invitations")).all())
    assert invites == {"pending-admin": "owner", "pending-member": "member"}


def test_without_vrajved_the_earliest_owner_is_main_and_ties_go_by_login(conn, mig):
    _person(conn, "zed", "admin", 3)
    _person(conn, "bob", "admin", 3)
    _person(conn, "late", "admin", 9)
    _person(conn, "kai", "member", 1)
    _run(conn, mig, "upgrade")
    assert _mains(conn) == ["bob"]


def test_a_team_with_no_admin_gets_no_main_owner(conn, mig):
    _person(conn, "kai", "member", 1)
    _run(conn, mig, "upgrade")
    assert _mains(conn) == []
    assert _roles(conn) == {"kai": "member"}


def test_the_new_roles_are_allowed_and_junk_still_is_not(conn, mig):
    _run(conn, mig, "upgrade")
    _person(conn, "o", "owner", 1)
    _invite(conn, "i", "owner", "t")
    with pytest.raises(IntegrityError):
        _person(conn, "bad", "root", 1)


def test_at_most_one_main_owner(conn, mig):
    _person(conn, "a", "admin", 1)
    _person(conn, "b", "admin", 2)
    _run(conn, mig, "upgrade")
    with pytest.raises(IntegrityError):
        conn.execute(text("UPDATE people SET main_owner = 1"))


def test_downgrade_maps_owner_back_to_admin_and_drops_the_column(conn, mig):
    _person(conn, "VrajVed", "admin", 1)
    _person(conn, "kai", "member", 2)
    _invite(conn, "pending", "admin", "t1")
    _run(conn, mig, "upgrade")
    _run(conn, mig, "downgrade")
    assert _roles(conn) == {"VrajVed": "admin", "kai": "member"}
    assert "main_owner" not in {c["name"] for c in inspect(conn).get_columns("people")}
    assert conn.execute(text("SELECT role FROM invitations")).scalar() == "admin"
    with pytest.raises(IntegrityError):
        _person(conn, "o", "owner", 1)
