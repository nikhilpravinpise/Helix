import pytest
from pydantic import ValidationError

from payment_api.config import Settings


def test_settings_load_from_env(monkeypatch):
    monkeypatch.setenv("PAYMENT_API_API_KEY", "secret")
    monkeypatch.setenv("PAYMENT_API_DOWNSTREAM_URL", "http://downstream.example/pay")

    settings = Settings()

    assert settings.api_key == "secret"
    assert settings.downstream_url == "http://downstream.example/pay"
    assert settings.ballast_mb == 0


def test_settings_missing_api_key_fails(monkeypatch):
    monkeypatch.delenv("PAYMENT_API_API_KEY", raising=False)
    monkeypatch.setenv("PAYMENT_API_DOWNSTREAM_URL", "http://downstream.example/pay")

    with pytest.raises(ValidationError):
        Settings()


def test_settings_missing_downstream_url_fails(monkeypatch):
    monkeypatch.setenv("PAYMENT_API_API_KEY", "secret")
    monkeypatch.delenv("PAYMENT_API_DOWNSTREAM_URL", raising=False)

    with pytest.raises(ValidationError):
        Settings()


def test_settings_empty_api_key_fails(monkeypatch):
    monkeypatch.setenv("PAYMENT_API_API_KEY", "  ")
    monkeypatch.setenv("PAYMENT_API_DOWNSTREAM_URL", "http://downstream.example/pay")

    with pytest.raises(ValidationError):
        Settings()
