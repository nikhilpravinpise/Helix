"""Kubernetes capability tests: fixture normalization, known per-fault
facts, secret_info metadata-only evidence (D4), and live-client failure
modes via stubbed API objects (no cluster needed)."""

import json

import pytest
import urllib3.exceptions
from conftest import CI_FIXTURES, CLUSTER_FIXTURES, assert_evidence
from kubernetes.client.exceptions import ApiException

from integrations.evidence import (
    EvidenceAuthError,
    EvidenceError,
    EvidenceNotFoundError,
    EvidencePartialError,
    EvidenceRateLimitError,
    EvidenceTimeoutError,
)
from integrations.fixtures import FixtureKubernetesEvidence
from integrations.kubernetes.client import KubernetesEvidenceClient


class _FakeApi:
    """Stands in for CoreV1Api/AppsV1Api: every method returns `result`
    or raises `exc`. Plain dicts pass through sanitize_for_serialization,
    so fixture-shaped dicts are valid fake returns."""

    def __init__(self, result=None, exc=None):
        self._result = result
        self._exc = exc

    def __getattr__(self, name):
        def call(**kwargs):
            if self._exc is not None:
                raise self._exc
            return self._result

        return call


def _kube(fixture: dict) -> FixtureKubernetesEvidence:
    return FixtureKubernetesEvidence(fixture)


# --- fixture normalization -----------------------------------------------


@pytest.mark.parametrize("name", [n for n in CLUSTER_FIXTURES if n != "cluster/secret-object"])
def test_cluster_fixture_pods_normalize(store, name):
    ev = _kube(store.load(name)).get_pods("payment-api", service="payment-api")
    assert_evidence(ev, source="kubernetes", kind="pod_list", origin="fixture")
    assert len(ev.payload["items"]) >= 1
    for pod in ev.payload["items"]:
        assert pod["name"]
        assert pod["phase"] in ("Running", "Pending", "Failed", "Succeeded")


@pytest.mark.parametrize("name", CI_FIXTURES)
def test_ci_fixture_has_no_pods(store, name):
    with pytest.raises(EvidenceNotFoundError):
        _kube(store.load(name)).get_pods("payment-api", service="payment-api")


def test_missing_secret_pod_shows_create_container_config_error(store):
    ev = _kube(store.load("missing-secret")).get_pods("payment-api", service="payment-api")
    waiting_reasons = [
        c["state"]["waiting"]["reason"]
        for p in ev.payload["items"]
        for c in p["containers"]
        if c["state"] and "waiting" in c["state"]
    ]
    assert "CreateContainerConfigError" in waiting_reasons
    # The pod's envFrom reference to the missing secret is visible by name.
    env_from = [e for p in ev.payload["items"] for e in p["env_from"]]
    assert {"type": "secret", "name": "payment-api-secret"} in env_from


def test_crashloop_pod_shows_exit_code_1(store):
    ev = _kube(store.load("crashloop")).get_pods("payment-api", service="payment-api")
    terminated = [
        c["last_terminated"]
        for p in ev.payload["items"]
        for c in p["containers"]
        if c["last_terminated"]
    ]
    assert any(t["exit_code"] == 1 for t in terminated)
    waiting = [
        c["state"].get("waiting", {}).get("reason")
        for p in ev.payload["items"]
        for c in p["containers"]
        if c["state"]
    ]
    assert "CrashLoopBackOff" in waiting


def test_oom_killed_pod_shows_reason_and_exit_137(store):
    ev = _kube(store.load("oom-killed")).get_pods("payment-api", service="payment-api")
    terminated = [
        c["last_terminated"]
        for p in ev.payload["items"]
        for c in p["containers"]
        if c["last_terminated"]
    ]
    assert any(t["reason"] == "OOMKilled" and t["exit_code"] == 137 for t in terminated)


def test_failed_rollout_shows_image_pull_backoff(store):
    ev = _kube(store.load("failed-rollout")).get_pods("payment-api", service="payment-api")
    waiting = [
        c["state"].get("waiting", {}).get("reason")
        for p in ev.payload["items"]
        for c in p["containers"]
        if c["state"]
    ]
    assert "ImagePullBackOff" in waiting


def test_bad_deployment_config_reads_suffixed_pods_key(store):
    # This fixture records pods_payment_api_after instead of
    # pods_payment_api - the prefix lookup must find it.
    ev = _kube(store.load("bad-deployment-config")).get_pods("payment-api", service="payment-api")
    assert_evidence(ev, source="kubernetes", kind="pod_list", origin="fixture")
    assert len(ev.payload["items"]) == 1


@pytest.mark.parametrize("name", CLUSTER_FIXTURES)
def test_cluster_fixture_events_normalize(store, name):
    try:
        ev = _kube(store.load(name)).get_events("payment-api")
    except EvidenceNotFoundError:
        assert name in ("cluster/bad-deployment-config", "cluster/secret-object")
        return
    assert_evidence(ev, source="kubernetes", kind="event_list", origin="fixture")
    assert ev.total == len(ev.payload["items"])


def test_events_involved_object_filter(store):
    ev = _kube(store.load("missing-secret")).get_events(
        "payment-api", involved_object="payment-api-7c886558c8-f29pk"
    )
    assert all(e["involved_name"] == "payment-api-7c886558c8-f29pk" for e in ev.payload["items"])
    assert ev.total >= 1


def test_get_pod_status_by_name(store):
    ev = _kube(store.load("missing-secret")).get_pod_status(
        "payment-api", "payment-api-7c886558c8-f29pk"
    )
    assert_evidence(ev, source="kubernetes", kind="pod_status", origin="fixture")
    assert ev.payload["phase"] == "Pending"
    reasons = [
        c["state"]["waiting"]["reason"]
        for c in ev.payload["containers"]
        if c["state"] and "waiting" in c["state"]
    ]
    assert "CreateContainerConfigError" in reasons


def test_get_pod_status_missing_pod(store):
    with pytest.raises(EvidenceNotFoundError):
        _kube(store.load("missing-secret")).get_pod_status("payment-api", "no-such-pod")


# --- D4: secret_info metadata-only evidence -------------------------------


def test_secret_fixture_yields_secret_info_without_values(store):
    ev = _kube(store.load("secret-object")).get_secret_info("payment-api", "payment-api-secret")
    assert_evidence(ev, source="kubernetes", kind="secret_info", origin="fixture")
    assert ev.payload["name"] == "payment-api-secret"
    assert ev.payload["namespace"] == "payment-api"
    assert ev.payload["keys"] == ["PAYMENT_API_API_KEY"]
    # The base64 value must not survive anywhere in the serialized output.
    dumped = json.dumps(ev.model_dump())
    assert "c2stc2FuZGJveC1wbGFjZWhvbGRlcg==" not in dumped
    assert "sk-sandbox-placeholder" not in dumped


def test_secret_fixture_has_no_pods(store):
    with pytest.raises(EvidenceNotFoundError):
        _kube(store.load("secret-object")).get_pods("payment-api")


def test_secret_info_missing(store):
    with pytest.raises(EvidenceNotFoundError):
        _kube(store.load("missing-secret")).get_secret_info("payment-api", "payment-api-secret")


# --- live client normalization and failure modes (fake API objects) -------


def _pod_doc(name="payment-api-abc"):
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "namespace": "payment-api"},
        "spec": {
            "nodeName": "helix-sandbox-control-plane",
            "containers": [
                {
                    "name": "payment-api",
                    "envFrom": [{"secretRef": {"name": "payment-api-secret"}}],
                    "env": [{"name": "PAYMENT_API_API_KEY", "valueFrom": {"secretKeyRef": {}}}],
                }
            ],
        },
        "status": {
            "phase": "Running",
            "startTime": "2026-09-15T10:00:00Z",
            "containerStatuses": [
                {
                    "name": "payment-api",
                    "ready": True,
                    "restartCount": 0,
                    "state": {"running": {"startedAt": "2026-09-15T10:00:00Z"}},
                }
            ],
        },
    }


def test_live_get_pods_normalizes():
    core = _FakeApi(result={"items": [_pod_doc()]})
    ev = KubernetesEvidenceClient(core_v1=core).get_pods("payment-api", service="payment-api")
    assert_evidence(ev, source="kubernetes", kind="pod_list", origin="live")
    pod = ev.payload["items"][0]
    assert pod["name"] == "payment-api-abc"
    assert pod["ready"] is True
    assert {"type": "secret", "name": "payment-api-secret"} in pod["env_from"]
    # env var value presence is recorded without the value.
    assert pod["env"][0]["value_from"] == "secret"


def test_live_get_secret_info_never_reads_values():
    secret = {
        "kind": "Secret",
        "metadata": {"name": "payment-api-secret", "namespace": "payment-api"},
        "type": "Opaque",
        "data": {"PAYMENT_API_API_KEY": "c2stc2FuZGJveC1wbGFjZWhvbGRlcg=="},
    }
    core = _FakeApi(result=secret)
    ev = KubernetesEvidenceClient(core_v1=core).get_secret_info("payment-api", "payment-api-secret")
    assert_evidence(ev, source="kubernetes", kind="secret_info", origin="live")
    assert ev.payload["keys"] == ["PAYMENT_API_API_KEY"]
    assert "c2stc2FuZGJveC1wbGFjZWhvbGRlcg==" not in json.dumps(ev.model_dump())


def test_live_get_deployment_status():
    dep = {
        "metadata": {"name": "payment-api", "namespace": "payment-api"},
        "spec": {"replicas": 1},
        "status": {
            "readyReplicas": 0,
            "unavailableReplicas": 1,
            "conditions": [
                {"type": "Progressing", "status": "False", "reason": "ProgressDeadlineExceeded"}
            ],
        },
    }
    ev = KubernetesEvidenceClient(apps_v1=_FakeApi(result=dep)).get_deployment_status(
        "payment-api", "payment-api"
    )
    assert_evidence(ev, source="kubernetes", kind="pod_status", origin="live")
    assert ev.payload["conditions"][0]["reason"] == "ProgressDeadlineExceeded"


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
def test_api_exception_mapping(status, exc):
    client = KubernetesEvidenceClient(core_v1=_FakeApi(exc=ApiException(status=status)))
    with pytest.raises(exc):
        client.get_pods("payment-api")


def test_timeout_maps_to_timeout_error():
    exc = urllib3.exceptions.ReadTimeoutError(None, "", "timed out")
    client = KubernetesEvidenceClient(core_v1=_FakeApi(exc=exc))
    with pytest.raises(EvidenceTimeoutError):
        client.get_pods("payment-api")


def test_transport_error_maps_to_evidence_error():
    exc = urllib3.exceptions.MaxRetryError(None, "", None)
    client = KubernetesEvidenceClient(core_v1=_FakeApi(exc=exc))
    with pytest.raises(EvidenceError):
        client.get_pods("payment-api")


def test_malformed_list_response_is_partial():
    client = KubernetesEvidenceClient(core_v1=_FakeApi(result={"unexpected": "shape"}))
    with pytest.raises(EvidencePartialError):
        client.get_pods("payment-api")
