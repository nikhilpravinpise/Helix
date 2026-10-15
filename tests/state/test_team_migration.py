"""Migration 0015 creates the people and services tables in step with the models."""

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect

from state import models


def test_0015_matches_the_models_and_downgrades():
    path = Path(__file__).resolve().parents[2] / "state/migrations/versions/0015_people_services.py"
    spec = importlib.util.spec_from_file_location("mig_0015", path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    assert (mig.revision, mig.down_revision) == ("0015", "0014")
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            mig.upgrade()
        for model in (models.Person, models.Service):
            table = model.__table__.name
            cols = {c["name"] for c in inspect(conn).get_columns(table)}
            # main_owner arrives in 0018 (tests/state/test_roles_migration.py)
            extra = {"main_owner"} if model is models.Person else set()
            assert cols == set(model.__table__.columns.keys()) - extra
        with Operations.context(MigrationContext.configure(conn)):
            mig.downgrade()
        assert not {"people", "services"} & set(inspect(conn).get_table_names())
