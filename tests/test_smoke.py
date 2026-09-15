"""Smoke test: the package imports and config loads with defaults."""

import helix
from helix.config import get_settings


def test_package_imports():
    assert helix is not None


def test_config_loads_with_defaults(monkeypatch):
    monkeypatch.delenv("HELIX_ENVIRONMENT", raising=False)
    monkeypatch.delenv("HELIX_LOG_LEVEL", raising=False)
    monkeypatch.delenv("HELIX_LLM_API_KEY", raising=False)
    settings = get_settings()
    assert settings.environment == "development"
    assert settings.log_level == "INFO"
    assert settings.llm_api_key is None
    assert settings.llm_base_url == "https://api.groq.com/openai/v1"
    assert settings.llm_model == "llama-3.3-70b-versatile"
