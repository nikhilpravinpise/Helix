from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Startup configuration, read from environment variables.

    api_key and downstream_url are required with no defaults - a default here
    would silently defeat the missing-Secret and config-mismatch faults.
    """

    model_config = SettingsConfigDict(env_prefix="PAYMENT_API_")

    api_key: str
    downstream_url: str
    ballast_mb: int = 0

    @field_validator("api_key", "downstream_url")
    @classmethod
    def not_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value
