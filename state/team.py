"""The team: people, invites and services in Postgres.

`import_registry` is the one-time import of config/service_registry.yaml. It
runs only into an empty team, so dashboard edits always win and running it
on every boot is harmless.

Invites work like GitHub collaborator invites: pending for 7 days, then
expired (an admin can resend), accepted by signing in with the invited
GitHub login. Rules break with a `TeamError` whose message is plain English
for the Team page to show as it is.

Roles rank owner (3) > admin (2) > member (1); one owner is the main owner.
Owners act on anyone except the main owner, admins only on members, members
on no one (but anyone may rename themselves). Every change takes the acting
person as `actor`: None is sign-in turned off, trusted with full owner
power. The main owner cannot be removed, demoted or renamed by anyone else.

Functions flush but never commit; the caller owns the transaction
(state/db.session_scope).
"""

import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agent.ops.registry import ServiceRegistry
from state.models import Invitation, Person, Service

INVITE_DAYS = 7
ROLES = ("owner", "admin", "member")
_RANK = {"owner": 3, "admin": 2, "member": 1}
# GitHub logins: letters, digits and single hyphens, 1-39 characters, no
# leading or trailing hyphen.
_GITHUB_LOGIN = re.compile(r"(?!-)(?!.*--)[A-Za-z0-9-]{1,39}(?<!-)")


class TeamError(Exception):
    """A team rule was broken; the message is for the person using the page."""


@dataclass(frozen=True)
class Actor:
    """Who is making a change: their login and role."""

    login: str
    role: str

    @property
    def rank(self) -> int:
        return _RANK[self.role]


@dataclass(frozen=True)
class ImportResult:
    imported_services: int = 0
    imported_people: int = 0
    #: Why nothing was imported; None when the import ran.
    skipped_reason: str | None = None


def import_registry(session: Session, registry: ServiceRegistry) -> ImportResult:
    """Copy the registry's services and people into the tables, only if both
    are empty. Every imported person is an owner: they are the people who
    could edit the config file. The first person listed is the main owner."""
    people = session.scalar(select(func.count()).select_from(Person)) or 0
    services = session.scalar(select(func.count()).select_from(Service)) or 0
    if people or services:
        return ImportResult(
            skipped_reason=f"the team already has {people} people and {services} services"
        )
    for name, entry in registry.services.items():
        session.add(
            Service(
                name=name,
                team=entry.team,
                repo=entry.repo,
                default_owner=entry.default_owner,
                owners=list(entry.owners),
                default_reviewers=list(entry.default_reviewers),
                depends_on=list(entry.depends_on),
                path_owners=[
                    {"pattern": rule.pattern, "owners": list(rule.owners)}
                    for rule in entry.path_owners
                ],
                namespace=entry.namespace,
            )
        )
    for index, (login, person) in enumerate(registry.people.items()):
        session.add(
            Person(
                login=login,
                name=person.name,
                slack_id=person.slack,
                role="owner",
                main_owner=index == 0,
            )
        )
    session.flush()
    return ImportResult(
        imported_services=len(registry.services), imported_people=len(registry.people)
    )


def _aware(value: datetime) -> datetime:
    """SQLite hands back naive datetimes; they are UTC."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def is_pending(invite: Invitation, now: datetime | None = None) -> bool:
    return (
        invite.accepted_at is None
        and invite.cancelled_at is None
        and _aware(invite.expires_at) > (now or datetime.now(UTC))
    )


def pending_invites(session: Session) -> list[Invitation]:
    now = datetime.now(UTC)
    rows = session.scalars(select(Invitation).order_by(Invitation.id)).all()
    return [row for row in rows if is_pending(row, now)]


def _person(session: Session, login: str) -> Person:
    person = session.get(Person, login)
    if person is None:
        raise TeamError(f"{login} is not on the team.")
    return person


def _check_role(role: str) -> None:
    if role not in ROLES:
        raise TeamError("Choose whether they are an owner, an admin or a member.")


def acting(session: Session, login: str) -> Actor:
    """The actor for someone on the team."""
    return Actor(login, _person(session, login).role)


def _rank(actor: Actor | None) -> int:
    return 3 if actor is None else actor.rank


def grantable_roles(actor: Actor | None) -> tuple[str, ...]:
    """The roles `actor` may give when inviting or changing a role."""
    return {3: ROLES, 2: ("member",)}.get(_rank(actor), ())


def may_act_on(actor: Actor | None, person: Person) -> bool:
    """Whether `actor` may change `person`'s role, remove them or rename
    them (renaming yourself is `may_rename`)."""
    if person.main_owner:
        return False
    if _rank(actor) == 3:
        return True
    return _rank(actor) == 2 and person.role == "member"


def may_rename(actor: Actor | None, person: Person) -> bool:
    return (actor is not None and actor.login == person.login) or may_act_on(actor, person)


def may_manage_invite(actor: Actor | None, invite: Invitation) -> bool:
    return _rank(actor) == 3 or (_rank(actor) == 2 and invite.role == "member")


def _require_act_on(actor: Actor | None, person: Person) -> None:
    if person.main_owner:
        raise TeamError(
            "The main owner cannot be removed or demoted. Make another owner the main owner first."
        )
    if not may_act_on(actor, person):
        raise TeamError(_cannot_change(actor))


def _cannot_change(actor: Actor | None) -> str:
    if _rank(actor) == 2:
        return "Admins can only change members."
    return "Members cannot change other people."


def _require_grant(actor: Actor | None, role: str) -> None:
    if role in grantable_roles(actor):
        return
    if _rank(actor) == 2:
        raise TeamError("Admins can only make people members.")
    raise TeamError("Members cannot invite people or change roles.")


def invite(
    session: Session,
    login: str,
    role: str,
    invited_by: str,
    email: str | None = None,
    slack_id: str | None = None,
    actor: Actor | None = None,
) -> Invitation:
    login = login.strip()
    if not _GITHUB_LOGIN.fullmatch(login):
        raise TeamError(
            "That is not a valid GitHub login. Use letters, numbers and single hyphens, "
            "up to 39 characters."
        )
    _check_role(role)
    _require_grant(actor, role)
    lowered = login.lower()
    if session.scalar(select(Person.login).where(func.lower(Person.login) == lowered)):
        raise TeamError(f"{login} is already on the team.")
    if any(row.login.lower() == lowered for row in pending_invites(session)):
        raise TeamError(f"{login} already has a pending invite. Resend or cancel it instead.")
    now = datetime.now(UTC)
    row = Invitation(
        login=login,
        role=role,
        email=(email or "").strip() or None,
        slack_id=slack_id,
        invited_by=invited_by,
        token=secrets.token_urlsafe(32),
        created_at=now,
        expires_at=now + timedelta(days=INVITE_DAYS),
    )
    session.add(row)
    session.flush()
    return row


def _open_invite(session: Session, invite_id: int) -> Invitation:
    row = session.get(Invitation, invite_id)
    if row is None:
        raise TeamError("That invite no longer exists.")
    if row.accepted_at is not None:
        raise TeamError("That invite was already accepted.")
    if row.cancelled_at is not None:
        raise TeamError("That invite was already cancelled.")
    return row


def _managed_invite(session: Session, invite_id: int, actor: Actor | None) -> Invitation:
    row = _open_invite(session, invite_id)
    if not may_manage_invite(actor, row):
        raise TeamError(
            "Admins can only change invites for members."
            if _rank(actor) == 2
            else "Members cannot change invites."
        )
    return row


def resend(session: Session, invite_id: int, actor: Actor | None = None) -> Invitation:
    """A new link and a fresh 7 days; works on an expired invite too."""
    row = _managed_invite(session, invite_id, actor)
    now = datetime.now(UTC)
    row.token = secrets.token_urlsafe(32)
    row.created_at = now
    row.expires_at = now + timedelta(days=INVITE_DAYS)
    session.flush()
    return row


def cancel(session: Session, invite_id: int, actor: Actor | None = None) -> None:
    _managed_invite(session, invite_id, actor).cancelled_at = datetime.now(UTC)
    session.flush()


def accept_on_sign_in(session: Session, login: str, name: str | None) -> Person | None:
    """Sign in with a GitHub login that has a pending invite (any case):
    create the person with the invite's role, email and Slack id, and mark it
    accepted.
    None when there is no pending invite for the login."""
    lowered = login.lower()
    row = next((r for r in pending_invites(session) if r.login.lower() == lowered), None)
    if row is None:
        return None
    row.accepted_at = datetime.now(UTC)
    person = session.get(Person, login) or Person(
        login=login,
        name=(name or "").strip() or login,
        email=row.email,
        slack_id=row.slack_id,
        role=row.role,
        main_owner=False,
    )
    session.add(person)
    session.flush()
    return person


def record_sign_in(session: Session, login: str) -> None:
    person = session.get(Person, login)
    if person is not None:
        person.last_signed_in_at = datetime.now(UTC)
        session.flush()


def is_admin(session: Session, login: str) -> bool:
    """Admin or above: may manage services, connections and members."""
    person = session.get(Person, login)
    return person is not None and _RANK[person.role] >= _RANK["admin"]


def set_role(session: Session, login: str, role: str, actor: Actor | None = None) -> None:
    _check_role(role)
    person = _person(session, login)
    _require_act_on(actor, person)
    _require_grant(actor, role)
    person.role = role
    session.flush()


def transfer_main_owner(session: Session, actor: Actor | None, login: str) -> None:
    """Hand main ownership to another owner; the old main owner stays an owner."""
    current = session.scalar(select(Person).where(Person.main_owner))
    if actor is None or current is None or actor.login != current.login:
        raise TeamError("Only the main owner can hand over main ownership.")
    person = _person(session, login)
    if person.login == current.login:
        raise TeamError(f"{person.name or login} is already the main owner.")
    if person.role != "owner":
        raise TeamError("Main ownership can only go to another owner. Make them an owner first.")
    current.main_owner = False
    session.flush()
    person.main_owner = True
    session.flush()


def link_slack(session: Session, login: str, slack_id: str, email: str | None = None) -> None:
    """Bind a Slack member id to a person, and fill their email if empty. One
    Slack account belongs to one person: an id already linked to someone else
    is refused."""
    person = _person(session, login)
    other = session.scalar(select(Person).where(Person.slack_id == slack_id, Person.login != login))
    if other is not None:
        raise TeamError(f"That Slack account is already linked to {other.name or other.login}.")
    person.slack_id = slack_id
    if email and not person.email:
        person.email = email
    session.flush()


def rename(session: Session, login: str, name: str, actor: Actor | None = None) -> None:
    person = _person(session, login)
    if not may_rename(actor, person):
        if person.main_owner:
            raise TeamError("The main owner can only be renamed by themselves.")
        raise TeamError(_cannot_change(actor))
    if not name.strip():
        raise TeamError("Enter a name.")
    person.name = name.strip()
    session.flush()


def _services_using(session: Session, login: str) -> list[str]:
    return sorted(
        svc.name
        for svc in session.scalars(select(Service))
        if login == svc.default_owner
        or login in (svc.owners or [])
        or login in (svc.default_reviewers or [])
        or any(login in (rule.get("owners") or []) for rule in svc.path_owners or [])
    )


def _and_list(names: list[str]) -> str:
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def remove_person(session: Session, login: str, actor: Actor | None = None) -> None:
    person = _person(session, login)
    _require_act_on(actor, person)
    using = _services_using(session, login)
    if using:
        raise TeamError(
            f"{person.name or login} still approves or reviews {_and_list(using)}. "
            "Change that on the Services page first."
        )
    session.delete(person)
    session.flush()
