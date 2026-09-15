"""Tests for the Evidence model and the error hierarchy."""

import json

import pytest

from integrations.evidence import (
    Evidence,
    EvidenceAuthError,
    EvidenceError,
    EvidenceKind,
    EvidenceNotFoundError,
    EvidenceOrigin,
    EvidencePartialError,
    EvidenceRateLimitError,
    EvidenceSource,
    EvidenceTimeoutError,
    build_evidence,
)


def _make(payload: dict, **kwargs) -> Evidence:
    defaults = {
        "source": EvidenceSource.KUBERNETES,
        "kind": EvidenceKind.POD_LIST,
        "service": "payment-api",
        "summary": "test",
        "origin": EvidenceOrigin.FIXTURE,
    }
    defaults.update(kwargs)
    return Evidence(payload=payload, **defaults)


def test_id_is_stable_content_hash():
    a = _make({"b": 2, "a": 1})
    b = _make({"a": 1, "b": 2})  # same payload, different key order
    c = _make({"a": 1, "b": 3})
    assert a.id == b.id  # canonical payload: key order does not matter
    assert a.id != c.id
    assert a.id.startswith("ev_")
    assert len(a.id) == 19  # "ev_" + 16 hex chars


def test_id_covers_source_and_kind():
    base = _make({"x": 1})
    other_source = _make({"x": 1}, source=EvidenceSource.ELK)
    other_kind = _make({"x": 1}, kind=EvidenceKind.EVENT_LIST)
    assert base.id != other_source.id
    assert base.id != other_kind.id


def test_model_dump_is_json_serializable():
    ev = _make({"nested": {"list": [1, "two", None, True]}})
    json.dumps(ev.model_dump())  # must not raise - no datetime/enum objects leak


def test_kind_enum_matches_spec():
    expected = {
        "pod_status",
        "pod_list",
        "event_list",
        "log_lines",
        "build_console",
        "test_results",
        "commit",
        "diff",
        "pr_info",
        "pr_review",
        "check_run",
        "branch_state",
        "secret_info",
        "image_info",
        "container_state",
    }
    assert {k.value for k in EvidenceKind} == expected


def test_source_and_origin_enums():
    assert {s.value for s in EvidenceSource} == {
        "github",
        "jenkins",
        "kubernetes",
        "docker",
        "elk",
    }
    assert {o.value for o in EvidenceOrigin} == {"live", "fixture"}


def test_error_hierarchy():
    for cls in (
        EvidenceAuthError,
        EvidenceTimeoutError,
        EvidenceRateLimitError,
        EvidenceNotFoundError,
        EvidencePartialError,
    ):
        assert issubclass(cls, EvidenceError)
        assert cls is not EvidenceError


def test_build_evidence_redacts_and_marks():
    ev = build_evidence(
        source=EvidenceSource.ELK,
        kind=EvidenceKind.LOG_LINES,
        service="payment-api",
        summary="Authorization: Bearer sk-sandbox-placeholder seen",
        payload={"lines": ["token=sk-sandbox-placeholder"]},
        origin=EvidenceOrigin.LIVE,
    )
    assert ev.redacted is True
    assert "sk-sandbox-placeholder" not in json.dumps(ev.model_dump())
    assert ev.id.startswith("ev_")


def test_build_evidence_records_truncation():
    ev = build_evidence(
        source=EvidenceSource.ELK,
        kind=EvidenceKind.LOG_LINES,
        service="payment-api",
        summary="s",
        payload={},
        origin=EvidenceOrigin.LIVE,
        truncated=True,
        total=275,
    )
    assert ev.truncated is True
    assert ev.total == 275


@pytest.mark.parametrize(
    "field,value",
    [("source", "vendor-x"), ("kind", "mystery"), ("origin", "tapes")],
)
def test_enum_fields_reject_unknown_values(field, value):
    with pytest.raises(Exception):
        _make({}, **{field: value})
