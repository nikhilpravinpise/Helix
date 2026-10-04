"""Suite-wide guard: unit tests never reach a developer's live integrations.

Settings read the developer's own .env, so once real credentials went in
(HELIX_WORK_PROVIDER=jira on 2026-10-03), any unit test that drained the
integration outbox delivered to live Jira: one suite run filed ~107 tickets
in a real project and took ten minutes instead of one. Environment
variables outrank .env, so pinning them here routes every unit test to the
recording fakes regardless of what is configured locally. A blank value
means "not set" (helix/config.py), which clears a credential .env holds.

The integration tier is exempt by design - it exists to talk to real
systems - and a test that needs a specific provider still sets it itself
with monkeypatch, which runs after this fixture.
"""

from pathlib import Path

import pytest

#: Every setting that can send traffic to a third-party integration.
_LIVE_INTEGRATIONS = {
    "HELIX_WORK_PROVIDER": "memory",
    "HELIX_JIRA_API_TOKEN": "",
    "HELIX_LINEAR_API_KEY": "",
    "HELIX_TRELLO_API_KEY": "",
    "HELIX_TRELLO_API_TOKEN": "",
    "HELIX_SLACK_BOT_TOKEN": "",
    "HELIX_SLACK_SIGNING_SECRET": "",
    "HELIX_SLACK_APP_TOKEN": "",
    "HELIX_NOTION_API_KEY": "",
    "HELIX_GRAPH_ENABLED": "false",
}


#: The fixed cast tests name (alice, bob, carol). config/service_registry.yaml
#: is the real team and may change; test expectations must not.
_TEST_REGISTRY = Path(__file__).resolve().parents[1] / "benchmark" / "service_registry.yaml"


@pytest.fixture(autouse=True)
def _no_live_integrations(request, monkeypatch):
    monkeypatch.setenv("HELIX_SERVICE_REGISTRY", str(_TEST_REGISTRY))
    if request.node.get_closest_marker("integration") is not None:
        return
    for name, value in _LIVE_INTEGRATIONS.items():
        monkeypatch.setenv(name, value)
