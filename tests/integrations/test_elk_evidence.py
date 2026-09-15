"""ELK capability tests: fixture log normalization, level filtering,
explicit truncation (CF-3), and live-client failure modes via
httpx.MockTransport."""

import httpx
import pytest
from conftest import CLUSTER_FIXTURES, assert_evidence

from integrations.elk.client import ElkEvidenceClient, build_log_query
from integrations.evidence import (
    EvidenceAuthError,
    EvidenceError,
    EvidenceNotFoundError,
    EvidencePartialError,
    EvidenceRateLimitError,
    EvidenceTimeoutError,
)
from integrations.fixtures import FixtureElkEvidence


def _elk(fixture: dict) -> FixtureElkEvidence:
    return FixtureElkEvidence(fixture)


def _client(handler) -> ElkEvidenceClient:
    return ElkEvidenceClient(
        base_url="http://es.test",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def _es_response(lines, total=None, timed_out=False):
    return {
        "timed_out": timed_out,
        "hits": {
            "total": {"value": total if total is not None else len(lines)},
            "hits": [
                {
                    "_source": {
                        "@timestamp": "2026-09-15T10:00:00Z",
                        "kubernetes": {"pod": {"name": "payment-api-x"}},
                        "message": line,
                    }
                }
                for line in lines
            ],
        },
    }


# --- query shape (must mirror scripts/query-logs.sh) ---------------------


def test_query_shape_matches_query_logs_sh():
    q = build_log_query("payment-api", "15m", "error")
    assert q["size"] == 200
    assert q["track_total_hits"] is True
    assert q["sort"] == [{"@timestamp": "asc"}]
    boolq = q["query"]["bool"]
    service_clause = boolq["must"][0]
    assert service_clause["bool"]["minimum_should_match"] == 1
    should = service_clause["bool"]["should"]
    assert {"term": {"kubernetes.labels.app": "payment-api"}} in should
    assert {"term": {"kubernetes.container.name": "payment-api"}} in should
    assert {"match_phrase": {"message": "error"}} in boolq["must"]
    assert boolq["filter"] == [{"range": {"@timestamp": {"gte": "now-15m"}}}]


def test_query_shape_wildcard_service_and_no_level():
    q = build_log_query("*", "1h")
    assert q["query"]["bool"]["must"] == [{"match_all": {}}]
    assert q["query"]["bool"]["filter"] == [{"range": {"@timestamp": {"gte": "now-1h"}}}]


# --- fixture normalization -----------------------------------------------


LOG_CLUSTER_FIXTURES = [
    n
    for n in CLUSTER_FIXTURES
    if n not in ("cluster/bad-deployment-config", "cluster/secret-object")
]


@pytest.mark.parametrize("name", LOG_CLUSTER_FIXTURES)
def test_fixture_logs_normalize(store, name):
    ev = _elk(store.load(name)).get_logs("payment-api", "3m")
    assert_evidence(ev, source="elk", kind="log_lines", origin="fixture")
    assert len(ev.payload["lines"]) > 0
    assert ev.truncated is False
    assert ev.total == len(ev.payload["lines"])
    # line shape matches query-logs.sh output: "<ts> [<pod>] <msg>"
    assert " [payment-api-" in ev.payload["lines"][0]


def test_fixture_logs_level_filter(store):
    ev = _elk(store.load("crashloop")).get_logs("payment-api", "3m", level="error")
    assert all("error" in line.lower() for line in ev.payload["lines"])
    assert ev.total == len(ev.payload["lines"])


@pytest.mark.parametrize(
    "name", ["cluster/bad-deployment-config", "cluster/secret-object", "ci/docker-build-failure"]
)
def test_fixture_missing_service_logs_raises_not_found(store, name):
    with pytest.raises(EvidenceNotFoundError):
        _elk(store.load(name)).get_logs("payment-api", "3m")


def test_fixture_truncation_over_200_lines():
    fixture = {
        "fault": "synthetic",
        "evidence": {"logs_payment_api": [f"line {i}" for i in range(250)]},
    }
    ev = _elk(fixture).get_logs("payment-api", "3m")
    assert ev.truncated is True
    assert ev.total == 250
    assert len(ev.payload["lines"]) == 200


def test_planted_secret_in_log_line_is_redacted():
    fixture = {
        "fault": "synthetic",
        "evidence": {
            "logs_payment_api": [
                "2026-09-15T10:00:00Z [payment-api-x] POST /x "
                "Authorization: Bearer sk-sandbox-placeholder"
            ]
        },
    }
    ev = _elk(fixture).get_logs("payment-api", "3m")
    line = ev.payload["lines"][0]
    assert "sk-sandbox-placeholder" not in line
    assert "Bearer <redacted>" in line


# --- live client over MockTransport --------------------------------------


def test_live_search_normalizes_lines_and_total():
    lines = [f"msg {i}" for i in range(3)]
    captured = {}

    def handler(request):
        captured["body"] = request.read()
        return httpx.Response(200, json=_es_response(lines, total=3))

    ev = _client(handler).get_logs("payment-api", "15m", level="error")
    assert_evidence(ev, source="elk", kind="log_lines", origin="live")
    assert ev.payload["lines"] == [
        "2026-09-15T10:00:00Z [payment-api-x] msg 0",
        "2026-09-15T10:00:00Z [payment-api-x] msg 1",
        "2026-09-15T10:00:00Z [payment-api-x] msg 2",
    ]
    assert ev.truncated is False and ev.total == 3
    assert b"now-15m" in captured["body"]


def test_live_truncated_window_is_explicit():
    lines = [f"msg {i}" for i in range(200)]

    def handler(request):
        return httpx.Response(200, json=_es_response(lines, total=275))

    ev = _client(handler).get_logs("payment-api", "1h")
    assert ev.truncated is True
    assert ev.total == 275
    assert len(ev.payload["lines"]) == 200


def test_live_timed_out_search_is_partial():
    def handler(request):
        return httpx.Response(200, json=_es_response(["a"], timed_out=True))

    with pytest.raises(EvidencePartialError):
        _client(handler).get_logs("payment-api", "1h")


def test_live_missing_hits_block_is_partial():
    def handler(request):
        return httpx.Response(200, json={"acknowledged": True})

    with pytest.raises(EvidencePartialError):
        _client(handler).get_logs("payment-api", "1h")


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
        return httpx.Response(status, json={"error": "x"})

    with pytest.raises(exc):
        _client(handler).get_logs("payment-api", "1h")


def test_timeout_maps_to_timeout_error():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(EvidenceTimeoutError):
        _client(handler).get_logs("payment-api", "1h")


def test_transport_error_maps_to_evidence_error():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(EvidenceError):
        _client(handler).get_logs("payment-api", "1h")
