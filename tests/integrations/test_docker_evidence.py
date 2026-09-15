"""Docker capability tests: host-daemon inspect/list via a fake CLI
runner, failure-mode mapping, and the fixture-backed impl. No daemon
needed - `runner` is injected."""

import json
import subprocess

import pytest
from conftest import assert_evidence

from integrations.docker.client import DockerEvidenceClient
from integrations.evidence import (
    EvidenceError,
    EvidenceNotFoundError,
    EvidencePartialError,
    EvidenceTimeoutError,
)
from integrations.fixtures import FixtureDockerEvidence


class _Proc:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _runner(proc=None, exc=None):
    def run(argv, **kwargs):
        if exc is not None:
            raise exc
        return proc

    return run


def _client(proc=None, exc=None) -> DockerEvidenceClient:
    return DockerEvidenceClient(runner=_runner(proc, exc))


_IMAGE = {
    "Id": "sha256:abc123",
    "RepoTags": ["payment-api:kind"],
    "RepoDigests": ["payment-api@sha256:ddd"],
    "Size": 12345,
    "Created": "2026-09-15T10:00:00Z",
}

_CONTAINER = {
    "Id": "cid",
    "Name": "/helix-es",
    "Config": {"Image": "elasticsearch:8"},
    "State": {"Status": "running", "Running": True, "ExitCode": 0, "StartedAt": "t"},
}


def test_get_image_info():
    ev = _client(_Proc(stdout=json.dumps([_IMAGE]))).get_image_info("payment-api:kind")
    assert_evidence(ev, source="docker", kind="image_info", origin="live")
    assert ev.payload["repo_tags"] == ["payment-api:kind"]
    assert ev.payload["id"] == "sha256:abc123"


def test_list_images():
    line = json.dumps({"Repository": "payment-api", "Tag": "kind", "ID": "abc", "Size": "1MB"})
    ev = _client(_Proc(stdout=line + "\n")).list_images()
    assert_evidence(ev, source="docker", kind="image_info", origin="live")
    assert ev.payload["items"][0]["repository"] == "payment-api"
    assert ev.total == 1


def test_get_container_state():
    ev = _client(_Proc(stdout=json.dumps([_CONTAINER]))).get_container_state("helix-es")
    assert_evidence(ev, source="docker", kind="container_state", origin="live")
    assert ev.payload["name"] == "helix-es"
    assert ev.payload["status"] == "running"
    assert ev.payload["image"] == "elasticsearch:8"


def test_list_containers():
    line = json.dumps(
        {"Names": "helix-es", "ID": "cid", "Image": "elasticsearch:8", "State": "running"}
    )
    ev = _client(_Proc(stdout=line + "\n")).list_containers()
    assert_evidence(ev, source="docker", kind="container_state", origin="live")
    assert ev.payload["items"][0]["name"] == "helix-es"


def test_no_such_object_is_not_found():
    proc = _Proc(returncode=1, stderr="Error: No such image: foo:latest")
    with pytest.raises(EvidenceNotFoundError):
        _client(proc).get_image_info("foo:latest")


def test_empty_inspect_is_not_found():
    with pytest.raises(EvidenceNotFoundError):
        _client(_Proc(stdout="[]")).get_image_info("foo:latest")


def test_daemon_down_is_evidence_error():
    proc = _Proc(returncode=1, stderr="Cannot connect to the Docker daemon")
    with pytest.raises(EvidenceError):
        _client(proc).list_containers()


def test_missing_cli_is_evidence_error():
    with pytest.raises(EvidenceError):
        _client(exc=FileNotFoundError("docker")).list_containers()


def test_timeout_is_timeout_error():
    exc = subprocess.TimeoutExpired(cmd="docker", timeout=30)
    with pytest.raises(EvidenceTimeoutError):
        _client(exc=exc).list_containers()


def test_malformed_inspect_json_is_partial():
    with pytest.raises(EvidencePartialError):
        _client(_Proc(stdout="not json")).get_image_info("x")


def test_inspect_missing_fields_is_partial():
    with pytest.raises(EvidencePartialError):
        _client(_Proc(stdout=json.dumps([{"no_id": True}]))).get_image_info("x")
    with pytest.raises(EvidencePartialError):
        _client(_Proc(stdout=json.dumps([{"Name": "/x"}]))).get_container_state("x")


# --- fixture-backed impl ---------------------------------------------------


def test_fixture_docker_reads_artifact():
    fixture = {
        "fault": "synthetic",
        "evidence": {"docker": {"image_info": {"id": "sha256:abc", "name": "payment-api:kind"}}},
    }
    ev = FixtureDockerEvidence(fixture).get_image_info("payment-api:kind")
    assert_evidence(ev, source="docker", kind="image_info", origin="fixture")
    assert ev.payload["id"] == "sha256:abc"


def test_fixture_docker_missing_artifact_raises(store):
    with pytest.raises(EvidenceNotFoundError):
        FixtureDockerEvidence(store.load("crashloop")).get_container_state("helix-es")
