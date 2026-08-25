"""Application configuration, loaded from environment and an optional .env file.

Secrets (API keys, tokens) are read from environment variables or a local
.env file and must never be committed. See .env.example for the expected
variable names.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Helix configuration. All fields have safe defaults for local dev."""

    model_config = SettingsConfigDict(env_file=".env", env_prefix="HELIX_", extra="ignore")

    environment: str = "development"
    log_level: str = "INFO"
    anthropic_api_key: str | None = None


def get_settings() -> Settings:
    """Load settings from environment and .env. Call sites should not cache
    this across tests, since env vars may change between them."""
    return Settings()
