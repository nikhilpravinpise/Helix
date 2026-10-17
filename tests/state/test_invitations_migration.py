"""Migration 0016 creates the invitations table in step with the model."""

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect

from state import models


def test_0016_matches_the_model_and_downgrades():
    path = Path(__file__).resolve().parents[2] / "state/migrations/versions/0016_invitations.py"
    spec = importlib.util.spec_from_file_location("mig_0016", path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    assert (mig.revision, mig.down_revision) == ("0016", "0015")
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            mig.upgrade()
        cols = {c["name"] for c in inspect(conn).get_columns("invitations")}
        # slack_id arrives in 0017 (tests/state/test_team_slack_ids.py)
        assert cols == set(models.Invitation.__table__.columns.keys()) - {
            "slack_id",
            "emailed_at",
            "slack_messaged_at",
        }
        with Operations.context(MigrationContext.configure(conn)):
            mig.downgrade()
        assert "invitations" not in inspect(conn).get_table_names()
