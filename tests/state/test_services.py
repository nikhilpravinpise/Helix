"""The Services rules (state/services.py): validation, and that what is
saved is what load_registry() resolves owners and reviewers from."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from agent.ops.registry import load_registry, resolve_owner, resolve_reviewer
from events.ingest import ingest_event
from events.schemas import EngineeringEvent, EventSource, EventType
from state import models, services, team
from state.deciders import approval_refusal
from state.services import ServiceError


@pytest.fixture
def db(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'svc.db'}"
    engine = create_engine(url)
    models.Base.metadata.create_all(engine)
    monkeypatch.setenv("HELIX_DATABASE_URL", url)
    monkeypatch.delenv("HELIX_SERVICE_REGISTRY", raising=False)
    monkeypatch.delenv("HELIX_ALLOWED_USERS", raising=False)
    with Session(engine) as s:
        s.add_all(
            [
                models.Person(login="zed", name="Zed", role="admin"),
                models.Person(login="yan", name="Yan", role="member"),
                models.Person(login="kai", name="Kai", role="member"),
            ]
        )
        s.commit()
    yield engine
    engine.dispose()


@pytest.fixture
def session(db):
    with Session(db) as s:
        yield s


def _fields(**over):
    base = dict(
        name="orders",
        team="core",
        repo="acme/orders",
        namespace="",
        owners=["zed", "yan"],
        default_owner="zed",
        default_reviewers=["yan", "kai"],
        depends_on=["db"],
        path_owners=[],
    )
    base.update(over)
    return base


def _create(session, **over):
    row = services.create_service(session, **_fields(**over))
    session.commit()
    return row


def _update_fields(**over):
    fields = _fields(**over)
    fields.pop("name")
    return fields


# ---- validation ---------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["", "Orders", "1orders", "-orders", "or ders", "or_ders", "a" * 64]
)
def test_a_bad_name_is_refused(session, name):
    with pytest.raises(ServiceError, match="name"):
        services.create_service(session, **_fields(name=name))


def test_a_63_character_name_is_fine(session):
    assert _create(session, name="a" * 63).name == "a" * 63


def test_a_duplicate_name_is_refused(session):
    _create(session)
    with pytest.raises(ServiceError, match="already exists"):
        services.create_service(session, **_fields())


def test_the_name_cannot_change_on_update(session):
    _create(session)
    with pytest.raises(ServiceError, match="cannot be renamed"):
        services.update_service(session, "orders", new_name="billing", **_update_fields())


def test_update_of_an_unknown_service_is_refused(session):
    with pytest.raises(ServiceError, match="no service"):
        services.update_service(session, "ghost", **_update_fields())


@pytest.mark.parametrize("repo", ["orders", "acme/", "/orders", "a/b/c", "acme/or ders"])
def test_a_bad_repo_is_refused(session, repo):
    with pytest.raises(ServiceError, match="repository"):
        services.create_service(session, **_fields(repo=repo))


def test_an_empty_repo_is_fine_and_a_github_repo_is_kept(session):
    assert _create(session, repo="").repo == ""
    assert _create(session, name="b", repo="acme/b-svc.v2").repo == "acme/b-svc.v2"


@pytest.mark.parametrize("namespace", ["Orders", "-ns", "ns-", "n_s", "a" * 64])
def test_a_bad_namespace_is_refused(session, namespace):
    with pytest.raises(ServiceError, match="namespace"):
        services.create_service(session, **_fields(namespace=namespace))


def test_an_empty_namespace_defaults_to_the_service_name(session):
    row = _create(session)
    assert row.namespace is None
    assert load_registry().namespaces() == {"orders"}
    assert _create(session, name="b", namespace="b-ns").namespace == "b-ns"


def test_a_long_team_is_refused(session):
    with pytest.raises(ServiceError, match="team"):
        services.create_service(session, **_fields(team="x" * 65))


def test_no_owners_is_refused(session):
    with pytest.raises(ServiceError) as err:
        services.create_service(session, **_fields(owners=[], default_owner=""))
    assert str(err.value) == "Add at least one approver, or nobody can approve changes to orders."


@pytest.mark.parametrize(
    "over",
    [
        dict(owners=["zed", "ghost"]),
        dict(default_reviewers=["ghost"]),
        dict(path_owners=[{"pattern": "*", "owners": ["ghost"]}]),
    ],
)
def test_someone_not_on_the_team_is_refused(session, over):
    with pytest.raises(ServiceError) as err:
        services.create_service(session, **_fields(**over))
    assert str(err.value) == "ghost is not on the team yet."


def test_a_pending_invitee_is_not_on_the_team(session):
    team.invite(session, "newbie", "member", "zed")
    session.commit()
    with pytest.raises(ServiceError, match="newbie is not on the team yet"):
        services.create_service(session, **_fields(owners=["zed", "newbie"]))


def test_the_default_owner_must_be_an_owner(session):
    with pytest.raises(ServiceError, match="default approver must be one of the approvers"):
        services.create_service(session, **_fields(default_owner="kai"))


def test_an_empty_default_owner_falls_back_to_the_first_owner(session):
    row = _create(session, default_owner="", owners=["yan", "zed"])
    assert row.default_owner is None
    assert resolve_owner(load_registry(), "orders") == ("yan", "registry")


def test_duplicate_reviewers_are_refused(session):
    with pytest.raises(ServiceError, match="yan is listed as a reviewer twice"):
        services.create_service(session, **_fields(default_reviewers=["yan", "kai", "yan"]))


def test_depends_on_is_trimmed_and_deduplicated_in_order(session):
    row = _create(session, depends_on=[" db ", "queue", "", "db", "cache"])
    assert row.depends_on == ["db", "queue", "cache"]


def test_empty_rule_patterns_are_dropped(session):
    row = _create(
        session,
        path_owners=[
            {"pattern": "  ", "owners": []},
            {"pattern": "", "owners": ["zed"]},
            {"pattern": " k8s/* ", "owners": ["zed"]},
        ],
    )
    assert row.path_owners == [{"pattern": "k8s/*", "owners": ["zed"]}]


def test_a_rule_without_owners_is_refused(session):
    with pytest.raises(ServiceError, match=r"The rule for k8s/\* needs at least one approver"):
        services.create_service(
            session, **_fields(path_owners=[{"pattern": "k8s/*", "owners": []}])
        )


def test_a_refused_save_leaves_nothing_behind(session):
    with pytest.raises(ServiceError):
        services.create_service(session, **_fields(owners=[]))
    session.commit()
    assert load_registry().services == {}


# ---- what the registry resolves ---------------------------------------------


RULES = [
    {"pattern": "*", "owners": ["yan"]},
    {"pattern": "k8s/*", "owners": ["kai"]},
]


def test_created_service_resolves_last_matching_rule_wins(session):
    _create(session, path_owners=RULES)
    registry = load_registry()
    assert registry.services["orders"].repo == "acme/orders"
    assert resolve_owner(registry, "orders", changed_paths=["k8s/a.yaml"]) == ("kai", "codeowners")
    assert resolve_owner(registry, "orders", changed_paths=["src/a.py"]) == ("yan", "codeowners")


def test_reviewer_follows_submitted_order_and_skips_the_author(session):
    _create(session, default_reviewers=["kai", "yan"])
    registry = load_registry()
    assert resolve_reviewer(registry, "orders", author="zed")[0] == "kai"
    assert resolve_reviewer(registry, "orders", author="kai")[0] == "yan"


def test_update_reorders_rules_and_reviewers_and_resolution_follows(session):
    _create(session, path_owners=RULES)
    services.update_service(
        session,
        "orders",
        **_update_fields(path_owners=list(reversed(RULES)), default_reviewers=["kai", "yan"]),
    )
    session.commit()
    registry = load_registry()
    # "*" is now last, so it wins even for k8s paths.
    assert resolve_owner(registry, "orders", changed_paths=["k8s/a.yaml"]) == ("yan", "codeowners")
    assert resolve_reviewer(registry, "orders", author="zed")[0] == "kai"


def test_delete_removes_it_and_approval_refusal_names_the_services_page(session, db):
    _create(session)
    services.delete_service(session, "orders")
    session.commit()
    assert "orders" not in load_registry().services
    incident = Session(db, expire_on_commit=False)
    result = ingest_event(
        incident,
        EngineeringEvent(
            event_type=EventType.PR_OPENED,
            source=EventSource.GITHUB,
            service="orders",
            environment="sandbox",
            actor="dana",
            metadata={"pr": 9, "branch": "feature/x"},
        ),
    )
    incident.commit()
    row = incident.query(models.Incident).filter_by(incident_id=result.incident_id).one()
    assert "Services page" in approval_refusal(row, "zed")
    incident.close()


def test_deleting_an_unknown_service_is_refused(session):
    with pytest.raises(ServiceError, match="no service"):
        services.delete_service(session, "ghost")


def test_a_person_can_be_removed_once_off_every_service(session):
    _create(session, path_owners=RULES)
    with pytest.raises(team.TeamError, match="still approves or reviews orders"):
        team.remove_person(session, "kai")
    services.update_service(
        session,
        "orders",
        **_update_fields(
            default_reviewers=["yan"], path_owners=[{"pattern": "*", "owners": ["yan"]}]
        ),
    )
    team.remove_person(session, "kai")
    session.commit()
    assert "kai" not in load_registry().people
