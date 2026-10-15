"""Services in Postgres: add, edit and remove what the Services page manages.

Rules break with a `ServiceError` (a `TeamError`) whose message is plain
English for the page to show as it is. Everyone named on a service must be on
the team (the people table); a pending invite does not count. Order is kept
as submitted: reviewers (the first one who is not the author reviews) and
path rules (CODEOWNERS style, the last match wins).

Functions flush but never commit; the caller owns the transaction
(state/db.session_scope).
"""

import re

from sqlalchemy.orm import Session

from state.models import Person, Service
from state.team import TeamError

# Lowercase letters, digits and hyphens, starting with a letter: safe as a
# Kubernetes name and as the key incidents refer to.
_NAME = re.compile(r"[a-z][a-z0-9-]{0,62}")
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_DNS_LABEL = re.compile(r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?")
_TEAM_MAX = 64


class ServiceError(TeamError):
    """A service rule was broken; the message is for the person using the page."""


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(v.strip() for v in values if v.strip()))


def _on_team(session: Session, login: str) -> None:
    if session.get(Person, login) is None:
        raise ServiceError(f"{login} is not on the team yet.")


def _clean(
    session: Session,
    name: str,
    *,
    team: str,
    repo: str,
    namespace: str,
    owners: list[str],
    default_owner: str,
    default_reviewers: list[str],
    depends_on: list[str],
    path_owners: list[dict],
) -> dict:
    """The validated column values for a service called `name`."""
    team, repo, namespace, default_owner = (
        team.strip(),
        repo.strip(),
        namespace.strip(),
        default_owner.strip(),
    )
    if len(team) > _TEAM_MAX:
        raise ServiceError(f"The team name can be at most {_TEAM_MAX} characters.")
    if repo and not _REPO.fullmatch(repo):
        raise ServiceError("The repository should look like owner/name, or be left empty.")
    if namespace and not _DNS_LABEL.fullmatch(namespace):
        raise ServiceError(
            "The namespace can use lowercase letters, digits and hyphens, up to 63 characters, "
            "and must start and end with a letter or digit. Leave it empty to use the service name."
        )
    owners = _unique(owners)
    if not owners:
        raise ServiceError(f"Add at least one approver, or nobody can approve changes to {name}.")
    for login in owners:
        _on_team(session, login)
    if default_owner:
        _on_team(session, default_owner)
        if default_owner not in owners:
            raise ServiceError("The default approver must be one of the approvers.")
    reviewers = [r.strip() for r in default_reviewers if r.strip()]
    for login in reviewers:
        _on_team(session, login)
    for login in dict.fromkeys(reviewers):
        if reviewers.count(login) > 1:
            raise ServiceError(f"{login} is listed as a reviewer twice.")
    rules = []
    for rule in path_owners:
        pattern = (rule.get("pattern") or "").strip()
        if not pattern:
            continue
        rule_owners = _unique(rule.get("owners") or [])
        if not rule_owners:
            raise ServiceError(f"The rule for {pattern} needs at least one approver.")
        for login in rule_owners:
            _on_team(session, login)
        rules.append({"pattern": pattern, "owners": rule_owners})
    return {
        "team": team,
        "repo": repo,
        "namespace": namespace or None,
        "default_owner": default_owner or None,
        "owners": owners,
        "default_reviewers": reviewers,
        "depends_on": _unique(depends_on),
        "path_owners": rules,
    }


def create_service(session: Session, name: str, **fields) -> Service:
    name = name.strip()
    if not _NAME.fullmatch(name):
        raise ServiceError(
            "The name can use lowercase letters, digits and hyphens, start with a letter "
            "and be up to 63 characters."
        )
    if session.get(Service, name) is not None:
        raise ServiceError(f"A service called {name} already exists.")
    row = Service(name=name, **_clean(session, name, **fields))
    session.add(row)
    session.flush()
    return row


def _existing(session: Session, name: str) -> Service:
    row = session.get(Service, name)
    if row is None:
        raise ServiceError(f"There is no service called {name}.")
    return row


def update_service(session: Session, name: str, new_name: str | None = None, **fields) -> Service:
    """Save the edit. Incidents refer to the name, so it cannot change:
    `new_name`, when given and different, is refused."""
    row = _existing(session, name)
    if new_name is not None and new_name.strip() != name:
        raise ServiceError("A service cannot be renamed because incidents refer to its name.")
    for column, value in _clean(session, name, **fields).items():
        setattr(row, column, value)
    session.flush()
    return row


def delete_service(session: Session, name: str) -> None:
    session.delete(_existing(session, name))
    session.flush()
