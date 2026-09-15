"""Application configuration, loaded from environment and an optional .env file.

Secrets (API keys, tokens) are read from environment variables or a local
.env file and must never be committed. See .env.example for the expected
variable names.
"""

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Helix configuration. All fields have safe defaults for local dev."""

    model_config = SettingsConfigDict(env_file=".env", env_prefix="HELIX_", extra="ignore")

    environment: str = "development"
    log_level: str = "INFO"

    # LLM provider (D14): any OpenAI-compatible endpoint. Defaults target
    # Groq's free tier; a local Ollama or another compatible provider works by
    # changing base_url/model only - no provider-specific client code exists.
    llm_api_key: str | None = None
    llm_base_url: str = "https://api.groq.com/openai/v1"
    llm_model: str = "llama-3.3-70b-versatile"

    # Evidence layer (subphase 1.3). evidence_source selects live vendor
    # clients vs fixture-backed implementations; fixture_path names the
    # fixture a fixture-mode run replays (a path to a JSON file or a fault
    # name resolved under fixtures/faults/).
    github_token: str | None = None
    github_repo: str = "nikhilpravinpise/Helix"
    jenkins_url: str = "http://localhost:8080"
    jenkins_user: str = "admin"
    jenkins_token: str = "admin-sandbox-placeholder"
    es_url: str = "http://localhost:9200"
    kube_context: str = "kind-helix-sandbox"
    evidence_source: Literal["live", "fixtures"] = "live"
    fixture_path: str | None = None

    # Incident state + intake (subphase 1.4). database_url points at the
    # host Postgres from infra/postgres-compose.yml (D3); it is a full
    # SQLAlchemy URL so tests can substitute e.g. an in-memory SQLite. The
    # host is literal 127.0.0.1, not "localhost": on Windows localhost
    # resolves IPv6-first and connecting to ::1 stalls instead of being
    # refused, while the compose port is bound to IPv4 loopback only.
    # api_bind/api_port set where `helix serve` listens - 8787 avoids the
    # payment-api (8000), Jenkins (8080) and Elasticsearch (9200) ports.
    # intake_token, when set, requires intake POSTs to carry
    # `Authorization: Bearer <token>`; unset means local-only trust.
    database_url: str = "postgresql+psycopg://helix:helix@127.0.0.1:5432/helix"
    api_bind: str = "127.0.0.1"
    api_port: int = 8787
    intake_token: str | None = None

    # Coordinator + diagnosis (subphase 1.5). max_evidence_rounds bounds the
    # LLM's request-more-evidence loop so an investigation cannot run away.
    # arm selects the ablation arm's graph composition (D8): E is the full
    # graph, B collapses diagnosis+planning into one LLM call and drops
    # validate/verify/replan, A replaces LLM nodes with deterministic rule
    # tables. Arm A/B node variants land in 1.11.
    max_evidence_rounds: int = 3
    arm: Literal["A", "B", "E"] = "E"

    # Safety policy + execution (subphase 1.8). allowed_kube_contexts is the
    # comma-separated allowlist of kube contexts the write client may mutate
    # - enforced in the client itself, so a wrong context is refused even
    # when policy would allow. operator is the default approver identity for
    # CLI approval decisions (`helix approve --actor` overrides; the API
    # takes `actor` in the request body).
    allowed_kube_contexts: str = "kind-helix-sandbox"
    operator: str = "operator"

    def allowed_kube_context_set(self) -> frozenset[str]:
        """HELIX_ALLOWED_KUBE_CONTEXTS parsed into a set of context names."""
        return frozenset(
            name.strip() for name in self.allowed_kube_contexts.split(",") if name.strip()
        )


def get_settings() -> Settings:
    """Load settings from environment and .env. Call sites should not cache
    this across tests, since env vars may change between them."""
    return Settings()
