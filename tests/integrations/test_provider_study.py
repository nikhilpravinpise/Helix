"""The 2.10 provider study (plan.md exit criterion).

The identical neutral lifecycle - create, assign, in_progress, comment,
done - runs through every provider over mock transports, and outcomes
are compared in provider-neutral vocabulary (WorkItemRef.status), so
what is proven is the CONTRACT, not a vendor. Differences may exist
only where `capabilities()` declares them: Trello's missing priority
and the no-Blocked-list gap are findings asserted here, and asserting
them is what keeps an undeclared difference a test failure instead of
a production surprise. Zero agent-code changes are required to swap
providers - this suite never names a vendor type; the fixture does.
"""

import pytest

from integrations.work.contract import (
    NEUTRAL_STATUSES,
    ProviderCapabilities,
    WorkItemError,
    WorkItemSpec,
)

#: The study's four arms: three live providers over mocked vendor APIs
#: plus the in-memory baseline.
PROVIDER_NAMES = ["memory", "jira", "linear", "trello"]


def _spec() -> WorkItemSpec:
    return WorkItemSpec(
        incident_id="INC-42",
        title="INC-42: payment-api crashloop",
        description="Helix incident on payment-api.",
        severity="high",
        priority="high",
        labels=["helix"],
    )


@pytest.mark.parametrize("name", PROVIDER_NAMES)
def test_identical_lifecycle_outcomes(provider_factory, name):
    """The same op sequence, four providers, one neutral outcome - the
    exit criterion itself. The expected status sequence is a literal
    shared by every arm, which is what 'identical' means here."""
    provider = provider_factory[name]()
    statuses = []

    ref = provider.create_work_item(_spec())
    statuses.append(ref.status)
    ref = provider.assign_work_item(ref.external_id, "ona")
    statuses.append(ref.status)
    ref = provider.transition_work_item(ref.external_id, "in_progress")
    statuses.append(ref.status)
    provider.comment_work_item(ref.external_id, "investigating")
    ref = provider.transition_work_item(ref.external_id, "done")
    statuses.append(ref.status)

    assert statuses == ["open", "open", "in_progress", "done"]

    # Exactly one item per incident, kept in step to the end (the 2.3
    # exit criterion, proven on every provider).
    found = provider.find_by_incident("INC-42")
    assert found is not None
    assert found.external_id == ref.external_id
    assert found.status == "done"


def test_capability_declarations(provider_factory):
    """The capability maps ARE the study's findings table: memory and
    Jira declare the full vocabulary; Linear and Trello declare the
    honest subset their native models can express on a default
    configuration (no blocked state type, no native priority)."""
    full = ProviderCapabilities()
    assert provider_factory["memory"]().capabilities() == full
    assert provider_factory["jira"]().capabilities() == full

    linear_caps = provider_factory["linear"]().capabilities()
    assert linear_caps == ProviderCapabilities(states=["closed", "done", "in_progress", "open"])

    trello_caps = provider_factory["trello"]().capabilities()
    assert trello_caps == ProviderCapabilities(
        priority=False, states=["closed", "done", "in_progress", "open"]
    )


def test_undeclared_differences_raise_not_approximate(provider_factory):
    """An undeclared difference is a defect in the abstraction: asking a
    provider for a status it did not declare must raise WorkItemError
    rather than degrade silently."""
    for name in ("linear", "trello"):
        provider = provider_factory[name]()
        ref = provider.create_work_item(_spec())
        undeclared = NEUTRAL_STATUSES - set(provider.capabilities().states)
        assert "blocked" in undeclared  # the finding, on both
        for status in undeclared:
            with pytest.raises(WorkItemError):
                provider.transition_work_item(ref.external_id, status)


def test_trello_priority_gap_is_declared_not_dropped(provider_factory):
    """Trello accepts priority in the neutral vocabulary and honestly
    reports it cannot express it - declared in the capability map, so
    callers never have to special-case the vendor."""
    provider = provider_factory["trello"]()
    ref = provider.create_work_item(_spec())
    assert provider.capabilities().priority is False
    assert ref.priority is None
    ref = provider.update_work_item(ref.external_id, priority="low")
    assert ref.priority is None


def test_board_configuration_extends_declared_states(provider_factory):
    """The capability map is a function of what the provider actually
    exposes, not a hardcoded excuse: a Trello board WITH a Blocked list
    declares `blocked` and transitions it."""
    provider = provider_factory["trello"](
        lists=[
            {"id": "l1", "name": "To Do"},
            {"id": "l2", "name": "Doing"},
            {"id": "l3", "name": "Blocked"},
            {"id": "l4", "name": "Done"},
        ]
    )
    assert "blocked" in provider.capabilities().states
    ref = provider.create_work_item(_spec())
    ref = provider.transition_work_item(ref.external_id, "blocked")
    assert ref.status == "blocked"
