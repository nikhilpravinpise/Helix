"""Smoke test: the package imports and config loads with defaults."""

import helix
from helix.config import Settings


def test_package_imports():
    assert helix is not None


def test_config_loads_with_defaults(monkeypatch):
    """Defaults only, with the developer's own .env excluded.

    `_env_file=None` matters twice over. Reading .env made this assert
    `llm_api_key is None` against a real configured key, so the test broke
    the moment anyone configured the app - and pytest renders the whole
    Settings object on an assertion failure, which printed the live
    credential in plaintext. A failing test must not be a disclosure
    channel, so nothing here asserts on the key's value at all.
    """
    monkeypatch.delenv("HELIX_ENVIRONMENT", raising=False)
    monkeypatch.delenv("HELIX_LOG_LEVEL", raising=False)
    monkeypatch.delenv("HELIX_LLM_API_KEY", raising=False)
    settings = Settings(_env_file=None)
    assert settings.environment == "development"
    assert settings.log_level == "INFO"
    assert settings.llm_api_key is None
    assert settings.llm_base_url == "https://api.groq.com/openai/v1"
    assert settings.llm_model == "openai/gpt-oss-120b"


def test_a_configured_key_is_never_rendered_by_a_failure(monkeypatch):
    """The disclosure guard, pinned. `repr(Settings)` is what pytest shows
    when an assertion on it fails, and it must not carry the secret."""
    monkeypatch.setenv("HELIX_LLM_API_KEY", "gsk_secret_value_do_not_print")
    rendered = repr(Settings())
    assert "gsk_secret_value_do_not_print" not in rendered
