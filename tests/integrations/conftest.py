"""Shared helpers for evidence-layer tests. All unit-tier: no live
infrastructure - fixtures, stubbed transports and fake API objects only.

The tail of this file is the work-provider test rig (subphases 2.3/2.10):
each live WorkManagementProvider gets a stateful fake of its vendor API
behind httpx.MockTransport, so the contract suite exercises real provider
code - real request shapes, real status mapping - with no network. The
`provider` fixture parametrizes the conformance suite over all four
implementations, which is what "providers are interchangeable behind the
contract" looks like as a test.
"""

import functools
import itertools
import json
import re
from collections.abc import Callable, Iterator

import httpx
import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.pool import StaticPool

from integrations.evidence import Evidence
from integrations.fixtures import FIXTURES_ROOT, FixtureStore
from integrations.work import (
    JiraProvider,
    LinearProvider,
    MemoryWorkProvider,
    TrelloProvider,
    WorkManagementProvider,
)
from state import models
from state.db import session_scope

CLUSTER_FIXTURES = sorted(f"cluster/{p.stem}" for p in (FIXTURES_ROOT / "cluster").glob("*.json"))
CI_FIXTURES = sorted(f"ci/{p.stem}" for p in (FIXTURES_ROOT / "ci").glob("*.json"))


@pytest.fixture
def sqlite_engine() -> Iterator[Engine]:
    """Fresh in-memory SQLite per test (StaticPool shares one connection)."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def sqlite_sessions(sqlite_engine: Engine):
    """A session_scope-equivalent bound to the test SQLite engine."""
    return functools.partial(session_scope, sqlite_engine)


@pytest.fixture
def store() -> FixtureStore:
    return FixtureStore()


def load_fixture(store: FixtureStore, name: str) -> dict:
    return store.load(name)


def assert_json_safe(value):
    """Recursively assert a value contains only plain JSON types - the
    C8 boundary check: no vendor SDK objects may hide inside a payload."""
    if isinstance(value, dict):
        for k, v in value.items():
            assert isinstance(k, str), f"non-str dict key {k!r}"
            assert_json_safe(v)
    elif isinstance(value, (list, tuple)):
        for item in value:
            assert_json_safe(item)
    else:
        assert value is None or isinstance(value, (str, int, float, bool)), (
            f"non-JSON value leaked: {type(value)}"
        )


def assert_evidence(ev: Evidence, *, source: str, kind: str, origin: str) -> Evidence:
    """The capability boundary contract: an Evidence instance, right
    source/kind/origin, redacted, and fully JSON-serializable."""
    assert isinstance(ev, Evidence)
    assert type(ev).__module__ == "integrations.evidence"
    assert ev.source == source
    assert ev.kind == kind
    assert ev.origin == origin
    assert ev.redacted is True
    assert ev.id.startswith("ev_") and len(ev.id) == 19
    assert_json_safe(ev.model_dump())
    json.dumps(ev.model_dump())  # must not raise
    return ev


# --- Work-provider rig (2.3/2.10) ------------------------------------
#
# Stateful vendor-API fakes behind httpx.MockTransport. Each builder
# takes `seen` (a list every request is appended to, for API-shape
# assertions) plus the knobs that change a provider's capability answer:
# Jira's available workflow transitions, Linear's team states/members/
# labels, Trello's board lists/members/labels.


def make_jira_provider(
    *,
    seen: list[httpx.Request] | None = None,
    transitions: list[dict] | None = None,
) -> JiraProvider:
    """JiraProvider over a fake Jira Cloud REST v3. `transitions` is the
    fixed set GET /transitions answers - the workflow the fake project has.

    It mirrors behaviour checked against a live Jira Cloud site: the old
    /rest/api/3/search answers 410 Gone, /search/jql returns only issue
    ids unless `fields` is passed, assignment takes an accountId resolved
    through /user/search, and {"name": ...} assignment is rejected 400.
    The earlier fake served the removed endpoints, which is how the
    provider passed every test here and failed against real Jira."""
    issues: dict[str, dict] = {}
    users = [
        {"accountId": "acc-ona", "displayName": "ona", "emailAddress": "ona@example.com"},
        {"accountId": "acc-alice", "displayName": "alice", "emailAddress": "alice@example.com"},
    ]
    display_for = {u["accountId"]: u["displayName"] for u in users}
    seq = itertools.count(1)
    available = (
        transitions
        if transitions is not None
        else [
            {
                "id": "11",
                "name": "To Do",
                "to": {"name": "To Do", "statusCategory": {"key": "new"}},
            },
            {
                "id": "21",
                "name": "In Progress",
                "to": {"name": "In Progress", "statusCategory": {"key": "indeterminate"}},
            },
            {
                "id": "25",
                "name": "Blocked",
                "to": {"name": "Blocked", "statusCategory": {"key": "indeterminate"}},
            },
            {
                "id": "31",
                "name": "Done",
                "to": {"name": "Done", "statusCategory": {"key": "done"}},
            },
            {
                "id": "41",
                "name": "Closed",
                "to": {"name": "Closed", "statusCategory": {"key": "done"}},
            },
        ]
    )
    by_id = {t["id"]: t for t in available}

    def find_issue(ref: str) -> dict | None:
        return next((i for i in issues.values() if ref in (i["id"], i["key"])), None)

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        path = request.url.path
        if path == "/rest/api/3/search":
            return httpx.Response(410, json={"errorMessages": ["endpoint removed"]})
        if path == "/rest/api/3/search/jql":
            match = re.search(r'labels\s*=\s*"([^"]+)"', request.url.params.get("jql", ""))
            label = match.group(1) if match else ""
            hits = [i for i in issues.values() if label in i["fields"]["labels"]]
            if "fields" not in request.url.params:
                hits = [{"id": i["id"]} for i in hits]
            return httpx.Response(200, json={"issues": hits, "isLast": True})
        if path == "/rest/api/3/user/search":
            query = request.url.params.get("query", "").lower()
            return httpx.Response(
                200,
                json=[
                    u
                    for u in users
                    if query in (u["displayName"].lower(), u["emailAddress"].lower())
                ],
            )
        if path == "/rest/api/3/issueLink" and request.method == "POST":
            body = json.loads(request.content)
            if (
                find_issue(body["outwardIssue"]["key"]) is None
                or find_issue(body["inwardIssue"]["key"]) is None
            ):
                return httpx.Response(404, json={"errorMessages": ["issue not found"]})
            return httpx.Response(201, json={})
        if path == "/rest/api/3/issue" and request.method == "POST":
            fields = json.loads(request.content)["fields"]
            iid = str(next(seq))
            key = f"{fields['project']['key']}-{iid}"
            issues[iid] = {
                "id": iid,
                "key": key,
                "fields": {
                    "summary": fields.get("summary"),
                    "description": fields.get("description"),
                    "labels": fields.get("labels") or [],
                    "assignee": (
                        {"displayName": display_for[fields["assignee"]["accountId"]]}
                        if fields.get("assignee")
                        else None
                    ),
                    "priority": (
                        {"name": fields["priority"]["name"]} if fields.get("priority") else None
                    ),
                    "status": {"name": "To Do", "statusCategory": {"key": "new"}},
                },
            }
            return httpx.Response(
                201,
                json={
                    "id": iid,
                    "key": key,
                    "self": f"https://jira.test/rest/api/3/issue/{iid}",
                },
            )
        match = re.match(r"^/rest/api/3/issue/([^/]+)(?:/([a-zA-Z]+))?$", path)
        if not match:
            return httpx.Response(404, json={"errorMessages": ["no route"]})
        issue = issues.get(match.group(1))
        if issue is None:
            return httpx.Response(404, json={"errorMessages": ["issue does not exist"]})
        sub = match.group(2)
        if sub is None:
            if request.method == "GET":
                return httpx.Response(200, json=issue)
            fields = json.loads(request.content).get("fields", {})
            for field in ("summary", "description"):
                if field in fields:
                    issue["fields"][field] = fields[field]
            if "priority" in fields:
                issue["fields"]["priority"] = {"name": fields["priority"]["name"]}
            return httpx.Response(204)
        if sub == "assignee":
            body = json.loads(request.content)
            if "accountId" not in body:
                return httpx.Response(400, json={"errorMessages": ["accountId required"]})
            account = body["accountId"]
            issue["fields"]["assignee"] = {"displayName": display_for[account]} if account else None
            return httpx.Response(204)
        if sub == "transitions":
            if request.method == "GET":
                return httpx.Response(200, json={"transitions": available})
            transition = by_id.get(json.loads(request.content)["transition"]["id"])
            if transition is None:
                return httpx.Response(400, json={"errorMessages": ["bad transition"]})
            issue["fields"]["status"] = {
                "name": transition["to"]["name"],
                "statusCategory": transition["to"]["statusCategory"],
            }
            return httpx.Response(204)
        if sub == "comment":
            return httpx.Response(201, json={"id": f"c-{next(seq)}"})
        return httpx.Response(404, json={"errorMessages": ["no route"]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return JiraProvider("https://jira.test", "bot@jira.test", "jira-token", "HELIX", client=client)


def make_linear_provider(
    *,
    seen: list[httpx.Request] | None = None,
    states: list[dict] | None = None,
    members: list[dict] | None = None,
    labels: list[dict] | None = None,
) -> LinearProvider:
    """LinearProvider over a fake Linear GraphQL endpoint. `states` is
    the team's workflow-state list - the thing capabilities() derives
    the declared neutral states from."""
    team_states = (
        states
        if states is not None
        else [
            {"id": "st-backlog", "name": "Backlog", "type": "backlog"},
            {"id": "st-todo", "name": "Todo", "type": "unstarted"},
            {"id": "st-prog", "name": "In Progress", "type": "started"},
            {"id": "st-done", "name": "Done", "type": "completed"},
            {"id": "st-cancel", "name": "Canceled", "type": "canceled"},
        ]
    )
    team_members = (
        members
        if members is not None
        else [{"id": "u-ona", "name": "ona", "displayName": "Ona", "email": "ona@x.test"}]
    )
    team_labels = list(labels) if labels is not None else [{"id": "lb-helix", "name": "helix"}]
    issues: dict[str, dict] = {}
    seq = itertools.count(1)

    def find_member(member_id: str | None) -> dict | None:
        return next((m for m in team_members if m["id"] == member_id), None)

    def find_state(state_id: str | None) -> dict | None:
        return next((s for s in team_states if s["id"] == state_id), None)

    def gql(request: httpx.Request) -> dict:
        body = json.loads(request.content)
        query, var = body["query"], body.get("variables") or {}
        if "issueCreate" in query:
            inp = var["input"]
            n = next(seq)
            issue = {
                "id": f"iss-{n}",
                "identifier": f"ENG-{n}",
                "url": f"https://linear.test/issue/iss-{n}",
                "title": inp.get("title"),
                "description": inp.get("description"),
                "priority": inp.get("priority", 0),
                "state": find_state(inp.get("stateId"))
                or next((s for s in team_states if s["type"] == "unstarted"), team_states[0]),
                "assignee": find_member(inp.get("assigneeId")),
                "labels": {
                    "nodes": [
                        lab for lab in team_labels if lab["id"] in (inp.get("labelIds") or [])
                    ]
                },
            }
            issues[issue["id"]] = issue
            return {"data": {"issueCreate": {"success": True, "issue": issue}}}
        if "issueUpdate" in query:
            issue = issues.get(var["id"])
            if issue is None:
                return {"errors": [{"message": "Entity not found"}]}
            inp = var["input"]
            for field in ("title", "description", "priority"):
                if field in inp:
                    issue[field] = inp[field]
            if "stateId" in inp:
                issue["state"] = find_state(inp["stateId"])
            if "assigneeId" in inp:
                issue["assignee"] = find_member(inp["assigneeId"])
            return {"data": {"issueUpdate": {"success": True, "issue": issue}}}
        if "issueRelationCreate" in query:
            inp = var["input"]
            if inp["issueId"] not in issues or inp["relatedIssueId"] not in issues:
                return {"errors": [{"message": "Entity not found"}]}
            return {"data": {"issueRelationCreate": {"success": True}}}
        if "issueLabelCreate" in query:
            label = {"id": f"lb-{next(seq)}", "name": var["input"]["name"]}
            team_labels.append(label)
            return {"data": {"issueLabelCreate": {"success": True, "issueLabel": label}}}
        if "commentCreate" in query:
            if var["input"]["issueId"] not in issues:
                return {"errors": [{"message": "Entity not found"}]}
            return {"data": {"commentCreate": {"success": True}}}
        if "issues" in query:
            term = var.get("term", "")
            hits = [i for i in issues.values() if term in (i.get("description") or "")]
            return {"data": {"issues": {"nodes": hits[:1]}}}
        if "issue(" in query:
            return {"data": {"issue": issues.get(var["id"])}}
        if "team" in query:
            return {
                "data": {
                    "team": {
                        "states": {"nodes": team_states},
                        "members": {"nodes": team_members},
                        "labels": {"nodes": team_labels},
                    }
                }
            }
        return {"errors": [{"message": "unhandled graphql"}]}

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(200, json=gql(request))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return LinearProvider("lin-key", "team-1", client=client)


def make_trello_provider(
    *,
    seen: list[httpx.Request] | None = None,
    lists: list[dict] | None = None,
    members: list[dict] | None = None,
    labels: list[dict] | None = None,
    board_id: str = "b-1",
) -> TrelloProvider:
    """TrelloProvider over a fake Trello REST API. `lists` is the board's
    workflow - what the provider's declared states derive from."""
    board_lists = (
        lists
        if lists is not None
        else [
            {"id": "li-todo", "name": "To Do"},
            {"id": "li-doing", "name": "Doing"},
            {"id": "li-done", "name": "Done"},
        ]
    )
    board_members = (
        members if members is not None else [{"id": "m-ona", "username": "ona", "fullName": "Ona"}]
    )
    board_labels = list(labels) if labels is not None else [{"id": "tla-helix", "name": "helix"}]
    cards: dict[str, dict] = {}
    seq = itertools.count(1)

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        path = request.url.path
        if path == f"/1/boards/{board_id}/lists":
            return httpx.Response(200, json=board_lists)
        if path == f"/1/boards/{board_id}/cards":
            return httpx.Response(200, json=list(cards.values()))
        if path == f"/1/boards/{board_id}/members":
            return httpx.Response(200, json=board_members)
        if path == f"/1/boards/{board_id}/labels":
            return httpx.Response(200, json=board_labels)
        if path == "/1/labels" and request.method == "POST":
            label = {"id": f"tla-{next(seq)}", "name": json.loads(request.content)["name"]}
            board_labels.append(label)
            return httpx.Response(200, json=label)
        if path == "/1/cards" and request.method == "POST":
            body = json.loads(request.content)
            n = next(seq)
            card = {
                "id": f"card-{n}",
                "shortLink": f"lnk{n}",
                "name": body["name"],
                "desc": body.get("desc", ""),
                "idList": body["idList"],
                "idMembers": list(body.get("idMembers") or []),
                "idLabels": list(body.get("idLabels") or []),
                "closed": False,
                "url": f"https://trello.test/c/card-{n}",
            }
            cards[card["id"]] = card
            return httpx.Response(200, json=card)
        match = re.match(r"^/1/cards/([^/]+)(/.+)?$", path)
        if not match:
            return httpx.Response(404, json={"message": "no route"})
        card = cards.get(match.group(1))
        if card is None:
            return httpx.Response(404, json={"message": "card not found"})
        sub = match.group(2)
        if sub is None:
            if request.method == "GET":
                return httpx.Response(200, json=card)
            body = json.loads(request.content)
            for field in ("name", "desc", "idList", "idMembers", "idLabels", "closed"):
                if field in body:
                    card[field] = body[field]
            return httpx.Response(200, json=card)
        if sub == "/actions/comments":
            return httpx.Response(200, json={"id": f"act-{next(seq)}"})
        if sub == "/attachments":
            return httpx.Response(200, json={"id": f"att-{next(seq)}"})
        return httpx.Response(404, json={"message": "no route"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return TrelloProvider("tre-key", "tre-tok", board_id, client=client)


@pytest.fixture
def provider_factory() -> dict[str, Callable[..., WorkManagementProvider]]:
    """name -> builder for a provider over a recording mock transport.
    Keyword args configure the fake backend (jira transitions, linear
    states, trello lists, and `seen` for request capture)."""
    return {
        "memory": lambda **_kw: MemoryWorkProvider(),
        "jira": make_jira_provider,
        "linear": make_linear_provider,
        "trello": make_trello_provider,
    }


@pytest.fixture(params=["memory", "jira", "linear", "trello"])
def provider(request: pytest.FixtureRequest, provider_factory) -> WorkManagementProvider:
    """Every WorkManagementProvider implementation, one per test run -
    the conformance suite's version of "zero vendor vocabulary in
    callers"."""
    return provider_factory[request.param]()
