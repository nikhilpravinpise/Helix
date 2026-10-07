"""Per-provider API-shape tests (subphases 2.3/2.10).

The contract suite proves every provider answers the same questions;
THIS file proves each one asks its vendor the right ones - Jira REST
v3 endpoints and basic auth, Linear's GraphQL mutations with the API
key as the bearerless Authorization header, Trello's key/token params
and list-as-workflow mapping. `seen` captures the exact requests the
mock transport received.
"""

import json

import pytest

from integrations.work import (
    JiraProvider,
    LinearProvider,
    MemoryWorkProvider,
    TrelloProvider,
    WorkItemError,
    WorkItemSpec,
    get_work_provider,
)


def _spec(incident_id: str = "INC-1", **overrides) -> WorkItemSpec:
    fields = {
        "incident_id": incident_id,
        "title": f"{incident_id}: payment-api crashloop",
        "description": "Helix incident on payment-api.",
        "severity": "high",
        "priority": "high",
        "labels": ["helix"],
    }
    fields.update(overrides)
    return WorkItemSpec(**fields)


# --- memory -----------------------------------------------------------


def test_memory_records_items_and_calls(provider_factory):
    provider = provider_factory["memory"]()
    ref = provider.create_work_item(_spec())
    provider.transition_work_item(ref.external_id, "in_progress")
    assert ref.external_id in provider.items
    assert provider.items[ref.external_id]["incident_id"] == "INC-1"
    assert [c["op"] for c in provider.calls] == [
        "create_work_item",
        "transition_work_item",
    ]


def test_memory_fail_with_simulates_outage(provider_factory):
    """The drain retry path's outage knob: with fail_with set, every
    method - even capabilities() - raises it."""
    provider = provider_factory["memory"]()
    provider.fail_with = WorkItemError("provider down")
    with pytest.raises(WorkItemError, match="down"):
        provider.capabilities()
    with pytest.raises(WorkItemError, match="down"):
        provider.find_by_incident("INC-1")
    with pytest.raises(WorkItemError, match="down"):
        provider.create_work_item(_spec())


def test_memory_create_is_idempotent_per_incident(provider_factory):
    provider = provider_factory["memory"]()
    first = provider.create_work_item(_spec())
    second = provider.create_work_item(_spec())
    assert first.external_id == second.external_id
    assert len(provider.items) == 1


def test_memory_ids_are_unique_across_provider_instances(provider_factory):
    """Every `helix` command is its own process with its own memory
    provider, so an id that is only unique within one instance is not an
    id at all. A per-instance counter gave every incident "MEM-1"; the
    benchmark's settle step then loaded all of them into one shared store,
    where they overwrote each other and one incident's "blocked" status
    became every other incident's - four Phase 2 scenarios measured as
    failed on arm A. Real providers mint keys unique across the whole
    remote (a Jira key never repeats), and the fake must too."""
    one = provider_factory["memory"]().create_work_item(_spec("INC-1"))
    two = provider_factory["memory"]().create_work_item(_spec("INC-2"))
    assert one.external_id != two.external_id


# --- jira -------------------------------------------------------------


def test_jira_create_shape(provider_factory):
    seen = []
    provider = provider_factory["jira"](seen=seen)
    ref = provider.create_work_item(_spec())
    request = next(r for r in seen if r.method == "POST" and r.url.path == "/rest/api/3/issue")
    assert request.headers["authorization"].startswith("Basic ")
    fields = json.loads(request.content)["fields"]
    assert fields["project"] == {"key": "HELIX"}
    assert fields["issuetype"] == {"name": "Task"}
    assert fields["priority"] == {"name": "High"}
    assert fields["summary"] == "INC-1: payment-api crashloop"
    assert "helix-incident-INC-1" in fields["labels"]
    assert "helix" in fields["labels"]
    assert fields["description"]["type"] == "doc"  # ADF, not a bare string
    assert ref.external_key == "HELIX-1"
    assert ref.external_url == "https://jira.test/browse/HELIX-1"


def test_jira_find_by_incident_uses_label_jql(provider_factory):
    seen = []
    provider = provider_factory["jira"](seen=seen)
    provider.create_work_item(_spec())
    seen.clear()
    found = provider.find_by_incident("INC-1")
    assert found is not None
    request = seen[-1]
    # /rest/api/3/search was removed from Jira Cloud (410 Gone, seen live);
    # /search/jql replaced it and needs `fields` to return more than ids.
    assert request.url.path == "/rest/api/3/search/jql"
    assert 'labels = "helix-incident-INC-1"' in request.url.params["jql"]
    assert "status" in request.url.params["fields"]


def test_jira_transition_goes_through_workflow(provider_factory):
    """Jira has no status field to write: the provider must read the
    available transitions and POST the matching transition id."""
    seen = []
    provider = provider_factory["jira"](seen=seen)
    ref = provider.create_work_item(_spec())
    seen.clear()
    ref = provider.transition_work_item(ref.external_id, "in_progress")
    assert ref.status == "in_progress"
    assert any(r.method == "GET" and r.url.path.endswith("/transitions") for r in seen)
    post = next(r for r in seen if r.method == "POST" and r.url.path.endswith("/transitions"))
    assert json.loads(post.content) == {"transition": {"id": "21"}}


def test_jira_transition_without_match_raises(provider_factory):
    """A workflow that cannot express a neutral status fails loudly."""
    provider = provider_factory["jira"](
        transitions=[
            {
                "id": "1",
                "name": "Done",
                "to": {"name": "Done", "statusCategory": {"key": "done"}},
            }
        ]
    )
    ref = provider.create_work_item(_spec())
    with pytest.raises(WorkItemError):
        provider.transition_work_item(ref.external_id, "in_progress")


def test_jira_assign_and_comment_and_link_shapes(provider_factory):
    seen = []
    provider = provider_factory["jira"](seen=seen)
    ref = provider.create_work_item(_spec())
    provider.assign_work_item(ref.external_id, "ona")
    assign = next(r for r in seen if r.url.path.endswith("/assignee"))
    assert assign.method == "PUT"
    # Jira Cloud rejects {"name": ...} with 400 (seen live); it takes the
    # accountId the provider resolves through /user/search.
    assert json.loads(assign.content) == {"accountId": "acc-ona"}

    provider.comment_work_item(ref.external_id, "note")
    comment = next(r for r in seen if r.url.path.endswith("/comment"))
    assert json.loads(comment.content)["body"]["type"] == "doc"

    other = provider.create_work_item(_spec("INC-2"))
    provider.link_work_item(ref.external_id, other.external_id, link_type="duplicates")
    link = next(r for r in seen if r.url.path == "/rest/api/3/issueLink")
    body = json.loads(link.content)
    assert body["type"] == {"name": "Duplicate"}
    assert body["outwardIssue"] == {"key": ref.external_id}
    assert body["inwardIssue"] == {"key": other.external_id}


# --- linear -----------------------------------------------------------


def test_linear_create_is_one_graphql_call(provider_factory):
    seen = []
    provider = provider_factory["linear"](seen=seen)
    ref = provider.create_work_item(_spec())
    request = next(r for r in seen if b"issueCreate" in r.content)
    assert str(request.url) == "https://api.linear.app/graphql"
    assert request.headers["authorization"] == "lin-key"
    body = json.loads(request.content)
    issue_input = body["variables"]["input"]
    assert issue_input["teamId"] == "team-1"
    assert issue_input["priority"] == 2  # neutral "high" -> Linear int
    assert "helix-incident:INC-1" in issue_input["description"]
    assert issue_input["labelIds"] == ["lb-helix"]  # name resolved to id
    assert ref.external_key == "ENG-1"
    assert ref.status == "open"


def test_linear_creates_missing_labels(provider_factory):
    seen = []
    provider = provider_factory["linear"](seen=seen)
    provider.create_work_item(_spec(labels=["novel-label"]))
    assert any(b"issueLabelCreate" in r.content for r in seen)


def test_linear_transition_maps_to_state_id(provider_factory):
    seen = []
    provider = provider_factory["linear"](seen=seen)
    ref = provider.create_work_item(_spec())
    ref = provider.transition_work_item(ref.external_id, "in_progress")
    assert ref.status == "in_progress"
    update = next(r for r in seen if b"issueUpdate" in r.content)
    issue_input = json.loads(update.content)["variables"]["input"]
    assert issue_input["stateId"] == "st-prog"


def test_linear_assign_resolves_member(provider_factory):
    seen = []
    provider = provider_factory["linear"](seen=seen)
    ref = provider.create_work_item(_spec())
    provider.assign_work_item(ref.external_id, "ona")
    update = next(
        r
        for r in seen
        if b"issueUpdate" in r.content
        and json.loads(r.content)["variables"]["input"].get("assigneeId")
    )
    issue_input = json.loads(update.content)["variables"]["input"]
    assert issue_input["assigneeId"] == "u-ona"
    with pytest.raises(WorkItemError):
        provider.assign_work_item(ref.external_id, "nobody")


def test_linear_update_preserves_dedup_marker(provider_factory):
    """A description rewrite must carry the marker forward, or the next
    find_by_incident misses and the drain mints a duplicate."""
    provider = provider_factory["linear"]()
    ref = provider.create_work_item(_spec())
    provider.update_work_item(ref.external_id, description="rewritten")
    assert provider.find_by_incident("INC-1") is not None


# --- trello -----------------------------------------------------------


def test_trello_auth_and_create_shape(provider_factory):
    seen = []
    provider = provider_factory["trello"](seen=seen)
    ref = provider.create_work_item(_spec())
    request = next(r for r in seen if r.method == "POST" and r.url.path == "/1/cards")
    assert request.url.params["key"] == "tre-key"
    assert request.url.params["token"] == "tre-tok"
    body = json.loads(request.content)
    assert body["idList"] == "li-todo"  # the board's To Do list
    assert "helix-incident:INC-1" in body["desc"]
    assert ref.external_key == "lnk1"
    assert ref.status == "open"


def test_trello_transition_moves_lists(provider_factory):
    seen = []
    provider = provider_factory["trello"](seen=seen)
    ref = provider.create_work_item(_spec())
    assert provider.transition_work_item(ref.external_id, "in_progress").status == ("in_progress")
    update = next(r for r in seen if r.method == "PUT" and r.url.path == "/1/cards/card-1")
    assert json.loads(update.content)["idList"] == "li-doing"


def test_trello_closed_archives_the_card(provider_factory):
    """No Closed list on a standard board - card archival is the native
    closed concept the provider falls back to."""
    seen = []
    provider = provider_factory["trello"](seen=seen)
    ref = provider.create_work_item(_spec())
    assert provider.transition_work_item(ref.external_id, "closed").status == "closed"
    update = next(r for r in seen if r.method == "PUT" and r.url.path == "/1/cards/card-1")
    assert json.loads(update.content) == {"closed": True}


def test_trello_no_matching_list_raises(provider_factory):
    """A board that cannot express a neutral status must not be
    approximated - no matching list means WorkItemError."""
    provider = provider_factory["trello"](lists=[{"id": "l1", "name": "To Do"}])
    ref = provider.create_work_item(_spec())
    assert "done" not in provider.capabilities().states
    with pytest.raises(WorkItemError):
        provider.transition_work_item(ref.external_id, "done")


def test_trello_create_needs_an_open_list(provider_factory):
    provider = provider_factory["trello"](lists=[{"id": "l1", "name": "Doing"}])
    with pytest.raises(WorkItemError):
        provider.create_work_item(_spec())


def test_trello_assign_comment_and_link_shapes(provider_factory):
    seen = []
    provider = provider_factory["trello"](seen=seen)
    ref = provider.create_work_item(_spec())
    provider.assign_work_item(ref.external_id, "ona")
    assign = next(r for r in seen if r.method == "PUT" and r.url.path == "/1/cards/card-1")
    assert json.loads(assign.content) == {"idMembers": ["m-ona"]}

    provider.comment_work_item(ref.external_id, "note")
    comment = next(r for r in seen if r.url.path.endswith("/actions/comments"))
    assert json.loads(comment.content) == {"text": "note"}

    other = provider.create_work_item(_spec("INC-2"))
    provider.link_work_item(ref.external_id, other.external_id)
    attach = next(r for r in seen if r.url.path.endswith("/attachments"))
    assert json.loads(attach.content)["url"] == other.external_url


# --- factory ------------------------------------------------------------


def test_factory_resolves_memory(monkeypatch):
    monkeypatch.setenv("HELIX_WORK_PROVIDER", "memory")
    assert isinstance(get_work_provider(), MemoryWorkProvider)


def test_factory_returns_none_without_credentials(monkeypatch):
    """Missing credentials -> None, so the drain records rows skipped
    rather than crashing."""
    monkeypatch.setenv("HELIX_WORK_PROVIDER", "jira")
    monkeypatch.setenv("HELIX_JIRA_API_TOKEN", "")
    assert get_work_provider() is None
    monkeypatch.setenv("HELIX_WORK_PROVIDER", "linear")
    monkeypatch.setenv("HELIX_LINEAR_API_KEY", "")
    monkeypatch.setenv("HELIX_LINEAR_TEAM_ID", "")
    assert get_work_provider() is None
    monkeypatch.setenv("HELIX_WORK_PROVIDER", "trello")
    monkeypatch.setenv("HELIX_TRELLO_API_KEY", "")
    assert get_work_provider() is None


def test_factory_builds_configured_providers(monkeypatch):
    monkeypatch.setenv("HELIX_WORK_PROVIDER", "jira")
    monkeypatch.setenv("HELIX_JIRA_API_TOKEN", "tok")
    assert isinstance(get_work_provider(), JiraProvider)
    monkeypatch.setenv("HELIX_WORK_PROVIDER", "linear")
    monkeypatch.setenv("HELIX_LINEAR_API_KEY", "key")
    monkeypatch.setenv("HELIX_LINEAR_TEAM_ID", "team")
    assert isinstance(get_work_provider(), LinearProvider)
    monkeypatch.setenv("HELIX_WORK_PROVIDER", "trello")
    monkeypatch.setenv("HELIX_TRELLO_API_KEY", "key")
    monkeypatch.setenv("HELIX_TRELLO_API_TOKEN", "tok")
    monkeypatch.setenv("HELIX_TRELLO_BOARD_ID", "board")
    assert isinstance(get_work_provider(), TrelloProvider)


def test_jira_assignee_with_no_matching_user_raises(provider_factory):
    """Helix's owners come from a service registry, not from Jira, so an
    owner may have no Jira account. That must surface as WorkItemError -
    the drain's failure vocabulary - not leave the item silently
    unassigned or send Jira a request it rejects."""
    provider = provider_factory["jira"]()
    ref = provider.create_work_item(_spec())
    with pytest.raises(WorkItemError, match="no Jira user"):
        provider.assign_work_item(ref.external_id, "nobody-in-jira")


class TestJiraStatusReadBack:
    """Live, 2026-10-06: KAN-122 moved to a "Blocked" column, but Helix read
    it back as `open`. On that board Blocked sits in Jira's To Do status
    category, and the name check only ran inside the In Progress
    category, so the mirror disagreed with Jira."""

    @pytest.mark.parametrize("category", ["new", "indeterminate"])
    def test_a_blocked_status_reads_as_blocked_in_any_category(self, category):
        from integrations.work.jira import _neutral_status

        status = {"name": "Blocked", "statusCategory": {"key": category}}
        assert _neutral_status(status) == "blocked"

    def test_ordinary_statuses_still_read_by_category(self):
        from integrations.work.jira import _neutral_status

        assert _neutral_status({"name": "To Do", "statusCategory": {"key": "new"}}) == "open"
        assert (
            _neutral_status({"name": "In Review", "statusCategory": {"key": "indeterminate"}})
            == "in_progress"
        )
        assert _neutral_status({"name": "Done", "statusCategory": {"key": "done"}}) == "done"
