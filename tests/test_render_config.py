"""The hosted demo's render.yaml must configure a runnable fixture replay.

The web service resumes runs in-process after a dashboard decision, and in
fixture mode that needs a recording to replay (HELIX_FIXTURE_PATH). Without
it every incident run on Render raised EvidenceError.
"""

from pathlib import Path

import yaml

from helix.config import Settings
from integrations.fixtures import selected_fixture

RENDER_YAML = Path(__file__).resolve().parents[1] / "render.yaml"


def _literal_web_env() -> dict[str, str]:
    """The web service's envVars that carry a literal `value:`."""
    blueprint = yaml.safe_load(RENDER_YAML.read_text())
    web = next(s for s in blueprint["services"] if s["type"] == "web")
    return {str(e["key"]): str(e["value"]) for e in web["envVars"] if "value" in e}


def test_render_env_resolves_a_fixture(monkeypatch):
    for key, value in _literal_web_env().items():
        monkeypatch.setenv(key, value)
    settings = Settings(_env_file=None)
    assert settings.evidence_source == "fixtures"
    assert selected_fixture(settings)  # raises EvidenceError without HELIX_FIXTURE_PATH
