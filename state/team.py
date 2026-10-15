"""The team: people and services in Postgres.

For now this holds the one-time import of config/service_registry.yaml.
`import_registry` runs only into an empty team, so dashboard edits always win
and running it on every boot is harmless.

Functions flush but never commit; the caller owns the transaction
(state/db.session_scope).
"""

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agent.ops.registry import ServiceRegistry
from state.models import Person, Service


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
