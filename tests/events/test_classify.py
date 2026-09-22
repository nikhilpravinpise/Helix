"""Signal-to-event-type classification (subphase E1).

The load-bearing test here is the catalogue sweep: every scenario Helix
recovers today must get a canonical type, and the type it gets is
asserted explicitly rather than merely being non-null. A classifier that
labels everything `pod.failed` would pass a "does it classify" test and
be useless.
"""

import glob

import pytest
import yaml

from events.classify import UnclassifiedSignal, classify
from events.schemas import EventType

#: The decided mapping, per events/schemas.py. Keyed by scenario name so a
#: failure names the scenario that regressed.
EXPECTED = {
    "crashloop": EventType.POD_FAILED,
    "oom-killed": EventType.POD_FAILED,
    "missing-secret": EventType.POD_FAILED,
    "failed-rollout": EventType.POD_FAILED,
    "config-mismatch": EventType.DEPLOYMENT_FAILED,
    "bad-deployment-config": EventType.DEPLOYMENT_FAILED,
    "ci-test-regression": EventType.BUILD_FAILED,
    "ci-dependency-failure": EventType.BUILD_FAILED,
    "ci-docker-build-failure": EventType.BUILD_FAILED,
    "failed-check": EventType.BUILD_FAILED,
    "rejected-pr": EventType.PR_REJECTED,
    "stale-branch": EventType.BRANCH_DIVERGED,
}


def _scenarios():
    """Signal-path scenarios only. `flow: change` scenarios (Phase 2)
    declare canonical `events:` instead of a raw `signal` - their
    coverage check is that every declared type is a known EventType."""
    out = {}
    for path in sorted(glob.glob("benchmark/scenarios/*.yaml")):
        doc = yaml.safe_load(open(path, encoding="utf-8"))
        if "signal" in doc:
            out[doc["name"]] = doc["signal"]
    return out


def _change_scenarios():
    out = {}
    for path in sorted(glob.glob("benchmark/scenarios/*.yaml")):
        doc = yaml.safe_load(open(path, encoding="utf-8"))
        if doc.get("flow") == "change":
            out[doc["name"]] = doc["events"]
    return out


SCENARIO_SIGNALS = _scenarios()


def test_every_scenario_in_the_catalogue_is_covered():
    """If a scenario is added without a mapping decision, say so here
    rather than discovering it when intake silently returns no event."""
    assert set(SCENARIO_SIGNALS) == set(EXPECTED)


def test_every_change_scenario_event_type_is_canonical():
    """Change-flow scenarios name canonical event types directly - the
    taxonomy check is that every declared type parses as an EventType,
    so a scenario author cannot typo a type the harness then drops."""
    assert _change_scenarios(), "no change-flow scenarios found - the catalogue regressed"
    for name, events in _change_scenarios().items():
        assert events, f"{name}: change-flow scenario declares no events"
        for event in events:
            assert EventType(event["type"]) is not None, (
                f"{name}: event type {event['type']!r} is not in the taxonomy"
            )


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_scenario_classifies_to_its_decided_type(name):
    signal = SCENARIO_SIGNALS[name]
    assert classify(signal["source"], signal.get("signature") or {}) == EXPECTED[name]


class TestKubernetes:
    def test_apply_rejected_is_a_deployment_failure(self):
        """No ReplicaSet and no pod are ever created, so there is nothing
        for pod.failed to describe."""
        assert (
            classify("kubernetes", {"reason": "ApplyRejected", "kind": "Deployment"})
            == EventType.DEPLOYMENT_FAILED
        )

    def test_wedged_rollout_is_a_deployment_failure(self):
        """The old ReplicaSet is still serving, so the service is up and
        the rollout is what failed."""
        assert classify("kubernetes", {"symptom": "rollout_wedged"}) == EventType.DEPLOYMENT_FAILED

    @pytest.mark.parametrize(
        "reason",
        [
            "CrashLoopBackOff",
            "ImagePullBackOff",
            "ErrImagePull",
            "CreateContainerConfigError",
            "InvalidImageName",
        ],
    )
    def test_any_waiting_reason_is_a_pod_failure(self, reason):
        """Keyed on presence, not an enumerated set: the set belongs to
        Kubernetes, and a reason we have not seen is still a pod that did
        not start."""
        assert classify("kubernetes", {"waiting_reason": reason}) == EventType.POD_FAILED

    def test_apply_rejection_wins_over_a_waiting_reason(self):
        """Ordering matters - a rejected apply that somehow also carries a
        stale waiting reason is still a deployment failure."""
        assert (
            classify("kubernetes", {"reason": "ApplyRejected", "waiting_reason": "Pending"})
            == EventType.DEPLOYMENT_FAILED
        )

    def test_empty_waiting_reason_does_not_classify(self):
        """An empty string is absence, not a reason."""
        with pytest.raises(UnclassifiedSignal):
            classify("kubernetes", {"waiting_reason": ""})


class TestGitHub:
    def test_diverged_branch(self):
        """The gap that forced branch.diverged into the taxonomy: the git
        family was push and pull-request events only."""
        assert (
            classify("github", {"type": "branch_state", "comparison": "diverged", "behind_by": 14})
            == EventType.BRANCH_DIVERGED
        )

    def test_a_branch_that_has_not_diverged_does_not_classify(self):
        with pytest.raises(UnclassifiedSignal):
            classify("github", {"type": "branch_state", "comparison": "identical"})

    def test_passing_check_run_does_not_classify_as_a_failure(self):
        """build.failed must not absorb a successful check - intake is
        failure-driven, and a success arriving here is a producer bug."""
        with pytest.raises(UnclassifiedSignal):
            classify("github", {"type": "check_run", "conclusion": "success"})

    def test_approving_review_does_not_classify_as_rejected(self):
        with pytest.raises(UnclassifiedSignal):
            classify("github", {"type": "pull_request_review", "review_state": "APPROVED"})


class TestUnclassified:
    def test_unknown_source(self):
        with pytest.raises(UnclassifiedSignal):
            classify("gitlab", {"anything": "here"})

    def test_empty_signature(self):
        with pytest.raises(UnclassifiedSignal):
            classify("kubernetes", {})

    def test_none_signature_is_tolerated(self):
        with pytest.raises(UnclassifiedSignal):
            classify("kubernetes", None)

    def test_error_names_the_source_and_signature(self):
        """The message is the whole diagnostic - the gap should be visible
        without attaching a debugger."""
        with pytest.raises(UnclassifiedSignal) as exc:
            classify("jenkins", {"job": "payment-api"})  # stage missing
        assert exc.value.source == "jenkins"
        assert exc.value.signature == {"job": "payment-api"}
        assert "jenkins" in str(exc.value)
