"""HELIX_EVIDENCE_SOURCE selection: the get_*_evidence factories return
live clients or fixture impls per config, and fixture mode requires
HELIX_FIXTURE_PATH."""

import pytest

from integrations import capabilities
from integrations.docker.client import DockerEvidenceClient
from integrations.elk.client import ElkEvidenceClient
from integrations.evidence import EvidenceError
from integrations.fixtures import (
    FixtureDockerEvidence,
    FixtureElkEvidence,
    FixtureGitHubEvidence,
    FixtureJenkinsEvidence,
    FixtureKubernetesEvidence,
    selected_fixture,
)
from integrations.github.client import GitHubEvidenceClient
from integrations.jenkins.client import JenkinsEvidenceClient
from integrations.kubernetes.client import KubernetesEvidenceClient


@pytest.fixture
def fixture_mode(monkeypatch, tmp_path):
    import json

    doc = {"fault": "t", "evidence": {"pods_payment_api": {"items": []}}}
    path = tmp_path / "t.json"
    path.write_text(json.dumps(doc))
    monkeypatch.setenv("HELIX_EVIDENCE_SOURCE", "fixtures")
    monkeypatch.setenv("HELIX_FIXTURE_PATH", str(path))
    return path


def test_factories_return_live_clients_by_default(monkeypatch):
    monkeypatch.delenv("HELIX_EVIDENCE_SOURCE", raising=False)
    monkeypatch.setenv("HELIX_EVIDENCE_SOURCE", "live")
    assert isinstance(capabilities.get_kubernetes_evidence(), KubernetesEvidenceClient)
    assert isinstance(capabilities.get_jenkins_evidence(), JenkinsEvidenceClient)
    assert isinstance(capabilities.get_elk_evidence(), ElkEvidenceClient)
    assert isinstance(capabilities.get_github_evidence(), GitHubEvidenceClient)
    assert isinstance(capabilities.get_docker_evidence(), DockerEvidenceClient)


def test_factories_return_fixture_impls_in_fixture_mode(fixture_mode):
    assert isinstance(capabilities.get_kubernetes_evidence(), FixtureKubernetesEvidence)
    assert isinstance(capabilities.get_jenkins_evidence(), FixtureJenkinsEvidence)
    assert isinstance(capabilities.get_elk_evidence(), FixtureElkEvidence)
    assert isinstance(capabilities.get_github_evidence(), FixtureGitHubEvidence)
    assert isinstance(capabilities.get_docker_evidence(), FixtureDockerEvidence)


def test_fixture_mode_requires_fixture_path(monkeypatch):
    monkeypatch.setenv("HELIX_EVIDENCE_SOURCE", "fixtures")
    monkeypatch.delenv("HELIX_FIXTURE_PATH", raising=False)
    with pytest.raises(EvidenceError, match="HELIX_FIXTURE_PATH"):
        capabilities.get_kubernetes_evidence()


def test_fixture_path_accepts_fault_name(monkeypatch):
    monkeypatch.setenv("HELIX_EVIDENCE_SOURCE", "fixtures")
    monkeypatch.setenv("HELIX_FIXTURE_PATH", "crashloop")
    ev = capabilities.get_kubernetes_evidence().get_pods("payment-api", service="payment-api")
    assert ev.origin == "fixture"


def test_fixture_store_lists_all_faults(store):
    names = store.names()
    assert "cluster/crashloop" in names
    assert "cluster/secret-object" in names
    assert "ci/docker-build-failure" in names
    # 1.11 added the three github/ fault fixtures the benchmark drives.
    assert "github/rejected-pr" in names
    assert "github/failed-check" in names
    assert "github/stale-branch" in names
    # B3 (2026-10-14) added cluster/unregistered-service, declared not
    # captured, for the billing-worker scenario.
    assert "cluster/unregistered-service" in names
    assert len(names) == 15


def test_ambiguous_bare_fault_name_errors(store):
    # dependency-failure exists under both ci/ and cluster/.
    with pytest.raises(EvidenceError, match="ambiguous"):
        store.load("dependency-failure")
    assert store.load("cluster/dependency-failure")["layer"] == "cluster"
    assert store.load("ci/dependency-failure")["layer"] == "ci"


def test_selected_fixture_by_name(monkeypatch):
    from helix.config import get_settings

    monkeypatch.setenv("HELIX_FIXTURE_PATH", "crashloop")
    doc = selected_fixture(get_settings())
    assert doc["fault"] == "crashloop"


def test_the_unregistered_service_scenario_replays_no_other_services_evidence(store):
    """Audit 2026-10-05 (B3): xsys-runtime-missing-owner's event is a
    billing-worker pod failure, but it replayed payment-api's crashloop
    capture, so the LLM arms reasoned over the wrong service. billing-worker
    is not deployed in the sandbox, so its honest evidence is none, declared
    as not captured (decided by Vraj 2026-10-14)."""
    from benchmark import catalog

    scenario = catalog.load_scenarios()["xsys-runtime-missing-owner"]
    fixture = store.load(scenario["evidence_fixture"])
    assert fixture.get("captured") is False
    assert not any("payment_api" in key for key in fixture.get("evidence") or {})
