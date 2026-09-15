"""Jenkins capability tests: CI fixture console normalization, build
status from captured fields, and live-client failure modes via
httpx.MockTransport."""

import httpx
import pytest
from conftest import CI_FIXTURES, assert_evidence

from integrations.evidence import (
    EvidenceAuthError,
    EvidenceError,
    EvidenceNotFoundError,
    EvidencePartialError,
    EvidenceRateLimitError,
    EvidenceTimeoutError,
)
from integrations.fixtures import FixtureJenkinsEvidence
from integrations.jenkins.client import JenkinsEvidenceClient


def _jenkins(fixture: dict) -> FixtureJenkinsEvidence:
    return FixtureJenkinsEvidence(fixture)


def _client(handler) -> JenkinsEvidenceClient:
    return JenkinsEvidenceClient(
        base_url="http://jenkins.test",
        user="u",
        token="t",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


# --- fixture normalization ------------------------------------------------


@pytest.mark.parametrize("name", CI_FIXTURES)
def test_ci_fixture_console_normalizes(store, name):
    ev = _jenkins(store.load(name)).get_console_log("payment-api", 1)
    assert_evidence(ev, source="jenkins", kind="build_console", origin="fixture")
    assert ev.total == len(ev.payload["lines"]) > 0


@pytest.mark.parametrize("name", CI_FIXTURES)
def test_ci_fixture_build_status_normalizes(store, name):
    doc = store.load(name)
    ev = _jenkins(doc).get_build_status("payment-api", doc["jenkins_build_number"])
    assert_evidence(ev, source="jenkins", kind="build_console", origin="fixture")
    assert ev.payload["result"] == doc["jenkins_result"]
    assert ev.payload["number"] == doc["jenkins_build_number"]
    # Stage names are recovered from the console's [Pipeline] { (name) markers.
    assert len(ev.payload["stages"]) >= 1


def test_docker_build_failure_console_mentions_build_error(store):
    ev = _jenkins(store.load("docker-build-failure")).get_console_log("payment-api", 4)
    joined = "\n".join(ev.payload["lines"])
    assert "ERROR" in joined or "error" in joined.lower()


def test_test_regression_console_mentions_failure(store):
    ev = _jenkins(store.load("test-regression")).get_console_log("payment-api", 2)
    joined = "\n".join(ev.payload["lines"])
    assert "FAIL" in joined or "failed" in joined.lower()


def test_cluster_fixture_has_no_console(store):
    with pytest.raises(EvidenceNotFoundError):
        _jenkins(store.load("crashloop")).get_console_log("payment-api", 1)


def test_fixture_test_results_absent(store):
    with pytest.raises(EvidenceNotFoundError):
        _jenkins(store.load("docker-build-failure")).get_test_results("payment-api", 4)


# --- live client over MockTransport ----------------------------------------


def _routing_handler(routes):
    def handler(request):
        for suffix, response in routes.items():
            if request.url.path.endswith(suffix):
                return response
        return httpx.Response(404, json={"error": "no route"})

    return handler


def test_live_build_status_with_stages():
    handler = _routing_handler(
        {
            "api/json": httpx.Response(
                200,
                json={
                    "number": 4,
                    "result": "FAILURE",
                    "building": False,
                    "url": "http://jenkins/job/payment-api/4",
                    "timestamp": 1,
                    "duration": 1000,
                },
            ),
            "wfapi/describe.json": httpx.Response(
                200,
                json={
                    "stages": [
                        {"name": "Build", "status": "SUCCESS", "durationMillis": 10},
                        {"name": "Dockerize", "status": "FAILED", "durationMillis": 5},
                    ]
                },
            ),
        }
    )
    # wfapi path also ends with .../describe.json - route order matters; the
    # dict is consulted in insertion order and "api/json" must not shadow.
    ev = _client(handler).get_build_status("payment-api", 4)
    assert_evidence(ev, source="jenkins", kind="build_console", origin="live")
    assert ev.payload["result"] == "FAILURE"
    assert ev.payload["stages"][1]["name"] == "Dockerize"


def test_live_build_status_without_wfapi():
    handler = _routing_handler(
        {
            "api/json": httpx.Response(
                200, json={"number": 4, "result": "SUCCESS", "building": False}
            ),
            "describe.json": httpx.Response(404, json={}),
        }
    )
    ev = _client(handler).get_build_status("payment-api", 4)
    assert ev.payload["stages"] == []


def test_live_console_log():
    handler = _routing_handler({"consoleText": httpx.Response(200, text="line one\nline two\n")})
    ev = _client(handler).get_console_log("payment-api", 4)
    assert ev.payload["lines"] == ["line one", "line two"]
    assert ev.total == 2


def test_live_test_results():
    report = {
        "totalCount": 3,
        "failCount": 1,
        "skipCount": 0,
        "suites": [
            {
                "cases": [
                    {"name": "test_ok", "className": "t", "status": "PASSED"},
                    {
                        "name": "test_bad",
                        "className": "t",
                        "status": "FAILED",
                        "errorDetails": "assert 1 == 2",
                    },
                    {"name": "test_ok2", "className": "t", "status": "PASSED"},
                ]
            }
        ],
    }
    handler = _routing_handler({"testReport/api/json": httpx.Response(200, json=report)})
    ev = _client(handler).get_test_results("payment-api", 4)
    assert_evidence(ev, source="jenkins", kind="test_results", origin="live")
    assert ev.payload["total"] == 3
    assert ev.payload["failed"] == 1
    assert ev.payload["passed"] == 2
    assert ev.payload["failures"][0]["name"] == "test_bad"


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
def test_http_error_mapping(status, exc):
    def handler(request):
        return httpx.Response(status, json={})

    with pytest.raises(exc):
        _client(handler).get_console_log("payment-api", 4)


def test_timeout_maps_to_timeout_error():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(EvidenceTimeoutError):
        _client(handler).get_console_log("payment-api", 4)


def test_transport_error_maps_to_evidence_error():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(EvidenceError):
        _client(handler).get_console_log("payment-api", 4)


def test_malformed_build_response_is_partial():
    handler = _routing_handler({"api/json": httpx.Response(200, json={"unexpected": 1})})
    with pytest.raises(EvidencePartialError):
        _client(handler).get_build_status("payment-api", 4)


def test_malformed_test_report_is_partial():
    handler = _routing_handler({"testReport/api/json": httpx.Response(200, json={"unexpected": 1})})
    with pytest.raises(EvidencePartialError):
        _client(handler).get_test_results("payment-api", 4)
