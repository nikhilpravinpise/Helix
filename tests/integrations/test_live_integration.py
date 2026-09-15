"""Live-sandbox smoke checks for each capability, marked `integration`
so `uv run pytest` never collects them on a clean machine (D9). These are
the live half of the 1.3 exit criterion: each capability returns
normalized evidence against the running sandbox.

Requires: kind cluster up (scripts/up.sh), ES on :9200, Jenkins on :8080,
and HELIX_* config pointing at them.
"""

import json

import pytest

from integrations import capabilities
from integrations.evidence import Evidence

pytestmark = pytest.mark.integration


def test_kubernetes_live_pods():
    ev = capabilities.get_kubernetes_evidence().get_pods("payment-api", service="payment-api")
    assert isinstance(ev, Evidence)
    assert ev.origin == "live"


def test_kubernetes_live_secret_info_metadata_only():
    ev = capabilities.get_kubernetes_evidence().get_secret_info("payment-api", "payment-api-secret")
    assert ev.kind == "secret_info"
    assert "c2stc2FuZGJveC1wbGFjZWhvbGRlcg==" not in json.dumps(ev.model_dump())


def test_elk_live_logs():
    ev = capabilities.get_elk_evidence().get_logs("payment-api", "24h")
    assert isinstance(ev, Evidence)
    assert ev.kind == "log_lines"


def test_jenkins_live_last_build():
    ev = capabilities.get_jenkins_evidence().get_build_status("payment-api", 1)
    assert isinstance(ev, Evidence)


def test_docker_live_images():
    ev = capabilities.get_docker_evidence().list_images()
    assert isinstance(ev, Evidence)


def test_github_live_branch():
    ev = capabilities.get_github_evidence().get_branch_state("main")
    assert isinstance(ev, Evidence)
