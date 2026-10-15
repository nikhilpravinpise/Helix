"""The Home dashboard at /ui/ (what needs me, what is going on, is Helix
working) and the incident list that moved to /ui/incidents. Incidents come
from the real runner where a lifecycle matters (review_pending, awaiting
approval) and from seeded rows where the test must control the clock."""

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_summary import _recovered, _review_pending
from test_ui import _suspend_on_gate

from helix import auth, ui
from state import connections, models
from state.machine import IncidentStatus

BENCHMARK_REGISTRY = Path(__file__).resolve().parents[2] / "benchmark" / "service_registry.yaml"
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def registry(tmp_path, monkeypatch):
    """The test cast plus people, so a login has a display name."""
    path = tmp_path / "registry.yaml"
    path.write_text(
        BENCHMARK_REGISTRY.read_text()
        + "\npeople:\n  alice:\n    name: Alice Anders\n  bob:\n    name: Bob Brown\n"
    )
    monkeypatch.setenv("HELIX_SERVICE_REGISTRY", str(path))
    monkeypatch.setenv("HELIX_ENV_FILE", "")
    monkeypatch.delenv("HELIX_EVIDENCE_SOURCE", raising=False)


def sign_in(client, monkeypatch, login: str) -> None:
    monkeypatch.setenv("HELIX_GITHUB_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("HELIX_GITHUB_OAUTH_CLIENT_SECRET", "secret")
    monkeypatch.setenv("HELIX_SESSION_SECRET", "s3cret")
    monkeypatch.setenv("HELIX_ALLOWED_USERS", "alice,bob,carol,zed")
    client.cookies.set(
        auth.SESSION_COOKIE, auth._sign({"login": login, "exp": datetime.now().timestamp() + 600})
    )


def seed(
    engine,
    n: int,
    status: IncidentStatus,
    created_at: datetime,
    transitions: list[tuple[IncidentStatus | None, IncidentStatus, datetime]] = (),
    service: str = "payment-api",
) -> str:
    with Session(engine) as session:
        incident = models.Incident(
            incident_id=f"INC-{n:08x}",
            fingerprint=f"fp-{n}",
            service=service,
            environment="sandbox",
            severity="high",
            status=status,
            created_at=created_at,
        )
        session.add(incident)
        session.flush()
        for src, dst, at in transitions:
            session.add(
                models.Transition(
                    incident_id=incident.id,
                    from_status=src,
                    to_status=dst,
                    cause=f"cause-{n}-{dst.value}",
                    created_at=at,
                )
            )
        session.commit()
        return incident.incident_id


def current(body: str) -> list[str]:
    return re.findall(r'<a [^>]*aria-current="page"[^>]*>(.*?)</a>', body, re.S)


def section(body: str, name: str) -> str:
    match = re.search(rf'<section[^>]*data-section="{name}".*?</section>', body, re.S)
    assert match, f"no {name} section"
    return match.group(0)


class TestRoutes:
    def test_home_and_list_are_separate_pages(self, client):
        home = client.get("/ui/")
        assert home.status_code == 200
        assert 'data-page="home"' in home.text
        assert len(current(home.text)) == 1 and "Home" in current(home.text)[0]
        assert 'class="brand" href="/ui/"' in home.text
        listing = client.get("/ui/incidents")
        assert listing.status_code == 200
        assert 'data-page="home"' not in listing.text
        assert len(current(listing.text)) == 1 and "Incidents" in current(listing.text)[0]
        assert 'href="/ui/incidents"' in home.text

    def test_greeting_uses_registry_display_name(self, client, monkeypatch):
        sign_in(client, monkeypatch, "alice")
        assert "Welcome back, Alice Anders" in client.get("/ui/").text

    def test_greeting_falls_back_to_login_then_plain_welcome(self, client, monkeypatch):
        sign_in(client, monkeypatch, "zed")
        assert "Welcome back, zed" in client.get("/ui/").text
        client.cookies.clear()
        monkeypatch.delenv("HELIX_GITHUB_OAUTH_CLIENT_ID")
        body = client.get("/ui/").text
        assert "<h1>Welcome</h1>" in body and "Welcome back" not in body

    def test_eyebrow_has_todays_date(self, client):
        assert datetime.now(UTC).strftime("%A, %d %B %Y") in client.get("/ui/").text

    def test_test_incident_button_follows_samples_enabled(self, client, monkeypatch):
        assert 'action="/ui/samples"' not in client.get("/ui/").text
        monkeypatch.setenv("HELIX_EVIDENCE_SOURCE", "fixtures")
        assert client.get("/ui/").text.count('action="/ui/samples"') >= 1


class TestNeedsYou:
    def test_review_assigned_to_me_is_listed_others_wait(
        self, client, sqlite_sessions, store, monkeypatch
    ):
        incident_id = _review_pending(sqlite_sessions, store)
        with sqlite_sessions() as session:
            reviewer = session.scalar(select(models.ReviewRequest)).reviewer
        other = "carol" if reviewer == "bob" else "bob"
        sign_in(client, monkeypatch, reviewer)
        body = client.get("/ui/").text
        needs = section(body, "needs-you")
        assert incident_id in needs and "review" in needs
        assert "waiting" in needs
        assert "Nothing needs you right now" not in body
        assert 'data-section="waiting-on"' not in body

        client.cookies.clear()
        sign_in(client, monkeypatch, other)
        body = client.get("/ui/").text
        assert incident_id not in section(body, "needs-you")
        assert "Nothing needs you right now." in body
        waiting = section(body, "waiting-on")
        assert incident_id in waiting and ui._display_name(reviewer) in waiting

    def test_approval_owner_decides_but_a_stranger_does_not(
        self, client, sqlite_sessions, store, monkeypatch
    ):
        incident_id, _ = _suspend_on_gate(sqlite_sessions, store)
        sign_in(client, monkeypatch, "alice")
        needs = section(client.get("/ui/").text, "needs-you")
        assert incident_id in needs and "reapply_manifest" in needs
        assert 'href="/ui/approvals#approval-' in needs
        client.cookies.clear()
        sign_in(client, monkeypatch, "zed")
        body = client.get("/ui/").text
        assert incident_id not in section(body, "needs-you")
        assert "Alice Anders" in section(body, "waiting-on")

    def test_signed_out_shows_everything_pending_as_waiting(self, client, sqlite_sessions, store):
        incident_id, _ = _suspend_on_gate(sqlite_sessions, store)
        body = client.get("/ui/").text
        assert 'data-section="needs-you"' not in body
        assert incident_id in section(body, "waiting-on")

    def test_a_service_with_no_approvers_says_so(self, client, sqlite_sessions, store, monkeypatch):
        incident_id, _ = _suspend_on_gate(sqlite_sessions, store)
        monkeypatch.setattr(ui, "approval_deciders", lambda incident: [])
        waiting = section(client.get("/ui/").text, "waiting-on")
        assert incident_id in waiting and "waiting on no approver" in waiting
        assert "registered owner" not in waiting


class TestOpenIncidents:
    def test_excludes_terminal_and_caps(self, client, sqlite_engine):
        done = seed(sqlite_engine, 1, IncidentStatus.RECOVERED, NOW - timedelta(hours=1))
        esc = seed(sqlite_engine, 2, IncidentStatus.ESCALATED, NOW - timedelta(hours=1))
        for n in range(10, 18):
            seed(sqlite_engine, n, IncidentStatus.DIAGNOSING, NOW - timedelta(minutes=n))
        body = client.get("/ui/").text
        open_ = section(body, "open-incidents")
        assert done not in open_ and esc not in open_
        assert len(re.findall(r'href="/ui/incidents/INC-', open_)) == ui.HOME_OPEN_LIMIT == 6
        # Newest first: n=10 is the most recent.
        assert open_.index("INC-0000000a") < open_.index("INC-0000000b")
        assert 'href="/ui/incidents"' in open_

    def test_row_has_status_and_waiting_on(self, client, sqlite_sessions, store):
        _suspend_on_gate(sqlite_sessions, store)
        open_ = section(client.get("/ui/").text, "open-incidents")
        assert "st-awaiting_approval" in open_ and "awaiting approval" in open_
        assert "waiting for approval" in open_ and "payment-api" in open_


class TestStats:
    def seeded(self, engine, now=NOW):
        old = now - timedelta(days=45)
        ok = IncidentStatus
        seed(engine, 1, ok.RECOVERED, old, [(ok.VERIFYING, ok.RECOVERED, old + timedelta(hours=1))])
        t1 = now - timedelta(days=2)
        seed(
            engine, 2, ok.RECOVERED, t1, [(ok.VERIFYING, ok.RECOVERED, t1 + timedelta(minutes=10))]
        )
        t2 = now - timedelta(days=3)
        seed(
            engine, 3, ok.RECOVERED, t2, [(ok.VERIFYING, ok.RECOVERED, t2 + timedelta(minutes=30))]
        )
        t3 = now - timedelta(days=4)
        seed(
            engine, 4, ok.RECOVERED, t3, [(ok.VERIFYING, ok.RECOVERED, t3 + timedelta(minutes=20))]
        )
        seed(engine, 5, ok.ESCALATED, now - timedelta(days=1))
        seed(engine, 6, ok.DIAGNOSING, now - timedelta(hours=1))

    def test_exact_numbers(self, sqlite_engine):
        self.seeded(sqlite_engine)
        with Session(sqlite_engine) as session:
            stats = ui._home_stats(session, NOW)
        # The 45 day old incident is outside the window.
        assert stats["opened"] == 5
        assert stats["recovered"] == 3
        assert stats["escalated"] == 1
        # 3 of the 4 incidents that reached a terminal state recovered.
        assert stats["recovered_pct"] == 75
        assert stats["median_recover"] == "20m"

    def test_page_renders_the_numbers_and_the_window(self, client, sqlite_engine):
        self.seeded(sqlite_engine, datetime.now(UTC))
        body = client.get("/ui/").text
        stats = section(body, "stats")
        assert "last 30 days" in stats.lower()
        tiles = dict(
            re.findall(r'data-stat="([\w-]+)"[^>]*>\s*<span class="stat-value">([^<]+)<', stats)
        )
        assert tiles["opened"] == "5"
        assert tiles["recovered"] == "3" and tiles["escalated"] == "1"
        assert tiles["median-recover"] == "20m"
        assert "75% of resolved" in stats

    def test_no_recoveries_shows_dash(self, sqlite_engine):
        seed(sqlite_engine, 1, IncidentStatus.DIAGNOSING, NOW - timedelta(hours=1))
        with Session(sqlite_engine) as session:
            stats = ui._home_stats(session, NOW)
        assert stats["median_recover"] == "-" and stats["recovered_pct"] is None
        assert stats["opened"] == 1 and stats["recovered"] == 0

    def test_median_of_an_even_count_and_duration_format(self):
        assert ui._duration(5) == "5s"
        assert ui._duration(61) == "1m"
        assert ui._duration(3600 + 12 * 60) == "1h 12m"
        assert ui._duration(7200) == "2h"
        assert ui._duration(2 * 86400 + 3 * 3600) == "2d 3h"
        assert ui._median([10, 20, 30, 40]) == 25
        assert ui._median([]) is None


class TestActivity:
    def test_newest_transition_first_and_capped(self, client, sqlite_engine):
        ok = IncidentStatus
        for n in range(1, 11):
            seed(
                sqlite_engine,
                n,
                ok.DIAGNOSING,
                NOW - timedelta(hours=n + 1),
                [(None, ok.RECEIVED, NOW - timedelta(hours=n))],
            )
        activity = section(client.get("/ui/").text, "activity")
        ids = re.findall(r'href="/ui/incidents/(INC-\w+)"', activity)
        assert ids == [f"INC-{n:08x}" for n in range(1, 9)]
        assert "cause-1-received" in activity and "received" in activity

    def test_shows_from_to_labels(self, client, sqlite_engine):
        ok = IncidentStatus
        seed(
            sqlite_engine,
            1,
            ok.REVIEW_PENDING,
            NOW - timedelta(hours=2),
            [(ok.TRIAGED, ok.REVIEW_PENDING, NOW - timedelta(hours=1))],
        )
        activity = section(client.get("/ui/").text, "activity")
        assert "triaged" in activity and "review pending" in activity


class TestTools:
    def test_connected_and_not(self, client, sqlite_engine, monkeypatch):
        monkeypatch.setenv("HELIX_CONNECTIONS_ENABLED", "true")
        monkeypatch.setenv("HELIX_CONNECTIONS_KEY", "test connections key")
        with Session(sqlite_engine) as session:
            connections.save_connection(
                session,
                "slack",
                account="Acme",
                scopes=["chat:write"],
                access_token="xoxb-secret",
                refresh_token=None,
                expires_at=None,
                settings={},
            )
            session.commit()
        tools = section(client.get("/ui/").text, "tools")
        slack = re.search(r'data-tool="slack".*?</li>', tools, re.S).group(0)
        jira = re.search(r'data-tool="jira".*?</li>', tools, re.S).group(0)
        github = re.search(r'data-tool="github".*?</li>', tools, re.S).group(0)
        assert "Connected" in slack and "Acme" in slack and "xoxb" not in tools
        assert "Not connected" in jira and "Not connected" in github
        assert tools.count('href="/ui/connections"') >= 3


class TestFreshWorkspace:
    def test_empty_workspace_is_intentional(self, client, monkeypatch):
        body = client.get("/ui/").text
        assert "Nothing needs you right now." in body
        assert "No open incidents" in section(body, "open-incidents")
        assert "No activity yet" in section(body, "activity")
        assert "Not connected" in section(body, "tools")
        assert 'data-section="getting-started"' in body
        assert 'href="/ui/connections"' in section(body, "getting-started")
        assert 'action="/ui/samples"' not in body
        monkeypatch.setenv("HELIX_EVIDENCE_SOURCE", "fixtures")
        assert 'action="/ui/samples"' in section(client.get("/ui/").text, "getting-started")

    def test_not_shown_once_there_is_an_incident(self, client, sqlite_sessions, store):
        _recovered(sqlite_sessions, store)
        assert 'data-section="getting-started"' not in client.get("/ui/").text
