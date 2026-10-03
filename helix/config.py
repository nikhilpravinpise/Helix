"""Application configuration, loaded from environment and an optional .env file.

Secrets (API keys, tokens) are read from environment variables or a local
.env file and must never be committed. See .env.example for the expected
variable names.
"""

from typing import Any, Literal

from pydantic import Field, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Helix configuration. All fields have safe defaults for local dev."""

    model_config = SettingsConfigDict(env_file=".env", env_prefix="HELIX_", extra="ignore")

    @field_validator("*", mode="before")
    @classmethod
    def _blank_means_unset(cls, value: Any, info: ValidationInfo) -> Any:
        """A blank value is "not set" for any setting whose default is None.

        .env.example ships blank lines, and a blank numeric
        (HELIX_SLACK_APPROVAL_TIMEOUT=) stopped every command at startup.
        Mapping blank to None here, rather than env_ignore_empty, keeps an
        explicit `HELIX_X=""` in the environment working as an override of
        .env - env_ignore_empty would skip it and let .env's value through.
        """
        if value == "" and cls.model_fields[info.field_name].default is None:
            return None
        return value

    environment: str = "development"
    log_level: str = "INFO"

    # LLM provider (D14): any OpenAI-compatible endpoint. Defaults target
    # Groq's free tier; a local Ollama or another compatible provider works by
    # changing base_url/model only - no provider-specific client code exists.
    # repr=False on every credential below. pytest renders the whole
    # Settings object when an assertion on it fails, which printed a live
    # API key in plaintext - a failing test must not be a disclosure
    # channel, and neither must a stack trace or a debug log.
    llm_api_key: str | None = Field(default=None, repr=False)
    llm_base_url: str = "https://api.groq.com/openai/v1"
    llm_model: str = "openai/gpt-oss-120b"

    # Evidence layer (subphase 1.3). evidence_source selects live vendor
    # clients vs fixture-backed implementations; fixture_path names the
    # fixture a fixture-mode run replays (a path to a JSON file or a fault
    # name resolved under fixtures/faults/).
    github_token: str | None = Field(default=None, repr=False)
    github_repo: str = "nikhilpravinpise/Helix"
    jenkins_url: str = "http://localhost:8080"
    jenkins_user: str = "admin"
    jenkins_token: str = Field(default="admin-sandbox-placeholder", repr=False)
    es_url: str = "http://localhost:9200"
    kube_context: str = "kind-helix-sandbox"
    evidence_source: Literal["live", "fixtures"] = "live"
    fixture_path: str | None = None

    # Kafka backbone (subphase E2), infra/kafka-compose.yml. Literal
    # 127.0.0.1 for the same reason as database_url below.
    kafka_bootstrap_servers: str = "127.0.0.1:9092"

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
    intake_token: str | None = Field(default=None, repr=False)

    # Coordinator + diagnosis (subphase 1.5). max_evidence_rounds bounds the
    # LLM's request-more-evidence loop so an investigation cannot run away.
    # arm selects the ablation arm's graph composition (D8): E is the full
    # graph, B collapses diagnosis+planning into one LLM call and drops
    # validate/verify/replan, A replaces LLM nodes with deterministic rule
    # tables (agent/rules.py - arm A runs with no HELIX_LLM_API_KEY).
    # C is B plus retrieval (E6): the same single LLM call, given prior
    # resolved incidents as context, so the ablation isolates what RAG
    # buys over a bare LLM rather than confounding it with verification.
    max_evidence_rounds: int = 3
    arm: Literal["A", "B", "C", "D", "E"] = "E"

    # Retrieval over incident history (subphase E6). OFF by default: the
    # point of the flag is that retrieval's effect on diagnosis is
    # measured rather than assumed, so every arm except C runs without it
    # unless it is turned on deliberately. Arm C enables it regardless.
    retrieval_enabled: bool = False
    retrieval_limit: int = 3

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

    # Orphaned-run recovery. A hard-killed run process (SIGKILL, an OOM
    # kill, a CI/harness subprocess timeout) never reaches finish_run, so
    # its workflow_runs row stays `running` forever and blocks the
    # single-active-run guard. stale_run_timeout is how long such a row
    # may sit `running` before the explicit `helix reclaim` command (never
    # automatic) will declare it stale and mark it `failed`. Conservative
    # by default: comfortably above step_timeout, since a genuinely
    # running incident can occupy several graph nodes in sequence.
    stale_run_timeout: float = 3600.0

    # Relationship graph (subphase 2.7). Off by default so Triage/Review's
    # pre-graph heuristics stay the exit criterion's comparison baseline
    # until deliberately turned on; graph_enabled=true with no reachable
    # Neo4j instance is handled by the same "advisory, never fails a
    # workflow" contract retrieval already follows (E6).
    graph_enabled: bool = False
    neo4j_uri: str = "bolt://127.0.0.1:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = Field(default="neo4j-sandbox-placeholder", repr=False)
    neo4j_database: str = "neo4j"

    # Slack (subphase 2.2). bot_token authenticates outbound calls
    # (chat.postMessage, etc.); signing_secret verifies inbound
    # interaction payloads are genuinely from Slack (D-equivalent of C6
    # for a webhook instead of a log). Both unset means Slack notify/
    # approve is a no-op the outbox records as skipped, never a crash.
    slack_bot_token: str | None = Field(default=None, repr=False)
    slack_signing_secret: str | None = Field(default=None, repr=False)
    slack_default_channel: str = "#helix-incidents"
    # How long an interactive Slack approval may sit undecided before the
    # same escalate-on-timeout contract 1.8's approval_timeout uses
    # applies (0 disables; falls back to approval_timeout when unset).
    slack_approval_timeout: float | None = None

    # Work management (subphase 2.3). provider selects the
    # WorkManagementProvider (2.10 adds linear/trello behind the same
    # contract); jira_* configure the first live implementation.
    work_provider: Literal["jira", "linear", "trello", "memory"] = "memory"
    jira_base_url: str = "https://example.atlassian.net"
    jira_email: str = "helix-bot@example.com"
    jira_api_token: str | None = Field(default=None, repr=False)
    jira_project_key: str = "HELIX"
    linear_api_key: str | None = Field(default=None, repr=False)
    linear_team_id: str = ""
    trello_api_key: str | None = Field(default=None, repr=False)
    trello_api_token: str | None = Field(default=None, repr=False)
    trello_board_id: str = ""

    # Notion (subphase 2.4).
    notion_api_key: str | None = Field(default=None, repr=False)
    notion_parent_page_id: str = ""

    def allowed_kube_context_set(self) -> frozenset[str]:
        """HELIX_ALLOWED_KUBE_CONTEXTS parsed into a set of context names."""
        return frozenset(
            name.strip() for name in self.allowed_kube_contexts.split(",") if name.strip()
        )


def get_settings() -> Settings:
    """Load settings from environment and .env. Call sites should not cache
    this across tests, since env vars may change between them."""
    return Settings()
