"""Migration 0020: invitations.slack_messaged_at."""

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text

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
    """A SQLite database migrated up to 0019 and nothing more."""
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        for name, file in [
            ("m15", "0015_people_services.py"),
            ("m16", "0016_invitations.py"),
            ("m17", "0017_invitation_slack_id.py"),
            ("m18", "0018_owner_role_main_owner.py"),
            ("m19", "0019_invitation_emailed_at.py"),
        ]:
            _run(connection, _load(name, file), "upgrade")
        yield connection


@pytest.fixture
def mig():
    return _load("m20", "0020_invitation_slack_messaged_at.py")


def _columns(conn):
    return {c["name"]: c for c in inspect(conn).get_columns("invitations")}


def test_upgrade_adds_a_nullable_column_and_keeps_rows(conn, mig):
    conn.execute(
        text(
            "INSERT INTO invitations (login, role, invited_by, token, created_at, expires_at,"
            " slack_id) VALUES ('kai', 'member', 'VrajVed', 't1', '2026-01-01', '2026-01-08',"
            " 'U1')"
        )
    )
    _run(conn, mig, "upgrade")
    cols = _columns(conn)
    assert "slack_messaged_at" in cols and cols["slack_messaged_at"]["nullable"]
    row = conn.execute(text("SELECT login, slack_id, slack_messaged_at FROM invitations")).one()
    assert tuple(row) == ("kai", "U1", None)


def test_downgrade_drops_the_column(conn, mig):
    _run(conn, mig, "upgrade")
    _run(conn, mig, "downgrade")
    assert "slack_messaged_at" not in _columns(conn)
