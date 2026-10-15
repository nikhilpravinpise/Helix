"""Migration 0013 creates the pr_bot_settings table in step with the model."""

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect

from state import models


def test_0013_matches_the_model_and_downgrades():
    path = Path(__file__).resolve().parents[2] / "state/migrations/versions/0013_pr_bot_settings.py"
    spec = importlib.util.spec_from_file_location("mig_0013", path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    assert (mig.revision, mig.down_revision) == ("0013", "0012")
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            mig.upgrade()
        cols = {c["name"] for c in inspect(conn).get_columns("pr_bot_settings")}
        assert cols == set(models.PRBotSettings.__table__.columns.keys())
        with Operations.context(MigrationContext.configure(conn)):
            mig.downgrade()
        assert "pr_bot_settings" not in inspect(conn).get_table_names()
