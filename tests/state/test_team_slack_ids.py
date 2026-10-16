"""Slack ids on invitations and people (state/team.py), and migration 0017."""

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session

from state import models, team
from state.team import TeamError


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add_all(
            [
                models.Person(login="VrajVed", name="Vraj", role="admin"),
                models.Person(login="nikhil", name="Nikhil", role="member", email="n@x.com"),
            ]
        )
        s.commit()
        yield s


def test_invite_stores_the_slack_id(session):
    row = team.invite(session, "newbie", "member", "VrajVed", "n@x.com", slack_id="U9")
    assert row.slack_id == "U9"
    assert team.invite(session, "other", "member", "VrajVed").slack_id is None


def test_accepting_copies_the_slack_id_to_the_person(session):
    team.invite(session, "newbie", "member", "VrajVed", "n@x.com", slack_id="U9")
    person = team.accept_on_sign_in(session, "newbie", "New")
    assert person.slack_id == "U9"


def test_accepting_without_a_slack_id_leaves_it_empty(session):
    team.invite(session, "newbie", "member", "VrajVed")
    assert team.accept_on_sign_in(session, "newbie", "New").slack_id is None


def test_link_slack_sets_the_id_and_fills_an_empty_email(session):
    team.link_slack(session, "VrajVed", "U1", "v@x.com")
    team.link_slack(session, "nikhil", "U2", "other@x.com")
    people = {p.login: p for p in session.query(models.Person)}
    assert (people["VrajVed"].slack_id, people["VrajVed"].email) == ("U1", "v@x.com")
    assert (people["nikhil"].slack_id, people["nikhil"].email) == ("U2", "n@x.com")


def test_link_slack_refuses_an_id_that_belongs_to_someone_else(session):
    team.link_slack(session, "VrajVed", "U1")
    with pytest.raises(TeamError, match="already linked to Vraj"):
        team.link_slack(session, "nikhil", "U1")
    team.link_slack(session, "VrajVed", "U1")  # the same person again is fine


def test_link_slack_refuses_someone_not_on_the_team(session):
    with pytest.raises(TeamError, match="not on the team"):
        team.link_slack(session, "ghost", "U1")


def _load(name: str, filename: str):
    path = Path(__file__).resolve().parents[2] / "state/migrations/versions" / filename
    spec = importlib.util.spec_from_file_location(name, path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    return mig


def test_0017_matches_the_model_and_downgrades():
    first = _load("mig_0016", "0016_invitations.py")
    mig = _load("mig_0017", "0017_invitation_slack_id.py")
    assert (mig.revision, mig.down_revision) == ("0017", "0016")
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            first.upgrade()
            mig.upgrade()
        cols = {c["name"] for c in inspect(conn).get_columns("invitations")}
        assert cols == set(models.Invitation.__table__.columns.keys()) - {"emailed_at"}
        with Operations.context(MigrationContext.configure(conn)):
            mig.downgrade()
        assert "slack_id" not in {c["name"] for c in inspect(conn).get_columns("invitations")}
