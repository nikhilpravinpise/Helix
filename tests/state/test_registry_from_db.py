"""With HELIX_SERVICE_REGISTRY unset the service registry is the people and
services tables (agent/ops/registry.py); set, the file still wins, which is
how the benchmark and tests keep their fixed cast."""

import time
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from agent.ops.registry import REPO_ROOT, load_registry, resolve_owner
from events.ingest import ingest_event
from events.schemas import EngineeringEvent, EventSource, EventType
from helix import auth, demo
from helix.api import _slack_handle
from state import models
from state.deciders import approval_deciders, approval_refusal


@pytest.fixture
def team_db(tmp_path, monkeypatch):
    """A file SQLite database that load_registry() reads, with the registry
    file setting removed so the database is the source."""
    url = f"sqlite:///{tmp_path / 'team.db'}"
    engine = create_engine(url)
    models.Base.metadata.create_all(engine)
    monkeypatch.setenv("HELIX_DATABASE_URL", url)
    monkeypatch.delenv("HELIX_SERVICE_REGISTRY", raising=False)
    monkeypatch.delenv("HELIX_ALLOWED_USERS", raising=False)
    yield engine
    engine.dispose()


def _seed(engine) -> None:
    with Session(engine) as session:
        session.add_all(
            [
                models.Person(login="zed", name="Zed", slack_id="U111", role="admin"),
                models.Person(login="yan", name="Yan", role="member"),
                models.Service(
                    name="orders",
                    team="core",
                    repo="acme/orders",
                    default_owner="zed",
                    owners=["yan"],
                    default_reviewers=["yan"],
                    depends_on=["db"],
                    path_owners=[
                        {"pattern": "*", "owners": ["yan"]},
                        {"pattern": "k8s/*", "owners": ["zed"]},
                    ],
                    namespace="orders-ns",
                ),
            ]
        )
        session.commit()


@pytest.fixture
def incident_for(team_db):
    """Makes an incident on the team database; its session stays open so the
    incident can be read lazily, as the deciders do."""
    sessions: list[Session] = []

    def make(*, author: str, service: str = "orders") -> models.Incident:
        session = Session(team_db, expire_on_commit=False)
        sessions.append(session)
        result = ingest_event(
            session,
            EngineeringEvent(
                event_type=EventType.PR_OPENED,
                source=EventSource.GITHUB,
                service=service,
                environment="sandbox",
                actor=author,
                metadata={"pr": 9, "branch": "feature/x"},
            ),
        )
        session.commit()
        return session.query(models.Incident).filter_by(incident_id=result.incident_id).one()

    yield make
    for session in sessions:
        session.close()


def test_database_rows_become_the_registry(team_db):
    _seed(team_db)
    registry = load_registry()
    entry = registry.services["orders"]
    assert (entry.team, entry.repo, entry.default_owner) == ("core", "acme/orders", "zed")
    assert entry.owners == ["yan"] and entry.default_reviewers == ["yan"]
    assert entry.depends_on == ["db"] and entry.namespace == "orders-ns"
    assert registry.namespaces() == {"orders-ns"}
    assert registry.people["zed"].slack == "U111" and registry.people["zed"].name == "Zed"
    assert registry.people["yan"].slack is None
    assert (registry.people["zed"].role, registry.people["yan"].role) == ("admin", "member")


def test_the_database_roles_let_an_admin_author_self_approve_but_not_a_member(
    team_db, incident_for
):
    _seed(team_db)
    admin = incident_for(author="zed")  # zed: admin and default owner of orders
    assert approval_refusal(admin, "zed") is not None
    assert approval_refusal(admin, "zed", self_approve=True) is None
    member = incident_for(author="yan")  # yan: member, an approver, but a member
    assert approval_refusal(member, "yan", self_approve=True) is not None


def test_path_rules_keep_stored_order_last_match_wins(team_db):
    _seed(team_db)
    registry = load_registry()
    assert [r.pattern for r in registry.services["orders"].path_owners] == ["*", "k8s/*"]
    k8s = resolve_owner(registry, "orders", changed_paths=["k8s/deploy.yaml"])
    other = resolve_owner(registry, "orders", changed_paths=["src/app.py"])
    assert k8s == ("zed", "codeowners")
    assert other == ("yan", "codeowners")


def test_approval_deciders_come_from_the_database_minus_the_author(team_db, incident_for):
    _seed(team_db)
    assert approval_deciders(incident_for(author="dana")) == ["zed", "yan"]
    assert approval_deciders(incident_for(author="zed")) == ["yan"]


def test_allowed_users_are_the_people_and_the_override_still_wins(team_db, monkeypatch):
    _seed(team_db)
    assert auth.allowed_users() == {"zed", "yan"}
    monkeypatch.setenv("HELIX_ALLOWED_USERS", "yan")
    assert auth.allowed_users() == {"yan"}


def test_a_login_outside_the_people_table_is_not_signed_in(team_db, monkeypatch):
    _seed(team_db)
    monkeypatch.setenv("HELIX_SESSION_SECRET", "test-session-secret")

    def request(login: str):
        cookie = auth._sign({"login": login, "exp": time.time() + 60})
        return SimpleNamespace(cookies={auth.SESSION_COOKIE: cookie})

    assert auth.signed_in_user(request("zed")) == "zed"
    assert auth.signed_in_user(request("mallory")) is None


def test_slack_member_maps_to_the_database_login(team_db):
    _seed(team_db)
    assert _slack_handle({"id": "U111", "username": "z"}) == "zed"
    assert _slack_handle({"id": "U999", "username": "stranger"}) == "slack:U999"


def test_the_registry_file_setting_wins_over_database_rows(team_db, monkeypatch):
    _seed(team_db)
    monkeypatch.setenv(
        "HELIX_SERVICE_REGISTRY", str(REPO_ROOT / "benchmark" / "service_registry.yaml")
    )
    registry = load_registry()
    assert "orders" not in registry.services
    assert registry.services["payment-api"].default_owner == "alice"


def test_a_blank_registry_setting_means_the_database(team_db, monkeypatch):
    _seed(team_db)
    monkeypatch.setenv("HELIX_SERVICE_REGISTRY", "")
    assert "orders" in load_registry().services


def test_empty_tables_give_an_empty_registry_and_refusal_names_the_services_page(
    team_db, incident_for
):
    registry = load_registry()
    assert registry.services == {} and registry.people == {}
    assert auth.allowed_users() == set()
    reason = approval_refusal(incident_for(author="dana"), "zed")
    assert "Services page" in reason and "service_registry.yaml" not in reason


def test_a_database_error_is_not_an_empty_registry(tmp_path, monkeypatch):
    # No tables created: the read must fail loudly.
    monkeypatch.setenv("HELIX_DATABASE_URL", f"sqlite:///{tmp_path / 'bare.db'}")
    monkeypatch.delenv("HELIX_SERVICE_REGISTRY", raising=False)
    with pytest.raises(Exception, match="no such table"):
        load_registry()


def test_demo_subprocesses_read_the_database(monkeypatch):
    envs = []

    def helix(args, env):
        envs.append(env)
        raise RuntimeError("stop after the first run")

    monkeypatch.setattr(demo.harness, "_helix", helix)
    monkeypatch.setattr(
        demo.harness,
        "_ingest_declared_events",
        lambda scenario, url: ([{"incident_id": "I"}], None),
    )
    with pytest.raises(RuntimeError):
        demo._park_review("sqlite://", "VrajVed", 101)
    assert envs[0]["HELIX_SERVICE_REGISTRY"] == ""
