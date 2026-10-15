"""Encrypted store for connected accounts (future_plans.md Stage 1): tokens
are Fernet-encrypted at rest under a key derived from HELIX_CONNECTIONS_KEY,
and a missing or wrong key reads as "not connected" instead of crashing.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text

from state import connections, models

ACCESS = "xoxb-access-token-plaintext"
REFRESH = "refresh-token-plaintext"
EXPIRES = datetime(2030, 1, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv("HELIX_ENV_FILE", "")
    monkeypatch.setenv("HELIX_CONNECTIONS_KEY", "any random string Render generated")


def _save(session, provider="slack", **overrides):
    fields = {
        "account": "Acme workspace",
        "scopes": ["chat:write", "channels:read"],
        "access_token": ACCESS,
        "refresh_token": REFRESH,
        "expires_at": EXPIRES,
        "settings": {"team_id": "T1", "channel": "#helix"},
    }
    fields.update(overrides)
    connections.save_connection(session, provider, **fields)
    session.commit()


def test_round_trip_decrypts(db_session):
    _save(db_session)
    conn = connections.get_connection(db_session, "slack")
    assert conn is not None
    assert conn.provider == "slack"
    assert conn.account == "Acme workspace"
    assert conn.scopes == ["chat:write", "channels:read"]
    assert conn.access_token == ACCESS
    assert conn.refresh_token == REFRESH
    assert conn.expires_at is not None
    assert conn.settings == {"team_id": "T1", "channel": "#helix"}


def test_optional_fields_may_be_absent(db_session):
    _save(db_session, refresh_token=None, expires_at=None, settings={})
    conn = connections.get_connection(db_session, "slack")
    assert conn is not None
    assert conn.refresh_token is None
    assert conn.expires_at is None
    assert conn.settings == {}


def test_tokens_not_stored_in_plaintext(db_session):
    _save(db_session)
    row = db_session.execute(text("SELECT access_token, refresh_token FROM connections")).one()
    for stored in row:
        assert ACCESS not in stored and REFRESH not in stored
    dump = str(db_session.execute(text("SELECT * FROM connections")).all())
    assert ACCESS not in dump and REFRESH not in dump


def test_repr_hides_tokens(db_session):
    _save(db_session)
    conn = connections.get_connection(db_session, "slack")
    assert ACCESS not in repr(conn) and REFRESH not in repr(conn)


def test_wrong_key_reads_as_not_connected(db_session, monkeypatch):
    _save(db_session)
    monkeypatch.setenv("HELIX_CONNECTIONS_KEY", "a different secret")
    assert connections.get_connection(db_session, "slack") is None


def test_no_key_reads_as_not_connected(db_session, monkeypatch):
    _save(db_session)
    monkeypatch.delenv("HELIX_CONNECTIONS_KEY")
    assert connections.get_connection(db_session, "slack") is None


def test_no_key_save_raises(db_session, monkeypatch):
    monkeypatch.delenv("HELIX_CONNECTIONS_KEY")
    with pytest.raises(connections.ConnectionsKeyMissing, match="HELIX_CONNECTIONS_KEY"):
        _save(db_session)
    assert db_session.query(models.Connection).count() == 0


def test_unknown_provider_is_none(db_session):
    assert connections.get_connection(db_session, "jira") is None


def test_save_upserts_by_provider(db_session):
    _save(db_session)
    _save(db_session, account="Other workspace", access_token="new-token", settings={"a": 1})
    assert db_session.query(models.Connection).count() == 1
    conn = connections.get_connection(db_session, "slack")
    assert conn.account == "Other workspace"
    assert conn.access_token == "new-token"
    assert conn.settings == {"a": 1}


def test_providers_are_independent(db_session):
    _save(db_session, "slack")
    _save(db_session, "jira", account="acme.atlassian.net", access_token="jira-token")
    assert connections.get_connection(db_session, "jira").access_token == "jira-token"
    assert connections.get_connection(db_session, "slack").access_token == ACCESS


def test_delete_removes_row(db_session):
    _save(db_session)
    assert connections.delete_connection(db_session, "slack") is True
    db_session.commit()
    assert connections.get_connection(db_session, "slack") is None
    assert connections.delete_connection(db_session, "slack") is False


def test_update_tokens_keeps_settings(db_session):
    _save(db_session)
    later = datetime(2031, 1, 1, tzinfo=UTC)
    assert connections.update_tokens(db_session, "slack", "fresh-access", "fresh-refresh", later)
    db_session.commit()
    conn = connections.get_connection(db_session, "slack")
    assert conn.access_token == "fresh-access"
    assert conn.refresh_token == "fresh-refresh"
    assert conn.settings == {"team_id": "T1", "channel": "#helix"}
    assert conn.account == "Acme workspace"
    assert conn.scopes == ["chat:write", "channels:read"]


def test_update_tokens_keeps_refresh_when_not_rotated(db_session):
    _save(db_session)
    connections.update_tokens(db_session, "slack", "fresh-access", None, EXPIRES)
    db_session.commit()
    assert connections.get_connection(db_session, "slack").refresh_token == REFRESH


def test_update_tokens_on_missing_provider_is_false(db_session):
    assert connections.update_tokens(db_session, "slack", "x", None, None) is False


def test_migration_matches_model():
    """0012 creates exactly the columns the model declares."""
    import importlib.util

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, inspect

    path = Path(__file__).resolve().parents[2] / "state/migrations/versions/0012_connections.py"
    spec = importlib.util.spec_from_file_location("mig_0012", path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    assert (mig.revision, mig.down_revision) == ("0012", "0011")
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            mig.upgrade()
        cols = {c["name"] for c in inspect(conn).get_columns("connections")}
        assert cols == set(models.Connection.__table__.columns.keys())
        with Operations.context(MigrationContext.configure(conn)):
            mig.downgrade()
        assert "connections" not in inspect(conn).get_table_names()
