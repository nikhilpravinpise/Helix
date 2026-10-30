"""Deciding change-lifecycle reviews from the web: the approvals page is the
single decision queue (approvals plus reviews) and a parked change shows the
same controls inline on its incident page."""

import html
import re
from urllib.parse import parse_qs, urlparse

import pytest
from fakes import FakeLLM, make_deps
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_summary import _review_pending

from agent.runner import run_incident
from helix import api, auth, ui
from state import models
from state.db import session_scope


def _review(sessions, incident_id: str) -> models.ReviewRequest:
    with sessions() as session:
        row = session.scalar(
            select(models.ReviewRequest)
            .join(models.Incident)
            .where(models.Incident.incident_id == incident_id)
        )
        session.expunge(row)
        return row


def _resume_with(monkeypatch, sqlite_sessions, store):
    deps, _ = make_deps(sqlite_sessions, FakeLLM(), "cluster/crashloop", store)
    monkeypatch.setattr(
        "agent.runner.run_incident", lambda iid, **kw: run_incident(iid, deps=deps, arm="A")
    )


def _status(sessions, incident_id: str) -> str:
    with sessions() as session:
        return session.scalar(
            select(models.Incident.status).where(models.Incident.incident_id == incident_id)
        ).value


class TestReviewsOnApprovalsPage:
    def test_page_title_is_plain_text(self, client, sqlite_sessions, store):
        """The reviews section once sat inside the title block, so the tab
        title carried raw HTML whenever a review existed."""
        _review_pending(sqlite_sessions, store)
        body = client.get("/ui/approvals").text
        title = re.search(r"<title>(.*?)</title>", body, re.S).group(1)
        assert title == "Approvals &amp; reviews - Helix"
        assert body.count('<h2 class="section-title">Pending reviews</h2>') == 1

    def test_pending_review_has_controls_with_id_and_kind(self, client, sqlite_sessions, store):
        incident_id = _review_pending(sqlite_sessions, store)
        review = _review(sqlite_sessions, incident_id)
        body = client.get("/ui/approvals").text
        assert "Pending reviews" in body
        assert f'id="review-{review.id}"' in body
        assert incident_id in body
        assert review.reviewer in body
        assert review.rationale in html.unescape(body)
        for action in ("approve", "reject"):
            assert re.search(
                rf'data-decide="{action}"[^>]*data-decide-kind="review"[^>]*'
                rf'data-approval-id="{review.id}"',
                body,
            )
        assert f'id="note-review-{review.id}"' in body
        assert 'id="actor"' in body

    def test_empty_queue_still_says_nothing_awaiting(self, client):
        body = client.get("/ui/approvals").text
        assert "Nothing awaiting approval" in body
        assert 'data-decide-kind="review"' not in body

    def test_nav_count_includes_pending_reviews(self, client, sqlite_sessions, store):
        _review_pending(sqlite_sessions, store)
        for path in ("/ui/", "/ui/approvals"):
            body = client.get(path).text
            assert re.search(r'class="count count-hot">1<', body), path

    def test_approve_moves_to_decided_and_approved(
        self, client, sqlite_sessions, store, monkeypatch
    ):
        incident_id = _review_pending(sqlite_sessions, store)
        review = _review(sqlite_sessions, incident_id)
        _resume_with(monkeypatch, sqlite_sessions, store)
        response = client.post(
            f"/reviews/{review.id}/approve",
            json={"actor": review.reviewer, "note": "looks fine"},
        )
        assert response.status_code == 200, response.text
        # The API resumes one hop: review_pending -> approved. Deploying
        # follows on the next run.
        assert _status(sqlite_sessions, incident_id) == "approved"
        page = client.get("/ui/approvals").text
        assert f'id="review-{review.id}"' not in page
        assert "ap-approved" in page
        assert review.reviewer in page
        assert "looks fine" in page
        assert not re.search(r'class="count count-hot"', page)
        detail = client.get(f"/ui/incidents/{incident_id}").text
        assert 'data-decide-kind="review"' not in detail
        assert "Review approved - deploy not started yet" in detail

    def test_reject_records_and_leaves_review_pending(
        self, client, sqlite_sessions, store, monkeypatch
    ):
        incident_id = _review_pending(sqlite_sessions, store)
        review = _review(sqlite_sessions, incident_id)
        _resume_with(monkeypatch, sqlite_sessions, store)
        response = client.post(
            f"/reviews/{review.id}/reject",
            json={"actor": review.reviewer, "note": "needs a test"},
        )
        assert response.status_code == 200, response.text
        assert _status(sqlite_sessions, incident_id) != "review_pending"
        page = client.get("/ui/approvals").text
        assert "ap-rejected" in page
        assert "needs a test" in page

    def test_wrong_decider_is_refused_and_stays_pending(self, client, sqlite_sessions, store):
        incident_id = _review_pending(sqlite_sessions, store)
        review = _review(sqlite_sessions, incident_id)
        response = client.post(f"/reviews/{review.id}/approve", json={"actor": "nobody"})
        assert response.status_code == 409
        assert _status(sqlite_sessions, incident_id) == "review_pending"


class TestReviewOnIncidentPage:
    def test_pending_review_shows_inline_controls(self, client, sqlite_sessions, store):
        incident_id = _review_pending(sqlite_sessions, store)
        review = _review(sqlite_sessions, incident_id)
        body = client.get(f"/ui/incidents/{incident_id}").text
        assert 'data-decide-kind="review"' in body
        assert f'data-approval-id="{review.id}"' in body
        assert f'id="note-review-{review.id}"' in body
        assert 'id="actor"' in body

    def test_incident_without_review_has_no_controls(self, client, sqlite_sessions):
        from fakes import K8S_SIGNAL
        from test_ui import _make_incident

        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)
        body = client.get(f"/ui/incidents/{incident_id}").text
        assert "data-decide-kind" not in body
        assert 'id="actor"' not in body


def test_signed_in_login_is_recorded_not_the_body_actor(
    sqlite_engine, sqlite_sessions, store, monkeypatch
):
    incident_id = _review_pending(sqlite_sessions, store)
    review = _review(sqlite_sessions, incident_id)
    _resume_with(monkeypatch, sqlite_sessions, store)

    def override():
        with session_scope(sqlite_engine) as session:
            yield session

    api.app.dependency_overrides[api.get_db_session] = override
    api.app.dependency_overrides[ui.get_db_session] = override
    monkeypatch.setenv("HELIX_SESSION_SECRET", "test-session-secret")
    monkeypatch.setenv("HELIX_PUBLIC_URL", "https://helix.example")
    monkeypatch.setenv("HELIX_GITHUB_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("HELIX_GITHUB_OAUTH_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("HELIX_ALLOWED_USERS", review.reviewer)
    monkeypatch.setattr(
        auth, "github_login", lambda code, redirect_uri: auth.GitHubUser(review.reviewer)
    )
    try:
        with TestClient(api.app, base_url="https://helix.example") as c:
            start = c.get("/auth/github", follow_redirects=False)
            state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
            c.get(f"/auth/github/callback?code=c&state={state}", follow_redirects=False)
            page = c.get("/ui/approvals").text
            assert f"Deciding as <b>{review.reviewer}</b>" in page
            response = c.post(f"/reviews/{review.id}/approve", json={"actor": "someone-else"})
            assert response.status_code == 200, response.text
            assert response.json()["review"]["decided_by"] == review.reviewer
            assert review.reviewer in c.get("/ui/approvals").text
    finally:
        api.app.dependency_overrides.clear()


@pytest.mark.parametrize(
    ("status", "text"),
    [
        ("approved", "Review approved - deploy not started yet"),
        ("deploying", "Review approved - waiting for the deploy to report"),
    ],
)
def test_summary_reads_sensibly_after_approval(status, text):
    assert ui._did({"status": status}, {"execution": [], "trigger": [{}]}) == {"text": text}
