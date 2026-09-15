"""Shared helpers for evidence-layer tests. All unit-tier: no live
infrastructure - fixtures, stubbed transports and fake API objects only."""

import json

import pytest

from integrations.evidence import Evidence
from integrations.fixtures import FIXTURES_ROOT, FixtureStore

CLUSTER_FIXTURES = sorted(f"cluster/{p.stem}" for p in (FIXTURES_ROOT / "cluster").glob("*.json"))
CI_FIXTURES = sorted(f"ci/{p.stem}" for p in (FIXTURES_ROOT / "ci").glob("*.json"))


@pytest.fixture
def store() -> FixtureStore:
    return FixtureStore()


def load_fixture(store: FixtureStore, name: str) -> dict:
    return store.load(name)


def assert_json_safe(value):
    """Recursively assert a value contains only plain JSON types - the
    C8 boundary check: no vendor SDK objects may hide inside a payload."""
    if isinstance(value, dict):
        for k, v in value.items():
            assert isinstance(k, str), f"non-str dict key {k!r}"
            assert_json_safe(v)
    elif isinstance(value, (list, tuple)):
        for item in value:
            assert_json_safe(item)
    else:
        assert value is None or isinstance(value, (str, int, float, bool)), (
            f"non-JSON value leaked: {type(value)}"
        )


def assert_evidence(ev: Evidence, *, source: str, kind: str, origin: str) -> Evidence:
    """The capability boundary contract: an Evidence instance, right
    source/kind/origin, redacted, and fully JSON-serializable."""
    assert isinstance(ev, Evidence)
    assert type(ev).__module__ == "integrations.evidence"
    assert ev.source == source
    assert ev.kind == kind
    assert ev.origin == origin
    assert ev.redacted is True
    assert ev.id.startswith("ev_") and len(ev.id) == 19
    assert_json_safe(ev.model_dump())
    json.dumps(ev.model_dump())  # must not raise
    return ev
