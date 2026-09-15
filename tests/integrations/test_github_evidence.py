"""GitHub capability tests: REST normalization via httpx.MockTransport,
failure-mode mapping, and the fixture-backed impl."""

import httpx
import pytest
from conftest import assert_evidence

from integrations.evidence import (
    EvidenceAuthError,
    EvidenceError,
    EvidenceNotFoundError,
    EvidencePartialError,
    EvidenceRateLimitError,
    EvidenceTimeoutError,
)
from integrations.fixtures import FixtureGitHubEvidence
from integrations.github.client import GitHubEvidenceClient


def _client(handler) -> GitHubEvidenceClient:
    return GitHubEvidenceClient(
        repo="nikhilpravinpise/Helix",
        token="t",
        client=httpx.Client(
            base_url="https://api.github.com",
            transport=httpx.MockTransport(handler),
        ),
    )


def _routing(routes):
    def handler(request):
        path = request.url.path
        for suffix, response in routes.items():
            if path.endswith(suffix):
                return response
        return httpx.Response(404, json={"message": "no route"})

    return handler


_COMMIT = {
    "sha": "abc123def456",
    "commit": {
        "message": "Fix the thing\n\ndetails",
        "author": {"name": "Dev", "date": "2026-09-15T10:00:00Z"},
    },
    "parents": [{"sha": "parent1"}],
    "html_url": "https://github.com/x/commit/abc123def456",
    "files": [
        {
            "filename": "sandbox/payment-api/app/main.py",
            "status": "modified",
            "additions": 3,
            "deletions": 1,
            "patch": "@@ -1 +1 @@",
        }
    ],
}


def test_get_commit_normalizes():
    ev = _client(_routing({"commits/abc123def456": httpx.Response(200, json=_COMMIT)})).get_commit(
        "abc123def456"
    )
    assert_evidence(ev, source="github", kind="commit", origin="live")
    assert ev.payload["sha"] == "abc123def456"
    assert ev.payload["message"].startswith("Fix the thing")
    assert ev.payload["parents"] == ["parent1"]


def test_get_diff_normalizes_files():
    ev = _client(_routing({"commits/abc123def456": httpx.Response(200, json=_COMMIT)})).get_diff(
        "abc123def456"
    )
    assert_evidence(ev, source="github", kind="diff", origin="live")
    f = ev.payload["files"][0]
    assert f["filename"] == "sandbox/payment-api/app/main.py"
    assert f["additions"] == 3
    assert ev.total == 1


def test_get_pull_request_normalizes():
    pr = {
        "number": 7,
        "title": "INC-1 fix",
        "state": "open",
        "merged": False,
        "mergeable": True,
        "draft": False,
        "head": {"sha": "headsha", "ref": "helix/INC-1"},
        "base": {"ref": "main"},
        "user": {"login": "helix"},
        "html_url": "https://github.com/x/pull/7",
    }
    ev = _client(_routing({"pulls/7": httpx.Response(200, json=pr)})).get_pull_request(7)
    assert_evidence(ev, source="github", kind="pr_info", origin="live")
    assert ev.payload["head_ref"] == "helix/INC-1"
    assert ev.payload["base_ref"] == "main"


def test_get_pr_reviews_normalizes():
    reviews = [
        {
            "id": 1,
            "user": {"login": "rev"},
            "state": "CHANGES_REQUESTED",
            "body": "nope",
            "submitted_at": "2026-09-15T10:00:00Z",
        }
    ]
    ev = _client(_routing({"pulls/7/reviews": httpx.Response(200, json=reviews)})).get_pr_reviews(7)
    assert_evidence(ev, source="github", kind="pr_review", origin="live")
    assert ev.payload["items"][0]["state"] == "CHANGES_REQUESTED"


def test_get_check_runs_normalizes():
    data = {
        "total_count": 1,
        "check_runs": [
            {
                "name": "ci",
                "status": "completed",
                "conclusion": "failure",
                "started_at": "t",
                "completed_at": "t2",
                "html_url": "u",
            }
        ],
    }
    ev = _client(_routing({"check-runs": httpx.Response(200, json=data)})).get_check_runs("abc123")
    assert_evidence(ev, source="github", kind="check_run", origin="live")
    assert ev.payload["items"][0]["conclusion"] == "failure"
    assert ev.truncated is False


def test_get_check_runs_truncated_when_paginated():
    data = {"total_count": 150, "check_runs": [{"name": f"c{i}"} for i in range(100)]}
    ev = _client(_routing({"check-runs": httpx.Response(200, json=data)})).get_check_runs("abc123")
    assert ev.truncated is True
    assert ev.total == 150


def test_get_branch_state_with_compare():
    routes = {
        "branches/feature": httpx.Response(
            200, json={"name": "feature", "commit": {"sha": "deadbeef"}, "protected": False}
        ),
        "compare/main...feature": httpx.Response(
            200, json={"ahead_by": 2, "behind_by": 5, "status": "diverged"}
        ),
    }
    ev = _client(_routing(routes)).get_branch_state("feature", base="main")
    assert_evidence(ev, source="github", kind="branch_state", origin="live")
    assert ev.payload["ahead_by"] == 2
    assert ev.payload["behind_by"] == 5


@pytest.mark.parametrize(
    "status,exc",
    [
        (401, EvidenceAuthError),
        (403, EvidenceAuthError),
        (404, EvidenceNotFoundError),
        (429, EvidenceRateLimitError),
        (500, EvidenceError),
    ],
)
def test_http_error_mapping(status, exc):
    def handler(request):
        return httpx.Response(status, json={"message": "x"})

    with pytest.raises(exc):
        _client(handler).get_commit("abc123")


def test_timeout_maps_to_timeout_error():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(EvidenceTimeoutError):
        _client(handler).get_commit("abc123")


def test_transport_error_maps_to_evidence_error():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(EvidenceError):
        _client(handler).get_commit("abc123")


def test_malformed_commit_is_partial():
    ev_client = _client(_routing({"commits/abc": httpx.Response(200, json={"nope": 1})}))
    with pytest.raises(EvidencePartialError):
        ev_client.get_commit("abc")


def test_malformed_check_runs_is_partial():
    ev_client = _client(_routing({"check-runs": httpx.Response(200, json=["not a dict"])}))
    with pytest.raises(EvidencePartialError):
        ev_client.get_check_runs("abc")


# --- fixture-backed impl ---------------------------------------------------


def test_fixture_github_reads_artifact():
    fixture = {
        "fault": "synthetic",
        "evidence": {"github": {"commit": {"sha": "abc", "message": "m"}}},
    }
    ev = FixtureGitHubEvidence(fixture).get_commit("abc")
    assert_evidence(ev, source="github", kind="commit", origin="fixture")
    assert ev.payload["sha"] == "abc"


def test_fixture_github_missing_artifact_raises(store):
    with pytest.raises(EvidenceNotFoundError):
        FixtureGitHubEvidence(store.load("crashloop")).get_pull_request(1)
