"""People are shown by display name with the GitHub login as a tooltip
(FOLLOWUPS #21 "Names vs logins"). Someone without a display name, or not
on the team, is shown by the login itself."""

import pytest

from helix import ui

REGISTRY = """
services:
  payment-api:
    owners: [alice]
    default_reviewers: [bob]
people:
  alice:
    name: Alice Smith
  bob:
    name: Bob <Jones>
  carol: {}
"""


@pytest.fixture(autouse=True)
def registry(tmp_path, monkeypatch):
    path = tmp_path / "registry.yaml"
    path.write_text(REGISTRY)
    monkeypatch.setenv("HELIX_SERVICE_REGISTRY", str(path))


def test_a_named_person_shows_the_name_with_the_login_as_tooltip():
    assert str(ui._person("alice")) == '<span class="person" title="alice">Alice Smith</span>'


def test_names_and_logins_are_escaped():
    assert str(ui._person("bob")) == '<span class="person" title="bob">Bob &lt;Jones&gt;</span>'
    assert str(ui._person("<x>")) == "&lt;x&gt;"


def test_someone_without_a_name_or_not_on_the_team_is_their_login():
    assert str(ui._person("carol")) == "carol"
    assert str(ui._person("zed")) == "zed"
    assert str(ui._person(None)) == ""


def _did(record):
    rows = ui._summary(record)["rows"]
    return next(r for r in rows if r["label"] == "What Helix did")["text"]


def test_the_summary_names_the_approver_and_the_reviewer():
    executed = {
        "incident": {"status": "recovered", "kind": "failure", "service": "payment-api"},
        "stages": {
            "execution": [
                {
                    "id": 1,
                    "action": "restart",
                    "result": "success",
                    "detail": "",
                    "output": {},
                    "compensates_id": None,
                    "decided_by": "alice",
                }
            ]
        },
    }
    assert "approved by Alice Smith" in _did(executed)
    pending = {
        "incident": {"status": "review_pending", "kind": "change", "service": "payment-api"},
        "stages": {"reviews": [{"status": "pending", "reviewer": "bob"}]},
    }
    assert _did(pending) == "Nothing executed - review pending with Bob <Jones>"


def test_the_summary_names_the_triage_owner():
    record = {
        "incident": {"status": "owner_assigned", "kind": "change", "service": "payment-api"},
        "stages": {
            "triage": [
                {
                    "owner": "alice",
                    "rationale": "from the registry",
                    "affected_service": "payment-api",
                }
            ]
        },
    }
    why = next(r for r in ui._summary(record)["rows"] if r["label"] == "Why")["text"]
    assert why == "Owner Alice Smith. from the registry"
