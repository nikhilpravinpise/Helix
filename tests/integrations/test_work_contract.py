"""Provider conformance suite (subphases 2.3/2.10).

Every WorkManagementProvider implementation answers the same contract
questions the same way: create/get/find round-trip, one item per
incident, the neutral status vocabulary both directions, and honest
capability declarations (a status outside `capabilities().states` must
raise, never silently approximate). The `provider` fixture parametrizes
over all four implementations - memory, Jira, Linear, Trello - so a
provider that cannot satisfy these assertions does not belong behind
the contract. That parametrization is the testable form of the 2.3 exit
criterion: no vendor vocabulary appears in callers.
"""

import pytest

from integrations.work.contract import (
    NEUTRAL_STATUSES,
    ProviderCapabilities,
    WorkItemError,
    WorkItemRef,
    WorkItemSpec,
    WorkManagementProvider,
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


def test_satisfies_the_protocol(provider):
    assert isinstance(provider, WorkManagementProvider)
    assert provider.name


def test_capabilities_shape(provider):
    caps = provider.capabilities()
    assert isinstance(caps, ProviderCapabilities)
    assert caps.states  # a provider must express at least one state
    assert "open" in caps.states  # ...and `open` is the create state
    assert set(caps.states) <= set(NEUTRAL_STATUSES)


def test_create_get_find_roundtrip(provider):
    ref = provider.create_work_item(_spec())
    assert isinstance(ref, WorkItemRef)
    assert ref.status == "open"
    assert ref.external_id

    got = provider.get_work_item(ref.external_id)
    assert got is not None
    assert got.external_id == ref.external_id
    assert got.status == "open"

    found = provider.find_by_incident("INC-1")
    assert found is not None
    assert found.external_id == ref.external_id

    assert provider.find_by_incident("INC-999") is None
    assert provider.get_work_item("no-such-item") is None


def test_one_item_per_incident(provider):
    """The 2.3 dedup contract: the drain's consult-before-create pattern
    must resolve to a single item no matter how many times it runs."""
    spec = _spec()
    first = provider.find_by_incident(spec.incident_id) or provider.create_work_item(spec)
    second = provider.find_by_incident(spec.incident_id) or provider.create_work_item(spec)
    assert first.external_id == second.external_id


def test_status_vocabulary_and_declared_states(provider):
    """Every declared neutral state round-trips through the provider's
    native workflow and reads back as the same neutral word."""
    ref = provider.create_work_item(_spec())
    declared = set(provider.capabilities().states)
    for status in ("open", "in_progress", "blocked", "done", "closed"):
        if status not in declared:
            continue
        ref = provider.transition_work_item(ref.external_id, status)
        assert ref.status == status
        assert provider.get_work_item(ref.external_id).status == status


def test_transition_validates_vocabulary(provider):
    ref = provider.create_work_item(_spec())
    with pytest.raises(WorkItemError):
        provider.transition_work_item(ref.external_id, "banana")


def test_undeclared_states_raise(provider):
    """A neutral status outside the provider's declared states must
    raise WorkItemError - the declared-difference mechanism the 2.10
    study relies on."""
    ref = provider.create_work_item(_spec())
    for status in NEUTRAL_STATUSES - set(provider.capabilities().states):
        with pytest.raises(WorkItemError):
            provider.transition_work_item(ref.external_id, status)


def test_update_patches_fields(provider):
    ref = provider.create_work_item(_spec())
    updated = provider.update_work_item(
        ref.external_id, title="new title", description="new desc", priority="low"
    )
    assert updated.external_id == ref.external_id
    got = provider.get_work_item(ref.external_id)
    if provider.capabilities().priority:
        assert got.priority == "low"
    else:
        # Accepted and ignored - the declared gap (Trello), never a lie.
        assert got.priority is None
    # A description rewrite must not lose the dedup marker.
    assert provider.find_by_incident("INC-1") is not None


def test_update_validates_priority(provider):
    ref = provider.create_work_item(_spec())
    if provider.capabilities().priority:
        with pytest.raises(WorkItemError):
            provider.update_work_item(ref.external_id, priority="banana")


def test_assign_and_clear(provider):
    ref = provider.create_work_item(_spec())
    ref = provider.assign_work_item(ref.external_id, "ona")
    assert (ref.assignee or "").lower() == "ona"
    assert provider.get_work_item(ref.external_id).assignee is not None
    ref = provider.assign_work_item(ref.external_id, None)
    assert ref.assignee is None


def test_comment_and_link(provider):
    first = provider.create_work_item(_spec("INC-1"))
    second = provider.create_work_item(_spec("INC-2"))
    provider.comment_work_item(first.external_id, "investigating")
    provider.link_work_item(first.external_id, second.external_id, link_type="duplicates")


def test_link_validates_vocabulary(provider):
    first = provider.create_work_item(_spec("INC-1"))
    second = provider.create_work_item(_spec("INC-2"))
    with pytest.raises(WorkItemError):
        provider.link_work_item(first.external_id, second.external_id, link_type="teleports")


def test_missing_item_raises(provider):
    """Writes against a nonexistent item raise WorkItemError - the drain
    reconciles from find_by_incident, so providers must not silently
    succeed against nothing."""
    with pytest.raises(WorkItemError):
        provider.update_work_item("missing", title="x")
    with pytest.raises(WorkItemError):
        provider.assign_work_item("missing", "ona")
    with pytest.raises(WorkItemError):
        provider.transition_work_item("missing", "done")
    with pytest.raises(WorkItemError):
        provider.comment_work_item("missing", "hi")
    with pytest.raises(WorkItemError):
        provider.link_work_item("missing", "also-missing")
