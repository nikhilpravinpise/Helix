"""Owner > Admin > Member and the main owner (state/team.py; future_plans.md
"Roles: owners, admins, members"). Every rule is a TeamError in plain English."""

import pytest
from sqlalchemy.orm import Session

from agent.ops.registry import REPO_ROOT, load_registry
from state import team
from state.models import Invitation, Person
from state.team import TeamError

REAL_YAML = REPO_ROOT / "config" / "service_registry.yaml"
ROLES = ("owner", "admin", "member")


@pytest.fixture
def session(sqlite_engine):
    with Session(sqlite_engine, expire_on_commit=False) as s:
        s.add_all(
            [
                Person(login="main", name="Main", role="owner", main_owner=True),
                Person(login="co", name="Co", role="owner"),
                Person(login="adm", name="Adm", role="admin"),
                Person(login="mem", name="Mem", role="member"),
            ]
        )
        s.flush()
        yield s


def _actor(session, kind):
    """`off` is sign-in turned off: no actor, trusted with full owner power."""
    return None if kind == "off" else team.acting(session, kind)


ACTORS = ("main", "co", "off", "adm", "mem")
TARGETS = ("main", "co", "adm", "mem")


def _may_act(actor: str, target: str) -> bool:
    """Whether `actor` may change `target` at all (role, remove)."""
    if target == "main":
        return False
    if actor in ("main", "co", "off"):
        return True
    return actor == "adm" and target == "mem"


# ---- the permission matrix --------------------------------------------------


@pytest.mark.parametrize("new_role", ROLES)
@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("actor", ACTORS)
def test_set_role_matrix(session, actor, target, new_role):
    allowed = _may_act(actor, target) and (
        actor != "adm" or new_role == "member"  # admins grant member only
    )
    who = _actor(session, actor)
    if allowed:
        team.set_role(session, target, new_role, actor=who)
        assert session.get(Person, target).role == new_role
    else:
        before = session.get(Person, target).role
        with pytest.raises(TeamError):
            team.set_role(session, target, new_role, actor=who)
        assert session.get(Person, target).role == before


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("actor", ACTORS)
def test_remove_matrix(session, actor, target):
    who = _actor(session, actor)
    if _may_act(actor, target):
        team.remove_person(session, target, actor=who)
        assert session.get(Person, target) is None
    else:
        with pytest.raises(TeamError):
            team.remove_person(session, target, actor=who)
        assert session.get(Person, target) is not None


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("actor", ACTORS)
def test_rename_matrix(session, actor, target):
    # Anyone may rename themselves, the main owner included; otherwise the
    # same rule as every other change.
    allowed = actor == target or _may_act(actor, target)
    who = _actor(session, actor)
    if allowed:
        team.rename(session, target, "New Name", actor=who)
        assert session.get(Person, target).name == "New Name"
    else:
        with pytest.raises(TeamError):
            team.rename(session, target, "New Name", actor=who)
        assert session.get(Person, target).name != "New Name"


def test_messages_are_plain_english(session):
    with pytest.raises(TeamError, match="main owner cannot be removed or demoted"):
        team.remove_person(session, "main", actor=team.acting(session, "co"))
    with pytest.raises(TeamError, match="main owner can only be renamed by themselves"):
        team.rename(session, "main", "X", actor=None)
    with pytest.raises(TeamError, match="Admins can only change members"):
        team.remove_person(session, "co", actor=team.acting(session, "adm"))
    with pytest.raises(TeamError, match="Members cannot change"):
        team.set_role(session, "mem", "member", actor=team.acting(session, "mem"))
    with pytest.raises(TeamError, match="Admins can only make people members"):
        team.set_role(session, "mem", "admin", actor=team.acting(session, "adm"))


def test_a_co_owner_can_demote_or_remove_themselves_but_the_main_owner_cannot(session):
    team.set_role(session, "co", "member", actor=team.acting(session, "co"))
    assert session.get(Person, "co").role == "member"
    with pytest.raises(TeamError):
        team.set_role(session, "main", "admin", actor=team.acting(session, "main"))
    with pytest.raises(TeamError):
        team.remove_person(session, "main", actor=team.acting(session, "main"))


def test_there_is_always_an_owner(session):
    for login in ("co",):
        team.remove_person(session, login, actor=None)
    for login in ("adm", "mem"):
        team.set_role(session, login, "member", actor=None)
    owners = session.query(Person).filter(Person.role == "owner").all()
    assert [p.login for p in owners] == ["main"]


def test_removal_is_still_refused_while_the_person_is_on_a_service(session):
    from state.models import Service

    session.add(Service(name="payment-api", owners=["mem"]))
    session.flush()
    with pytest.raises(TeamError, match="still owns or reviews payment-api"):
        team.remove_person(session, "mem", actor=team.acting(session, "co"))


def test_the_acting_login_must_be_on_the_team(session):
    with pytest.raises(TeamError, match="not on the team"):
        team.acting(session, "ghost")


def test_is_admin_means_admin_or_above(session):
    assert [team.is_admin(session, x) for x in ("main", "co", "adm", "mem", "ghost")] == [
        True,
        True,
        True,
        False,
        False,
    ]


# ---- invites ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("actor", "granted"),
    [
        ("main", ROLES),
        ("co", ROLES),
        ("off", ROLES),
        ("adm", ("member",)),
        ("mem", ()),
    ],
)
def test_invite_role_limits(session, actor, granted):
    who = _actor(session, actor)
    assert team.grantable_roles(who) == granted
    for role in ROLES:
        if role in granted:
            row = team.invite(session, f"x-{role}", role, "co", actor=who)
            assert row.role == role
        else:
            with pytest.raises(TeamError):
                team.invite(session, f"x-{role}", role, "co", actor=who)


def test_invite_still_refuses_an_unknown_role(session):
    with pytest.raises(TeamError, match="an owner, an admin or a member"):
        team.invite(session, "someone", "root", "co", actor=None)


@pytest.mark.parametrize("invite_role", ROLES)
@pytest.mark.parametrize("actor", ACTORS)
def test_resend_and_cancel_limits(session, actor, invite_role):
    row = team.invite(session, "guest", invite_role, "co", actor=None)
    old_token = row.token
    who = _actor(session, actor)
    allowed = actor in ("main", "co", "off") or (actor == "adm" and invite_role == "member")
    if allowed:
        team.resend(session, row.id, actor=who)
        assert row.token != old_token
        team.cancel(session, row.id, actor=who)
        assert row.cancelled_at is not None
    else:
        with pytest.raises(TeamError):
            team.resend(session, row.id, actor=who)
        with pytest.raises(TeamError):
            team.cancel(session, row.id, actor=who)
        assert row.token == old_token and row.cancelled_at is None


def test_accepting_an_owner_invite_makes_a_co_owner_never_the_main_owner(session):
    team.invite(session, "newcomer", "owner", "main", actor=team.acting(session, "main"))
    person = team.accept_on_sign_in(session, "newcomer", "New")
    assert (person.role, person.main_owner) == ("owner", False)
    assert [p.login for p in session.query(Person).filter(Person.main_owner)] == ["main"]


# ---- handing over main ownership --------------------------------------------


def test_the_main_owner_hands_over_to_an_owner_and_stays_an_owner(session):
    team.transfer_main_owner(session, team.acting(session, "main"), "co")
    assert (session.get(Person, "co").main_owner, session.get(Person, "co").role) == (True, "owner")
    assert (session.get(Person, "main").main_owner, session.get(Person, "main").role) == (
        False,
        "owner",
    )
    # and the old main owner is now an ordinary co-owner
    team.set_role(session, "main", "member", actor=team.acting(session, "main"))


@pytest.mark.parametrize("actor", ["co", "adm", "mem", "off"])
def test_only_the_main_owner_can_hand_over(session, actor):
    with pytest.raises(TeamError, match="Only the main owner"):
        team.transfer_main_owner(session, _actor(session, actor), "co")
    assert session.get(Person, "main").main_owner


@pytest.mark.parametrize("target", ["adm", "mem", "ghost", "main"])
def test_main_ownership_goes_only_to_another_owner(session, target):
    with pytest.raises(TeamError):
        team.transfer_main_owner(session, team.acting(session, "main"), target)
    assert session.get(Person, "main").main_owner


# ---- importing the registry -------------------------------------------------


def test_import_makes_everyone_an_owner_and_the_first_listed_the_main_owner(sqlite_engine):
    registry = load_registry(REAL_YAML)
    first = next(iter(registry.people))
    assert first == "VrajVed"
    with Session(sqlite_engine) as s:
        team.import_registry(s, registry)
        s.commit()
        rows = {p.login: p for p in s.query(Person)}
        assert {p.role for p in rows.values()} == {"owner"}
        assert [login for login, p in rows.items() if p.main_owner] == [first]
        assert s.query(Invitation).count() == 0
