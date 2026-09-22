"""Service registry + ownership resolution (subphase 2.1/2.5)."""

from pathlib import Path

import pytest

from agent.ops.registry import (
    ServiceEntry,
    ServiceRegistry,
    load_registry,
    resolve_owner,
    resolve_reviewer,
)


def test_load_registry_reads_the_committed_config() -> None:
    registry = load_registry()
    entry = registry.get("payment-api")
    assert entry is not None
    assert entry.team == "payments"
    assert "alice" in entry.owners


def test_load_registry_missing_file_is_empty_not_an_error(tmp_path: Path) -> None:
    registry = load_registry(tmp_path / "does-not-exist.yaml")
    assert registry.services == {}
    assert registry.get("payment-api") is None


def _entry(**kwargs) -> ServiceEntry:
    return ServiceEntry.model_validate(kwargs)


def test_resolve_owner_codeowners_wins_over_default() -> None:
    registry = ServiceRegistry(
        services={
            "payment-api": _entry(
                default_owner="bob",
                owners=["bob"],
                path_owners=[
                    {"pattern": "sandbox/payment-api/*", "owners": ["carol"]},
                    {"pattern": "sandbox/payment-api/k8s/03-secret.yaml", "owners": ["alice"]},
                ],
            )
        }
    )
    owner, source = resolve_owner(
        registry, "payment-api", changed_paths=["sandbox/payment-api/k8s/03-secret.yaml"]
    )
    assert owner == "alice"
    assert source == "codeowners"


def test_resolve_owner_last_matching_pattern_wins() -> None:
    """git CODEOWNERS semantics: later, more specific rules override
    earlier catch-alls - not "first match", "last match"."""
    registry = ServiceRegistry(
        services={
            "payment-api": _entry(
                path_owners=[
                    {"pattern": "sandbox/payment-api/k8s/*", "owners": ["alice"]},
                    {"pattern": "sandbox/payment-api/*", "owners": ["bob"]},
                ]
            )
        }
    )
    owner, source = resolve_owner(
        registry, "payment-api", changed_paths=["sandbox/payment-api/k8s/04-deployment.yaml"]
    )
    # Both patterns match this path; the LAST one in file order wins.
    assert owner == "bob"
    assert source == "codeowners"


def test_resolve_owner_falls_back_to_registry_default() -> None:
    registry = ServiceRegistry(services={"payment-api": _entry(default_owner="alice")})
    owner, source = resolve_owner(registry, "payment-api", changed_paths=["some/other/path.py"])
    assert (owner, source) == ("alice", "registry")


def test_resolve_owner_falls_back_to_registry_owners_list() -> None:
    registry = ServiceRegistry(services={"payment-api": _entry(owners=["dana"])})
    owner, source = resolve_owner(registry, "payment-api")
    assert (owner, source) == ("dana", "registry")


def test_resolve_owner_falls_back_to_commit_history_when_service_unregistered() -> None:
    registry = ServiceRegistry(services={})
    owner, source = resolve_owner(registry, "unknown-service", commit_author="erin")
    assert (owner, source) == ("erin", "commit_history")


def test_resolve_owner_none_when_nothing_resolves() -> None:
    registry = ServiceRegistry(services={})
    owner, source = resolve_owner(registry, "unknown-service")
    assert (owner, source) == (None, "none")


def test_resolve_reviewer_never_proposes_self_review() -> None:
    registry = ServiceRegistry(services={"payment-api": _entry(default_reviewers=["bob", "carol"])})
    reviewer, rationale = resolve_reviewer(registry, "payment-api", author="bob")
    assert reviewer == "carol"
    assert "excluded" in rationale


def test_resolve_reviewer_escalates_when_every_candidate_is_excluded() -> None:
    registry = ServiceRegistry(services={"payment-api": _entry(default_reviewers=["bob", "carol"])})
    reviewer, rationale = resolve_reviewer(registry, "payment-api", author="bob", exclude={"carol"})
    assert reviewer is None
    assert "excluded" in rationale


def test_resolve_reviewer_none_when_service_has_no_reviewers() -> None:
    registry = ServiceRegistry(services={})
    reviewer, rationale = resolve_reviewer(registry, "unknown-service", author="bob")
    assert reviewer is None
    assert "no reviewers registered" in rationale


@pytest.mark.parametrize("changed_paths", [None, []])
def test_resolve_owner_handles_no_changed_paths(changed_paths) -> None:
    registry = ServiceRegistry(services={"payment-api": _entry(default_owner="alice")})
    owner, source = resolve_owner(registry, "payment-api", changed_paths=changed_paths)
    assert (owner, source) == ("alice", "registry")
