"""Service registry and ownership resolution (subphase 2.1/2.5).

`load_registry()` builds the "provider-neutral... service registry
primitives" 2.1 calls for. It reads the people and services tables (the
team and Services pages edit them); when HELIX_SERVICE_REGISTRY names a
file it reads that instead, which is how the benchmark and tests keep
their fixed cast. Every
Phase 2 agent that needs to answer "who owns this" (Triage 2.5, Review
2.6) resolves through `resolve_owner()` rather than each inventing its own
lookup, so the three declared signal sources (code ownership, commit
history, the registry) are checked in one consistent order everywhere.

Resolution order, most specific first:

1. **Code ownership** - `path_owners` patterns matched against the files
   the change touches, git-CODEOWNERS semantics (rules checked in file
   order, the LAST match wins - a later, more specific pattern overrides
   an earlier catch-all). Sourced from the registry rather than a live
   GitHub file-content fetch: no CODEOWNERS file exists in this repository
   today, and adding a vendor capability to read one before a concrete
   need forces it would be exactly the speculative integration CLAUDE.md
   warns against. The registry's `path_owners` block is the same
   ownership signal, declared in the one place Helix already reads
   service metadata from.
2. **The service registry's default owner/owners** - `default_owner`, or
   the first name in `owners`.
3. **Commit history** - the author of the most recent commit evidence
   already collected (via the existing `GitHubEvidence.get_commit`
   capability - no new vendor surface), when neither of the above
   resolved.

`None` means genuinely unknown: 2.5's exit criterion is that Triage
escalates rather than guesses, and a caller getting `None` back is exactly
that signal.
"""

from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

REPO_ROOT = Path(__file__).resolve().parents[2]


class PathOwnerRule(BaseModel):
    """One code-ownership rule: a glob `pattern` and the owners it names."""

    pattern: str
    owners: list[str] = Field(default_factory=list)


class ServiceEntry(BaseModel):
    """One service's registry record."""

    team: str = ""
    repo: str = ""
    default_owner: str | None = None
    owners: list[str] = Field(default_factory=list)
    default_reviewers: list[str] = Field(default_factory=list)
    path_owners: list[PathOwnerRule] = Field(default_factory=list)
    # What this service depends on - databases, queues, other services.
    # The graph projection turns each name into a DEPENDS_ON edge so the
    # Knowledge Agent's neighborhood answer (graph.queries.
    # services_depended_on_by) is real rather than permanently empty.
    depends_on: list[str] = Field(default_factory=list)
    #: The Kubernetes namespace the service runs in; defaults to the
    #: service's own name. Helix writes to the cluster unasked only in a
    #: registered namespace (agent/policy.py).
    namespace: str | None = None


class Person(BaseModel):
    """How to show and reach one person. Keyed in the registry by the handle
    owners and reviewers use - a GitHub login, so self-review exclusion can
    compare it with a PR's author directly."""

    name: str = ""
    #: Slack member id (`U...`) to @-mention; None shows the name unpinged.
    slack: str | None = None


@dataclass(frozen=True)
class ServiceRegistry:
    """The loaded registry: service name -> `ServiceEntry`, plus people."""

    services: dict[str, ServiceEntry] = field(default_factory=dict)
    people: dict[str, Person] = field(default_factory=dict)

    def get(self, service: str) -> ServiceEntry | None:
        return self.services.get(service)

    def person(self, handle: str) -> Person | None:
        return self.people.get(handle)

    def namespaces(self) -> set[str]:
        """Every namespace a registered service runs in."""
        return {entry.namespace or name for name, entry in self.services.items()}


def _configured_path() -> Path | None:
    """HELIX_SERVICE_REGISTRY, resolved from the repo root when relative;
    None when unset, which means the database."""
    from helix.config import get_settings

    if not get_settings().service_registry:
        return None
    configured = Path(get_settings().service_registry)
    return configured if configured.is_absolute() else REPO_ROOT / configured


def _registry_from_database() -> ServiceRegistry:
    """The registry the people and services tables hold. Read only; a
    database error propagates rather than becoming an empty registry, which
    would silently refuse every approval."""
    from sqlalchemy import select

    from state.db import session_scope
    from state.models import Person as PersonRow
    from state.models import Service as ServiceRow

    with session_scope() as session:
        services = {
            row.name: ServiceEntry(
                team=row.team,
                repo=row.repo,
                default_owner=row.default_owner,
                owners=list(row.owners or []),
                default_reviewers=list(row.default_reviewers or []),
                path_owners=[PathOwnerRule.model_validate(r) for r in row.path_owners or []],
                depends_on=list(row.depends_on or []),
                namespace=row.namespace,
            )
            for row in session.scalars(select(ServiceRow))
        }
        people = {
            row.login: Person(name=row.name, slack=row.slack_id)
            for row in session.scalars(select(PersonRow))
        }
    return ServiceRegistry(services=services, people=people)


def load_registry(path: Path | None = None) -> ServiceRegistry:
    """Load and validate the registry: `path` if given, else the file named
    by HELIX_SERVICE_REGISTRY, else the people and services tables.

    A missing file is not an error - it means no services are registered
    yet, and every resolution falls through to "unknown" honestly rather
    than the loader inventing a default."""
    target = path or _configured_path()
    if target is None:
        return _registry_from_database()
    if not target.is_file():
        return ServiceRegistry(services={})
    raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    services = {
        name: ServiceEntry.model_validate(entry or {})
        for name, entry in (raw.get("services") or {}).items()
    }
    people = {
        handle: Person.model_validate(entry or {})
        for handle, entry in (raw.get("people") or {}).items()
    }
    return ServiceRegistry(services=services, people=people)


def _code_owners(entry: ServiceEntry, changed_paths: list[str]) -> list[str]:
    """CODEOWNERS-style resolution: for each changed path, the LAST
    pattern in file order that matches wins; the returned set is the
    union of owners across all changed paths (a change touching two
    differently-owned areas is owned by both)."""
    if not changed_paths or not entry.path_owners:
        return []
    owners: list[str] = []
    for changed in changed_paths:
        matched: list[str] | None = None
        for rule in entry.path_owners:
            if fnmatch(changed, rule.pattern):
                matched = rule.owners
        if matched:
            owners.extend(matched)
    seen: set[str] = set()
    ordered: list[str] = []
    for name in owners:
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def resolve_owner(
    registry: ServiceRegistry,
    service: str,
    *,
    changed_paths: list[str] | None = None,
    commit_author: str | None = None,
) -> tuple[str | None, str]:
    """Resolve an owner for `service`. Returns `(owner, source)` where
    `source` is one of `codeowners` | `registry` | `commit_history` |
    `none` - the provenance a `TriageResult` row records so a later
    graph-backed comparison (2.7) can tell which path answered.

    Ownership genuinely unknown returns `(None, "none")` - the caller
    (Triage) escalates rather than guessing, per 2.5's exit criterion.
    """
    entry = registry.get(service)
    if entry is not None:
        owners = _code_owners(entry, changed_paths or [])
        if owners:
            return owners[0], "codeowners"
        if entry.default_owner:
            return entry.default_owner, "registry"
        if entry.owners:
            return entry.owners[0], "registry"
    if commit_author:
        return commit_author, "commit_history"
    return None, "none"


def resolve_reviewer(
    registry: ServiceRegistry,
    service: str,
    *,
    author: str | None,
    exclude: "set[str] | None" = None,
) -> tuple[str | None, str]:
    """Resolve a reviewer for a change to `service` (subphase 2.6).

    Picks the first candidate from the service's `default_reviewers` list
    that is not the change's own author and not in `exclude` (already
    declined/unavailable) - "self-review is never proposed" is enforced
    here structurally, not left to the caller to remember. Returns
    `(reviewer, rationale)`; `(None, rationale)` when every candidate is
    excluded, which the Review Agent turns into an escalation.
    """
    entry = registry.get(service)
    if entry is None or not entry.default_reviewers:
        return None, f"no reviewers registered for service {service!r}"
    excluded = set(exclude or set())
    if author:
        excluded.add(author)
    for candidate in entry.default_reviewers:
        if candidate not in excluded:
            return (
                candidate,
                f"next available reviewer for {service} from the registry's "
                f"default_reviewers list (author {author!r} excluded)",
            )
    return None, f"every registered reviewer for {service} is excluded (author or declined)"
