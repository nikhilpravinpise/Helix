"""Correlation ground truth (subphase E7): `benchmark/correlation/groups.py`
built from real captured fixtures, and what `events.ingest.ingest_event`
actually does with it on a real event store.

Runs against the in-memory SQLite tier, same as `tests/events/test_ingest.py`
- everything under test lives in `ingest_event` and `correlation_accuracy`,
neither of which is database-specific.
"""

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from benchmark.correlation.groups import build_groups
from events.correlation import tokens_for
from events.ingest import ingest_event
from events.metrics import correlation_accuracy
from state import models


@pytest.fixture
def db_session(sqlite_engine: Engine) -> Iterator[Session]:
    with Session(sqlite_engine, expire_on_commit=False) as session:
        yield session


def test_build_groups_returns_the_two_documented_groups():
    groups = build_groups()
    assert [g.name for g in groups] == ["failed-check", "crashloop"]
    assert [g.source_fixture for g in groups] == [
        "github/failed-check.json",
        "cluster/crashloop.json",
    ]
    assert [len(g.events) for g in groups] == [2, 3]


def test_group_events_carry_real_fixture_values_not_placeholders():
    failed_check, crashloop = build_groups()
    github_event, jenkins_event = failed_check.events
    assert github_event.metadata["ref"] == "77aa01c9e2b3f4d5a6b7c8d9e0f1a2b3c4d5e6f7"
    assert jenkins_event.metadata["job"] == "payment-api"
    assert jenkins_event.metadata["build_number"] == 5

    pod_event, object_event, log_event = crashloop.events
    assert pod_event.metadata["pod"] == "payment-api-98867c74d-h6j8v"
    assert pod_event.metadata["waiting_reason"] == "CrashLoopBackOff"
    assert object_event.metadata["involved_name"] == "payment-api-98867c74d-h6j8v"
    assert log_event.metadata["pod"] == "payment-api-98867c74d-h6j8v"
    assert "startup failed" in log_event.metadata["message"]


def test_github_and_jenkins_share_no_identity_token():
    """A real finding, not a design choice this test is asserting away:
    `events.adapters.jenkins.build_event` never puts a commit/ref in its
    metadata, so a real GitHub check-run and a real Jenkins build for the
    same commit correlate only through the `service` proximity token, not
    through anything that asserts they are the same commit."""
    failed_check, _crashloop = build_groups()
    github_event, jenkins_event = failed_check.events
    github_tokens = tokens_for(github_event)
    jenkins_tokens = tokens_for(jenkins_event)
    assert github_tokens & jenkins_tokens == {"service:payment-api:sandbox"}
    assert any(t.startswith("commit:") for t in github_tokens)
    assert not any(t.startswith("commit:") for t in jenkins_tokens)


def test_kubernetes_object_event_and_pod_event_share_the_pod_token():
    """A Kubernetes Warning about a pod and that pod's own failure must
    meet on identity, not only at the service token.

    They did not: the Warning spelled it `object:<name>` and the pod
    spelled it `pod:<name>`, so the only thing linking them was
    `service:payment-api:sandbox`. That is invisible on this
    single-service sandbox, where the service token matches everything,
    and would split one fault across two incidents on any cluster where
    the service token actually discriminates. An earlier version of this
    test asserted the mismatch as expected behaviour, which recorded a
    defect as a requirement."""
    _failed_check, crashloop = build_groups()
    pod_event, object_event, _log_event = crashloop.events
    shared = tokens_for(pod_event) & tokens_for(object_event)
    assert "pod:payment-api-98867c74d-h6j8v" in shared


def _ingest_all(session: Session, groups, order) -> dict[str, set[str]]:
    """Ingest every event across `groups` in `order`, returning the set of
    incident ids each group's events landed on."""
    all_events = [(event, group.name) for group in groups for event in order(group.events)]
    for event, _name in all_events:
        ingest_event(session, event)
    landed: dict[str, set[str]] = {group.name: set() for group in groups}
    for group in groups:
        for event in group.events:
            record = session.scalar(
                select(models.EventRecord).where(models.EventRecord.event_id == event.event_id)
            )
            if record is not None:
                landed[group.name].add(record.incident.incident_id)
    return landed


def test_chronological_ingestion_keeps_the_two_groups_on_separate_incidents(db_session):
    """The two groups are ~22 hours apart in their real captured
    timestamps - well outside CORRELATION_WINDOW (30 minutes) - so
    ingesting them in the order they actually occurred must not merge
    them, even though both share `service:payment-api:sandbox`."""
    groups = build_groups()
    ordered_events = sorted(
        ((event, group.name) for group in groups for event in group.events),
        key=lambda pair: pair[0].timestamp,
    )
    results = {event.event_id: ingest_event(db_session, event) for event, _name in ordered_events}

    failed_check, crashloop = groups
    failed_check_incidents = {results[e.event_id].incident_id for e in failed_check.events}
    crashloop_incidents = {results[e.event_id].incident_id for e in crashloop.events}
    assert len(failed_check_incidents) == 1
    assert len(crashloop_incidents) == 1
    assert failed_check_incidents != crashloop_incidents


def test_out_of_order_ingestion_does_not_merge_unrelated_groups(db_session):
    """The correlation window has to bound BOTH directions.

    `events.ingest._correlated_incident` compared
    `occurred_at - latest <= CORRELATION_WINDOW`, a signed difference. An
    event older than the incident's latest produces a negative delta,
    which passes that check however far back it reaches - so the window
    bounded only the future. Ingesting the newer group (`failed-check`,
    2026-08-29) before the older one (`crashloop`, 2026-08-28) merged two
    real faults 22 hours apart onto one incident, on nothing but the
    shared service token.

    Out-of-order delivery is not adversarial here; it is what a Kafka
    topic produces and what replay does by definition. An earlier version
    of this test asserted the merge as expected behaviour, which recorded
    a defect as a requirement.
    """
    failed_check, crashloop = build_groups()
    results = {}
    for group in (failed_check, crashloop):  # newer group first, deliberately
        for event in group.events:
            results[event.event_id] = ingest_event(db_session, event)

    failed_check_incidents = {results[e.event_id].incident_id for e in failed_check.events}
    crashloop_incidents = {results[e.event_id].incident_id for e in crashloop.events}
    assert len(failed_check_incidents) == 1
    assert len(crashloop_incidents) == 1
    assert failed_check_incidents.isdisjoint(crashloop_incidents)


def test_correlation_accuracy_reports_both_groups_correct_either_way(db_session):
    """`correlation_accuracy` only asks "did this group's own events share
    one incident" - it has no way to know two groups were meant to be
    different incidents, so it reports 1.0 whether or not the out-of-order
    false merge above happened. That is the metric's documented scope,
    not a bug in it; the false merge is a separate, real finding a caller
    has to check for itself (`benchmark/correlation/run.py` does)."""
    groups = build_groups()
    for group in groups:  # newer-first: triggers the false merge
        for event in group.events:
            ingest_event(db_session, event)
    expected_groups = [[event.event_id for event in group.events] for group in groups]
    result = correlation_accuracy(db_session, expected_groups)
    assert result.rate == 1.0
    assert result.correct == 2
    assert result.denominator == 2
