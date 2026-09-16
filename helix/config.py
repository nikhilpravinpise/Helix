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
    # tables (agent/rules.py - arm A runs with no HELIX_LLM_API_KEY).
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

    # Verification agent (subphase 1.9). verify_window is how many seconds the
    # post-action desired state must hold before an incident may close; the
    # window is sampled every verify_poll seconds, and a pod that was Ready
    # then is not - inside the window - counts as flapping, not recovered.
    # elk_error_threshold is the count of error-level log lines allowed in a
    # poll-sized post-action window (default 0: any fresh ERROR fails the leg).
    # verify_functional toggles the functional leg (browser + API
    # round-trip). Turn it off for headless/CI contexts; it is required in
    # live benchmark mode. A disabled or fixture-mode leg is recorded as
    # skipped, never as passed.
    # verify_base_url overrides HOW the leg reaches the service: unset means
    # `kubectl port-forward svc/<service>` (works on any cluster); set it to
    # an ingress base URL - http://payment-api.local on the kind sandbox -
    # to verify through the same ingress surface operators use (1.13). The
    # name must resolve locally (hosts-file entry) and the request leaves
    # this machine and re-enters through the controller's NodePort.
    verify_window: int = 60
    verify_poll: int = 20
    # Settle budget (seconds): deployment-mutating runtime actions wait for
    # rollout convergence before the stability window opens, so intended pod
    # turnover does not read as flapping.
    verify_settle: int = 120
    elk_error_threshold: int = 0
    verify_functional: bool = True
    verify_base_url: str | None = None

    # Unhappy-path bounds (subphase 1.10). max_attempts bounds the
    # diagnose -> plan -> validate -> execute -> verify loop per incident:
    # every failed cycle consumes one attempt and exhaustion escalates
    # instead of looping forever. approval_timeout is how long a pending
    # approvals row may sit undecided before the incident escalates as
    # approval-timeout (0 disables). step_timeout bounds one graph node's
    # wall-clock inside a run - a node still running at the deadline is
    # recorded as a node failure and the coordinator fails the run
    # (0 disables).
    max_attempts: int = 3
    approval_timeout: float = 86400.0
    step_timeout: float = 600.0

    def allowed_kube_context_set(self) -> frozenset[str]:
        """HELIX_ALLOWED_KUBE_CONTEXTS parsed into a set of context names."""
        return frozenset(
            name.strip() for name in self.allowed_kube_contexts.split(",") if name.strip()
        )


def get_settings() -> Settings:
    """Load settings from environment and .env. Call sites should not cache
    this across tests, since env vars may change between them."""
    return Settings()
