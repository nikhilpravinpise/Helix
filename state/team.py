"""The team: people, invites and services in Postgres.

`import_registry` is the one-time import of config/service_registry.yaml. It
runs only into an empty team, so dashboard edits always win and running it
on every boot is harmless.

Invites work like GitHub collaborator invites: pending for 7 days, then
expired (an admin can resend), accepted by signing in with the invited
GitHub login. Rules break with a `TeamError` whose message is plain English
for the Team page to show as it is.

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
ROLES = ("admin", "member")
# GitHub logins: letters, digits and single hyphens, 1-39 characters, no
# leading or trailing hyphen.
_GITHUB_LOGIN = re.compile(r"(?!-)(?!.*--)[A-Za-z0-9-]{1,39}(?<!-)")
_LAST_ADMIN = "Helix needs at least one admin."


class TeamError(Exception):
    """A team rule was broken; the message is for the person using the page."""


@dataclass(frozen=True)
class ImportResult:
    imported_services: int = 0
    imported_people: int = 0
    #: Why nothing was imported; None when the import ran.
    skipped_reason: str | None = None


def import_registry(session: Session, registry: ServiceRegistry) -> ImportResult:
    """Copy the registry's services and people into the tables, only if both
    are empty. Every imported person is an admin: they are the people who
    could edit the config file."""
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
    for login, person in registry.people.items():
        session.add(Person(login=login, name=person.name, slack_id=person.slack, role="admin"))
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
        raise TeamError("Choose whether they are an admin or a member.")


def _admin_count(session: Session) -> int:
    return (
        session.scalar(select(func.count()).select_from(Person).where(Person.role == "admin")) or 0
    )


def invite(
    session: Session, login: str, role: str, invited_by: str, email: str | None = None
) -> Invitation:
    login = login.strip()
    if not _GITHUB_LOGIN.fullmatch(login):
        raise TeamError(
            "That is not a valid GitHub login. Use letters, numbers and single hyphens, "
            "up to 39 characters."
        )
    _check_role(role)
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


def resend(session: Session, invite_id: int) -> Invitation:
    """A new link and a fresh 7 days; works on an expired invite too."""
    row = _open_invite(session, invite_id)
    now = datetime.now(UTC)
    row.token = secrets.token_urlsafe(32)
    row.created_at = now
    row.expires_at = now + timedelta(days=INVITE_DAYS)
    session.flush()
    return row


def cancel(session: Session, invite_id: int) -> None:
    _open_invite(session, invite_id).cancelled_at = datetime.now(UTC)
    session.flush()


def accept_on_sign_in(session: Session, login: str, name: str | None) -> Person | None:
    """Sign in with a GitHub login that has a pending invite (any case):
    create the person with the invite's role and email, and mark it accepted.
    None when there is no pending invite for the login."""
    lowered = login.lower()
    row = next((r for r in pending_invites(session) if r.login.lower() == lowered), None)
    if row is None:
        return None
    row.accepted_at = datetime.now(UTC)
    person = session.get(Person, login) or Person(
        login=login, name=(name or "").strip() or login, email=row.email, role=row.role
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
    person = session.get(Person, login)
    return person is not None and person.role == "admin"


def set_role(session: Session, login: str, role: str) -> None:
    _check_role(role)
    person = _person(session, login)
    if person.role == "admin" and role != "admin" and _admin_count(session) <= 1:
        raise TeamError(_LAST_ADMIN)
    person.role = role
    session.flush()


def rename(session: Session, login: str, name: str) -> None:
    person = _person(session, login)
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


def remove_person(session: Session, login: str) -> None:
    person = _person(session, login)
    if person.role == "admin" and _admin_count(session) <= 1:
        raise TeamError(_LAST_ADMIN)
    using = _services_using(session, login)
    if using:
        raise TeamError(
            f"{person.name or login} still owns or reviews {_and_list(using)}. "
            "Change that on the Services page first."
        )
    session.delete(person)
    session.flush()
