"""The Services page (future_plans.md "Team and services"). A file SQLite
database so the page and load_registry() see the same rows; GitHub is faked."""

from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from agent.ops.registry import load_registry, resolve_owner, resolve_reviewer
from helix import auth, ui
from helix.api import app, get_db_session
from state import models
from state.db import session_scope


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
                models.Person(login="VrajVed", name="Vraj Ved", role="admin"),
                models.Person(login="nikhil", name="Nikhil P", role="member"),
                models.Person(login="kai", name="", role="member"),
            ]
        )
        s.commit()
    yield engine
    engine.dispose()


@pytest.fixture
def client(db, monkeypatch):
    def override():
        with session_scope(db) as session:
            yield session

    app.dependency_overrides[get_db_session] = override
    app.dependency_overrides[ui.get_db_session] = override
    monkeypatch.setenv("HELIX_SESSION_SECRET", "test-session-secret")
    monkeypatch.setenv("HELIX_PUBLIC_URL", "https://helix.example")
    with TestClient(app, base_url="https://helix.example") as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def sign_in_on(monkeypatch):
    monkeypatch.setenv("HELIX_GITHUB_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("HELIX_GITHUB_OAUTH_CLIENT_SECRET", "csecret")


def _sign_in(client, monkeypatch, login):
    monkeypatch.setattr(auth, "github_login", lambda code, uri: auth.GitHubUser(login, None))
    start = client.get("/auth/github", follow_redirects=False)
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    return client.get(f"/auth/github/callback?code=c&state={state}", follow_redirects=False)


def _form(**over):
    data = {
        "name": "orders",
        "team": "core",
        "repo": "acme/orders",
        "namespace": "",
        "owner": ["VrajVed", "nikhil"],
        "default_owner": "VrajVed",
        "reviewer": ["nikhil", "kai"],
        "depends_on": "db, queue\ncache",
        "rule_pattern": ["*", "k8s/*", ""],
        "rule_owners_0": ["nikhil"],
        "rule_owners_1": ["kai"],
    }
    data.update(over)
    return data


def _seed(db, **over):
    with Session(db) as s:
        s.add(
            models.Service(
                **{
                    "name": "orders",
                    "team": "core",
                    "repo": "acme/orders",
                    "default_owner": "VrajVed",
                    "owners": ["VrajVed", "nikhil"],
                    "default_reviewers": ["nikhil", "kai"],
                    "depends_on": ["db"],
                    "path_owners": [
                        {"pattern": "*", "owners": ["nikhil"]},
                        {"pattern": "k8s/*", "owners": ["kai"]},
                    ],
                    **over,
                }
            )
        )
        s.commit()


POSTS = ["/ui/services", "/ui/services/orders", "/ui/services/orders/delete"]


# ---- who may do what ----------------------------------------------------------


def test_a_member_gets_403_on_every_services_post(client, db, sign_in_on, monkeypatch):
    _seed(db)
    _sign_in(client, monkeypatch, "nikhil")
    for path in POSTS:
        r = client.post(path, data=_form(name="other"), follow_redirects=False)
        assert r.status_code == 403, path
        assert "Only admins" in r.text
    assert list(load_registry().services) == ["orders"]
    assert load_registry().services["orders"].team == "core"


def test_a_member_reads_the_list_but_is_sent_away_from_the_forms(
    client, db, sign_in_on, monkeypatch
):
    _seed(db)
    _sign_in(client, monkeypatch, "nikhil")
    page = client.get("/ui/services")
    assert page.status_code == 200
    assert "Only admins can change services." in page.text
    assert "Add a service" not in page.text and "/ui/services/orders/edit" not in page.text
    for path in ["/ui/services/new", "/ui/services/orders/edit"]:
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/ui/services"


def test_an_admin_can_create(client, db, sign_in_on, monkeypatch):
    _sign_in(client, monkeypatch, "VrajVed")
    r = client.post("/ui/services", data=_form(), follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/ui/services?notice=created")
    assert "orders" in load_registry().services


def test_sign_in_off_is_trusted(client, db):
    assert client.post("/ui/services", data=_form(), follow_redirects=False).status_code == 303
    assert client.get("/ui/services/new").status_code == 200


def test_a_user_from_the_allow_list_override_without_a_row_is_trusted(
    client, db, sign_in_on, monkeypatch
):
    monkeypatch.setenv("HELIX_ALLOWED_USERS", "operator")
    _sign_in(client, monkeypatch, "operator")
    assert client.post("/ui/services", data=_form(), follow_redirects=False).status_code == 303


# ---- saving -------------------------------------------------------------------


def test_create_saves_everything_in_order(client, db):
    client.post("/ui/services", data=_form())
    entry = load_registry().services["orders"]
    assert (entry.team, entry.repo, entry.namespace) == ("core", "acme/orders", None)
    assert entry.owners == ["VrajVed", "nikhil"] and entry.default_owner == "VrajVed"
    assert entry.default_reviewers == ["nikhil", "kai"]
    assert entry.depends_on == ["db", "queue", "cache"]
    assert [(r.pattern, r.owners) for r in entry.path_owners] == [
        ("*", ["nikhil"]),
        ("k8s/*", ["kai"]),
    ]
    registry = load_registry()
    assert resolve_owner(registry, "orders", changed_paths=["k8s/a.yaml"]) == ("kai", "codeowners")
    assert resolve_owner(registry, "orders", changed_paths=["a.py"]) == ("nikhil", "codeowners")
    assert resolve_reviewer(registry, "orders", author="nikhil")[0] == "kai"


def test_a_blank_rule_row_does_not_shift_the_owners_of_the_rows_after_it(client, db):
    client.post(
        "/ui/services",
        data=_form(rule_pattern=["", "k8s/*"], rule_owners_0=["nikhil"], rule_owners_1=["kai"]),
    )
    rules = load_registry().services["orders"].path_owners
    assert [(r.pattern, r.owners) for r in rules] == [("k8s/*", ["kai"])]


def test_a_failed_save_shows_the_message_and_keeps_what_was_typed(client, db):
    r = client.post(
        "/ui/services",
        data=_form(owner=[], default_owner="", team="Platform <b>", rule_pattern=["src/**"]),
    )
    assert r.status_code == 400
    assert "Add at least one owner, or nobody can approve changes to orders." in r.text
    assert 'value="orders"' in r.text and 'value="acme/orders"' in r.text
    assert "Platform &lt;b&gt;" in r.text and 'value="src/**"' in r.text
    assert "queue" in r.text  # the dependencies textarea
    assert load_registry().services == {}


def test_a_failed_save_keeps_the_checked_people_and_their_order(client, db):
    r = client.post("/ui/services", data=_form(name="Bad Name"))
    assert r.status_code == 400 and "The name can use lowercase letters" in r.text
    assert r.text.index('value="nikhil" selected') < r.text.index('value="kai" selected')
    assert 'name="owner" value="nikhil" checked' in r.text


def test_a_name_that_is_taken_is_refused(client, db):
    _seed(db)
    r = client.post("/ui/services", data=_form())
    assert r.status_code == 400 and "already exists" in r.text


def test_an_owner_who_left_the_team_is_still_shown_so_it_can_be_fixed(client, db):
    _seed(db, owners=["VrajVed", "ghost"])
    page = client.get("/ui/services/orders/edit")
    assert 'name="owner" value="ghost" checked' in page.text


def test_update_reorders_rules_and_reviewers(client, db):
    _seed(db)
    r = client.post(
        "/ui/services/orders",
        data=_form(
            reviewer=["kai", "nikhil"],
            rule_pattern=["k8s/*", "*"],
            rule_owners_0=["kai"],
            rule_owners_1=["nikhil"],
        ),
        follow_redirects=False,
    )
    assert r.status_code == 303 and "notice=updated" in r.headers["location"]
    registry = load_registry()
    assert resolve_owner(registry, "orders", changed_paths=["k8s/a.yaml"]) == (
        "nikhil",
        "codeowners",
    )
    assert resolve_reviewer(registry, "orders", author="zzz")[0] == "kai"


def test_the_name_cannot_be_changed_by_an_update(client, db):
    _seed(db)
    r = client.post("/ui/services/orders", data=_form(name="billing"))
    assert r.status_code == 400 and "cannot be renamed" in r.text
    assert list(load_registry().services) == ["orders"]


def test_updating_an_unknown_service_is_a_404(client, db):
    assert client.post("/ui/services/ghost", data=_form(name="ghost")).status_code == 404


def test_delete_removes_it(client, db):
    _seed(db)
    r = client.post("/ui/services/orders/delete", follow_redirects=False)
    assert r.status_code == 303 and "notice=removed" in r.headers["location"]
    assert load_registry().services == {}
    page = client.get(r.headers["location"])
    assert "Removed orders." in page.text


def test_deleting_an_unknown_service_shows_a_notice(client, db):
    r = client.post("/ui/services/ghost/delete", follow_redirects=True)
    assert "There is no service called ghost." in r.text


# ---- the pages ----------------------------------------------------------------


def test_the_list_shows_each_service(client, db):
    _seed(db)
    page = client.get("/ui/services").text
    assert "orders" in page and "core" in page
    assert 'href="https://github.com/acme/orders"' in page
    assert 'title="VrajVed"' in page and ">Vraj Ved<" in page
    assert "2 path rules" in page
    assert page.index("Nikhil P") < page.index(">kai<")  # reviewers in order, name falls back
    assert "Open incidents on orders will have nobody who can approve them." in page
    assert "/ui/services/orders/edit" in page and "/ui/services/new" in page


def test_the_list_has_an_empty_state_with_an_add_button(client, db):
    page = client.get("/ui/services").text
    assert "No services yet" in page and "Add a service" in page


def test_a_notice_is_shown_only_for_known_codes(client, db):
    assert "Added orders." in client.get("/ui/services?notice=created&service=orders").text
    assert "Added" not in client.get("/ui/services?notice=<script>").text


def test_the_form_has_ordered_rows_a_blank_rule_and_the_template(client, db):
    _seed(db)
    page = client.get("/ui/services/orders/edit").text
    assert "last match wins" in page and "Put broad rules first." in page
    assert page.count('name="rule_pattern"') == 4  # 2 rules + 1 blank + the template row
    assert "<template" in page
    assert "cannot be changed" in page and "readonly" in page
    assert 'name="rule_owners_1"' in page


def test_the_new_form_renders_for_an_admin(client, db):
    page = client.get("/ui/services/new")
    assert page.status_code == 200 and 'name="owner" value="VrajVed"' in page.text


def test_the_sidebar_links_to_services(client, db):
    for path in ["/ui/services", "/ui/team", "/ui/incidents"]:
        assert 'href="/ui/services"' in client.get(path).text
    assert 'href="/ui/services" aria-current="page"' in client.get("/ui/services").text
    assert 'href="/ui/services" aria-current="page"' not in client.get("/ui/team").text
