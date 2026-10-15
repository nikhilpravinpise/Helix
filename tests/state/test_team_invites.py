"""Invites, roles and removal rules (state/team.py). Plain-English errors are
the contract: the Team page shows TeamError messages as they are."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from state import team
from state.models import Invitation, Person, Service
from state.team import TeamError


@pytest.fixture
def session(sqlite_engine):
    with Session(sqlite_engine, expire_on_commit=False) as s:
        s.add_all(
            [
                Person(login="VrajVed", name="Vraj", role="owner", main_owner=True),
                Person(login="nikhil", name="Nikhil", role="member"),
            ]
        )
        s.flush()
        yield s


def _invite(session, login="newbie", **kw):
    return team.invite(session, login, kw.pop("role", "member"), "VrajVed", **kw)


@pytest.mark.parametrize(
    "bad", ["", "-lead", "trail-", "dou--ble", "has space", "under_score", "a" * 40, "n@me"]
)
def test_invite_refuses_a_bad_github_login(session, bad):
    with pytest.raises(TeamError, match="GitHub login"):
        _invite(session, bad)


def test_invite_accepts_edge_valid_logins(session):
    _invite(session, "a")
    _invite(session, "a-b-c1")
    _invite(session, "x" * 39)


def test_invite_refuses_a_bad_role(session):
    with pytest.raises(TeamError, match="admin or a member"):
        _invite(session, role="root")


def test_invite_creates_a_pending_invite_that_expires_in_seven_days(session):
    inv = _invite(session, "Newbie", email="n@example.com")
    assert (inv.login, inv.role, inv.email, inv.invited_by) == (
        "Newbie",
        "member",
        "n@example.com",
        "VrajVed",
    )
    assert len(inv.token) >= 32
    assert inv.expires_at - inv.created_at == timedelta(days=7)
    assert team.pending_invites(session) == [inv]


def test_invite_refuses_someone_already_on_the_team_any_case(session):
    with pytest.raises(TeamError, match="already on the team"):
        _invite(session, "NIKHIL")


def test_invite_refuses_a_second_pending_invite_any_case(session):
    _invite(session, "Newbie")
    with pytest.raises(TeamError, match="already has a pending invite"):
        _invite(session, "newbie")


def test_an_expired_invite_does_not_block_a_new_one(session):
    inv = _invite(session, "newbie")
    inv.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    session.flush()
    assert _invite(session, "newbie").id != inv.id


def test_pending_boundary(session):
    inv = _invite(session)
    now = datetime.now(UTC)
    inv.expires_at = now + timedelta(seconds=5)
    assert team.is_pending(inv, now)
    inv.expires_at = now
    assert not team.is_pending(inv, now)
    inv.expires_at = now + timedelta(days=1)
    inv.cancelled_at = now
    assert not team.is_pending(inv, now)
    inv.cancelled_at = None
    inv.accepted_at = now
    assert not team.is_pending(inv, now)


def test_resend_gives_a_new_token_and_expiry_even_when_expired(session):
    inv = _invite(session)
    old = inv.token
    inv.expires_at = datetime.now(UTC) - timedelta(days=2)
    session.flush()
    team.resend(session, inv.id)
    assert inv.token != old
    assert team.is_pending(inv)


def test_resend_refuses_accepted_or_cancelled(session):
    inv = _invite(session)
    team.cancel(session, inv.id)
    with pytest.raises(TeamError, match="cancelled"):
        team.resend(session, inv.id)
    with pytest.raises(TeamError, match="cancelled"):
        team.cancel(session, inv.id)


def test_cancel_removes_it_from_pending_and_unknown_id_is_refused(session):
    inv = _invite(session)
    team.cancel(session, inv.id)
    assert team.pending_invites(session) == []
    assert inv.cancelled_at is not None
    with pytest.raises(TeamError, match="no longer exists"):
        team.cancel(session, 9999)
    # a cancelled invite frees the login for a new one
    _invite(session)


def test_accept_on_sign_in_creates_the_person_case_insensitively(session):
    inv = _invite(session, "Newbie", role="admin", email="n@example.com")
    person = team.accept_on_sign_in(session, "newbie", "New Bie")
    assert (person.login, person.name, person.role, person.email) == (
        "newbie",
        "New Bie",
        "admin",
        "n@example.com",
    )
    assert inv.accepted_at is not None
    assert team.pending_invites(session) == []


def test_accept_uses_the_login_when_github_has_no_name(session):
    _invite(session, "newbie")
    assert team.accept_on_sign_in(session, "newbie", None).name == "newbie"
    session.rollback()


def test_accept_ignores_expired_cancelled_and_unknown(session):
    inv = _invite(session, "newbie")
    inv.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert team.accept_on_sign_in(session, "newbie", "N") is None
    assert team.accept_on_sign_in(session, "stranger", "S") is None
    team.resend(session, inv.id)
    team.cancel(session, inv.id)
    assert team.accept_on_sign_in(session, "newbie", "N") is None
    assert session.get(Person, "newbie") is None


def test_record_sign_in_sets_last_signed_in(session):
    team.record_sign_in(session, "nikhil")
    assert session.get(Person, "nikhil").last_signed_in_at is not None
    team.record_sign_in(session, "not-on-the-team")  # no row: nothing happens


def test_set_role_and_rename(session):
    team.set_role(session, "nikhil", "admin")
    assert session.get(Person, "nikhil").role == "admin"
    team.rename(session, "nikhil", "  Nikhil P  ")
    assert session.get(Person, "nikhil").name == "Nikhil P"
    with pytest.raises(TeamError, match="name"):
        team.rename(session, "nikhil", "   ")
    with pytest.raises(TeamError, match="admin or a member"):
        team.set_role(session, "nikhil", "root")
    with pytest.raises(TeamError, match="not on the team"):
        team.set_role(session, "ghost", "admin")


def test_the_main_owner_cannot_be_demoted_or_removed_even_by_themselves(session):
    me = team.acting(session, "VrajVed")
    for action in (
        lambda: team.set_role(session, "VrajVed", "member", actor=me),
        lambda: team.remove_person(session, "VrajVed", actor=me),
        lambda: team.set_role(session, "VrajVed", "member"),
        lambda: team.remove_person(session, "VrajVed"),
    ):
        with pytest.raises(TeamError, match="main owner cannot be removed or demoted"):
            action()
    team.set_role(session, "nikhil", "owner")
    with pytest.raises(TeamError, match="main owner cannot be removed or demoted"):
        team.remove_person(session, "VrajVed", actor=team.acting(session, "nikhil"))


def test_is_admin(session):
    assert team.is_admin(session, "VrajVed")
    assert not team.is_admin(session, "nikhil")
    assert not team.is_admin(session, "ghost")


@pytest.mark.parametrize(
    "fields",
    [
        {"owners": ["nikhil"]},
        {"default_owner": "nikhil"},
        {"default_reviewers": ["nikhil"]},
        {"path_owners": [{"pattern": "*", "owners": ["nikhil"]}]},
    ],
)
def test_remove_is_refused_while_a_service_uses_the_person(session, fields):
    session.add(Service(name="payment-api", **fields))
    session.flush()
    with pytest.raises(TeamError) as err:
        team.remove_person(session, "nikhil")
    assert str(err.value) == (
        "Nikhil still approves or reviews payment-api. Change that on the Services page first."
    )
    assert session.get(Person, "nikhil") is not None


def test_remove_names_every_service_and_works_once_clear(session):
    session.add_all([Service(name="b-svc", owners=["nikhil"]), Service(name="a-svc", owners=[])])
    session.add(Service(name="c-svc", default_reviewers=["nikhil"]))
    session.flush()
    with pytest.raises(TeamError, match="b-svc and c-svc"):
        team.remove_person(session, "nikhil")
    for svc in session.scalars(select(Service)):
        svc.owners, svc.default_reviewers = [], []
    session.flush()
    team.remove_person(session, "nikhil")
    assert session.get(Person, "nikhil") is None
    with pytest.raises(TeamError, match="not on the team"):
        team.remove_person(session, "nikhil")


def test_invitation_rows_are_stored(session):
    _invite(session)
    assert session.scalar(select(Invitation).where(Invitation.login == "newbie"))
