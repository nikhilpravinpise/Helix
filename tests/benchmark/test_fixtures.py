"""GitHub fault fixtures (subphase 1.11): the three crafted docs load
through FixtureStore and feed FixtureGitHubEvidence the normalized keys
the real GitHub client produces."""

import pytest

from integrations.evidence import EvidenceKind, EvidenceOrigin
from integrations.fixtures import FixtureGitHubEvidence, FixtureStore

GITHUB_FAULTS = {
    "github/rejected-pr": {
        "artifacts": ["pr_info", "pr_reviews", "branch_state", "commit", "diff"],
        "check": lambda ev: ev.get_pull_request(17).payload["state"] == "open",
    },
    "github/failed-check": {
        "artifacts": ["check_runs", "commit", "diff"],
        "check": lambda ev: (
            ev.get_check_runs("77aa01c9e2b3f4d5a6b7c8d9e0f1a2b3c4d5e6f7").payload["items"][0][
                "conclusion"
            ]
            == "failure"
        ),
    },
    "github/stale-branch": {
        "artifacts": ["branch_state", "commit", "diff"],
        "check": lambda ev: (
            ev.get_branch_state("feature/timeout-tuning").payload["comparison"] == "diverged"
        ),
    },
}


@pytest.mark.parametrize("fault", sorted(GITHUB_FAULTS))
def test_github_fixture_loads_and_feeds_evidence(store: FixtureStore, fault):
    doc = store.load(fault)
    assert doc["layer"] == "github"
    evidence = FixtureGitHubEvidence(doc)
    for artifact in GITHUB_FAULTS[fault]["artifacts"]:
        assert artifact in doc["evidence"]["github"], f"{fault}: missing {artifact}"
    assert GITHUB_FAULTS[fault]["check"](evidence)


def test_failed_check_fixture_serves_jenkins_recheck(store: FixtureStore):
    """failed-check's rerun_pipeline verification reads the latest Jenkins
    build - the fixture carries jenkins_result=SUCCESS (the world AFTER the
    transient check re-ran)."""
    from integrations.fixtures import FixtureJenkinsEvidence

    doc = store.load("github/failed-check")
    jenkins = FixtureJenkinsEvidence(doc)
    job = jenkins.get_job("payment-api")
    assert job.kind == EvidenceKind.JOB_INFO
    assert job.payload["last_build"] == 5
    build = jenkins.get_build_status("payment-api", job.payload["last_build"])
    assert build.payload["result"] == "SUCCESS"
    assert build.origin == EvidenceOrigin.FIXTURE


def test_rejected_pr_reviews_carry_changes_requested(store: FixtureStore):
    doc = store.load("github/rejected-pr")
    reviews = FixtureGitHubEvidence(doc).get_pr_reviews(17)
    states = [r["state"] for r in reviews.payload["items"]]
    assert "CHANGES_REQUESTED" in states


def test_fixture_evidence_origin_is_fixture(store: FixtureStore):
    doc = store.load("github/stale-branch")
    ev = FixtureGitHubEvidence(doc).get_commit("55cc22dd11ee00ff99aa88bb77cc66dd55ee44ff")
    assert ev.origin == EvidenceOrigin.FIXTURE
