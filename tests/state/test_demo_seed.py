"""Demo seeding against the compose Postgres (integration tier): the real
harness subprocesses fill an empty database, and a second run changes
nothing. Unit-tier coverage of the early exit and failures is
tests/test_demo.py.
"""

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from agent.agents._change_context import change_context
from helix import demo
from helix.config import get_settings
from state import models


@pytest.mark.integration
def test_seed_demo_populates_an_empty_database_once(pg_engine, pg, monkeypatch):
    # run_benchmark pins these in the process env; monkeypatch restores them.
    for key, value in (
        ("HELIX_WORK_PROVIDER", "memory"),
        ("HELIX_GRAPH_ENABLED", "false"),
        ("HELIX_VERIFY_FUNCTIONAL", "false"),
        ("HELIX_SLACK_BOT_TOKEN", ""),
        ("HELIX_SLACK_SIGNING_SECRET", ""),
        ("HELIX_SLACK_APP_TOKEN", ""),
        ("HELIX_NOTION_API_KEY", ""),
    ):
        monkeypatch.setenv(key, value)
    url = get_settings().database_url

    def statuses() -> dict[str, str]:
        with Session(pg_engine) as session:
            rows = session.execute(select(models.Incident.incident_id, models.Incident.status))
            return dict(rows.all())

    assert demo.seed_demo(url) == len(demo.DEMO_SCENARIOS) + 2
    first = statuses()
    assert len(first) == len(demo.DEMO_SCENARIOS) + 2
    counts = {s: list(first.values()).count(s) for s in set(first.values())}
    assert counts == {"recovered": 7, "escalated": 1, "review_pending": 2}
    with Session(pg_engine) as session:
        pending = [
            (review.reviewer, change_context(review.incident)["author"])
            for review in session.scalars(
                select(models.ReviewRequest).where(models.ReviewRequest.status == "pending")
            )
        ]
    assert len(pending) == 2
    # Only the live registry's logins can sign in to the demo, and a PR's
    # author never reviews it.
    assert {reviewer for reviewer, _ in pending} == {"VrajVed", "nikhilpravinpise"}
    assert all(reviewer != author for reviewer, author in pending)

    assert demo.seed_demo(url) == 0
    assert statuses() == first
