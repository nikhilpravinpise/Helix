"""SQLAlchemy models for Helix incident state (CONTEXT.md section 13).

Subphase 1.4 landed `incidents` (the canonical active-state record),
`signals` (every raw intake payload attached to an incident) and
`transitions` (the persisted status-change audit written by
state/machine.py). Subphase 1.5 adds `evidence_items` (every collected
Evidence, so the full evidence chain is a DB query), `hypotheses` (the
Diagnosis Agent's recorded claims) and `workflow_runs` (the
single-active-run guard, hardened in 1.10). Subphase 1.6 adds
`candidates` (the full ranked candidate list per planning attempt).
Subphase 1.7 adds `validations` (the Validation Agent's per-candidate
verdicts, whose `fail` rows also serve as the persisted exclusion list
the replan consults). Subphase 1.8 adds `approvals` (the pending/decided
gate decisions that mediate the AWAITING_APPROVAL suspension) and
`executions` (the execution audit record: who, what, when, under which
policy decision). Subphase 1.9 adds `verifications` (the per-attempt
desired-state check record, whose `fail` rows extend the exclusion list
and whose `needs_compensation` flag records the compensation debt 1.10
consumes). Later subphases add their own tables alongside
these - the schema grows with the code that uses it.

Phase 2 (2.1-2.4) adds the change-review lifecycle's own state, extending
rather than replacing the Phase 1 tables above: `Incident.kind`
distinguishes the two lifecycles (state/machine.py has the full
reasoning), `triage_results`/`review_requests` record the Triage and
Review agents' decisions the same way `hypotheses`/`validations` record
the Ops loop's, `work_items`/`work_item_comments` and `knowledge_pages`
hold the provider-neutral projection state CONTEXT.md section 11/13
describes (a `WorkItem` row per (incident, provider), never Jira- or
Linear-specific columns), and `integration_outbox` is the durable
send-queue every external side effect (Slack notify, work-item op, page
sync) goes through - the same outbox pattern `event_records.published_at`
already uses for Kafka, applied here because CONTEXT.md section 8's "Slack
is not the database" holds for Jira and Notion too: the state change and
the intent to notify commit or roll back together, and an integration
outage degrades the send, never the state.

Notes on the choices made here:

- `incidents.fingerprint` is the dedup key computed by state/intake.py; it
  is indexed with `status` because dedup looks for an OPEN incident
  carrying a given fingerprint.
- `status` columns are non-native enums (VARCHAR holding the
  IncidentStatus value). The edge map in machine.py is the authoritative
  enforcement; a DB-level CHECK would just force migration churn when
  Phase 2 statuses arrive.
- `signature`/`raw`/`payload` are JSONB on Postgres (the real store) with
  a plain JSON variant so in-memory SQLite can still host contract tests.
- `linked_pr`/`linked_deployment` are strings: they hold external
  identifiers (PR number or URL, deployment/build id), not local FKs.
- `transitions.from_status` is nullable so the creation record
  (NULL -> received) can live in the same table as real transitions.
- `evidence_items` carries `service`/`truncated`/`total` beyond the
  minimal column list so a stored row reconstructs the Evidence object
  faithfully; `redacted` is omitted because build_evidence guarantees it
  is always True.
"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from state.machine import IncidentStatus


class Base(DeclarativeBase):
    """Declarative base for the incident-state schema."""


def _status_type() -> Enum:
    """IncidentStatus as stored strings ("received", ...). native_enum=False
    keeps it a VARCHAR on Postgres and portable to SQLite; validate_strings
    lets columns accept and return the enum either way."""
    return Enum(
        IncidentStatus,
        native_enum=False,
        validate_strings=True,
        values_callable=lambda e: [m.value for m in e],
        name="incident_status",
        length=32,
    )


def _json_doc() -> JSON:
    """JSONB on Postgres, plain JSON on SQLite (test engines)."""
    return JSONB().with_variant(JSON(), "sqlite")


class IncidentKind(StrEnum):
    """Which lifecycle an incident follows (subphase 2.1).

    `recovery` - a failure signal; the Phase 1 chain (RECEIVED ->
    DIAGNOSING -> ... -> RECOVERED). `change` - a PR/push event; the
    Phase 2 change-review chain (RECEIVED -> TRIAGED -> ... -> DEPLOYING).
    See state/machine.py for how the two chains meet on a failed deploy.
    """

    RECOVERY = "recovery"
    CHANGE = "change"


def _kind_type() -> Enum:
    return Enum(
        IncidentKind,
        native_enum=False,
        validate_strings=True,
        values_callable=lambda e: [m.value for m in e],
        name="incident_kind",
        length=16,
    )


class Incident(Base):
    """The canonical incident record. One row per deduplicated failure."""

    __tablename__ = "incidents"
    __table_args__ = (Index("ix_incidents_fingerprint_status", "fingerprint", "status"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    incident_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    service: Mapped[str] = mapped_column(String(128))
    environment: Mapped[str] = mapped_column(String(32), default="sandbox")
    severity: Mapped[str] = mapped_column(String(32), default="medium")
    status: Mapped[IncidentStatus] = mapped_column(_status_type(), default=IncidentStatus.RECEIVED)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    linked_pr: Mapped[str | None] = mapped_column(String(256))
    linked_deployment: Mapped[str | None] = mapped_column(String(256))
    # Phase 2 (2.1): which lifecycle this incident follows, and the
    # provider-neutral ownership/priority/duplicate fields CONTEXT.md
    # section 11 and 2.5 need. `kind` defaults `recovery` so every Phase 1
    # incident (and every existing row) is unambiguous without a backfill.
    kind: Mapped[IncidentKind] = mapped_column(
        _kind_type(), default=IncidentKind.RECOVERY, server_default=IncidentKind.RECOVERY.value
    )
    owner: Mapped[str | None] = mapped_column(String(128))
    priority: Mapped[str | None] = mapped_column(String(16))
    duplicate_of_id: Mapped[int | None] = mapped_column(
        ForeignKey("incidents.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    duplicate_of: Mapped["Incident | None"] = relationship(remote_side="Incident.id")
    signals: Mapped[list["Signal"]] = relationship(back_populates="incident", order_by="Signal.id")
    transitions: Mapped[list["Transition"]] = relationship(
        back_populates="incident", order_by="Transition.id"
    )
    evidence_items: Mapped[list["EvidenceItem"]] = relationship(
        back_populates="incident", order_by="EvidenceItem.id"
    )
    hypotheses: Mapped[list["Hypothesis"]] = relationship(
        back_populates="incident", order_by="Hypothesis.id"
    )
    workflow_runs: Mapped[list["WorkflowRun"]] = relationship(
        back_populates="incident", order_by="WorkflowRun.id"
    )
    candidates: Mapped[list["Candidate"]] = relationship(
        back_populates="incident", order_by="Candidate.id"
    )
    validations: Mapped[list["Validation"]] = relationship(
        back_populates="incident", order_by="Validation.id"
    )
    approvals: Mapped[list["Approval"]] = relationship(
        back_populates="incident", order_by="Approval.id"
    )
    executions: Mapped[list["Execution"]] = relationship(
        back_populates="incident", order_by="Execution.id"
    )
    verifications: Mapped[list["Verification"]] = relationship(
        back_populates="incident", order_by="Verification.id"
    )
    event_records: Mapped[list["EventRecord"]] = relationship(
        back_populates="incident", order_by="EventRecord.id"
    )
    correlation_tokens: Mapped[list["CorrelationToken"]] = relationship(
        back_populates="incident", order_by="CorrelationToken.id"
    )
    triage_results: Mapped[list["TriageResult"]] = relationship(
        back_populates="incident",
        order_by="TriageResult.id",
        foreign_keys="TriageResult.incident_id",
    )
    review_requests: Mapped[list["ReviewRequest"]] = relationship(
        back_populates="incident", order_by="ReviewRequest.id"
    )
    work_items: Mapped[list["WorkItem"]] = relationship(
        back_populates="incident", order_by="WorkItem.id"
    )
    knowledge_page: Mapped["KnowledgePage | None"] = relationship(back_populates="incident")
    outbox_entries: Mapped[list["IntegrationOutbox"]] = relationship(
        back_populates="incident", order_by="IntegrationOutbox.id"
    )


class Signal(Base):
    """One intake payload attached to an incident. `signature` holds the
    fault-identifying fields the dedup fingerprint is computed over; `raw`
    keeps the full payload for evidence and debugging."""

    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    incident_id: Mapped[int] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), index=True
    )
    source: Mapped[str] = mapped_column(String(32))
    environment: Mapped[str] = mapped_column(String(32))
    severity: Mapped[str] = mapped_column(String(32))
    signature: Mapped[dict[str, Any]] = mapped_column(_json_doc())
    raw: Mapped[dict[str, Any]] = mapped_column(_json_doc())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    incident: Mapped[Incident] = relationship(back_populates="signals")


class Transition(Base):
    """A persisted status change. from_status is NULL only on the creation
    row (NULL -> received) written by machine.record_creation."""

    __tablename__ = "transitions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    incident_id: Mapped[int] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), index=True
    )
    from_status: Mapped[IncidentStatus | None] = mapped_column(_status_type(), nullable=True)
    to_status: Mapped[IncidentStatus] = mapped_column(_status_type())
    cause: Mapped[str] = mapped_column(Text)
    # App-side default (microsecond, tz-aware) in front of the server
    # fallback: func.now() on SQLite stores second-precision strings,
    # and the coordinator's "events newer than this transition" check
    # compares this column against event_records.created_at - the two
    # sides must share precision or same-second ordering goes wrong.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    incident: Mapped[Incident] = relationship(back_populates="transitions")


class EvidenceItem(Base):
    """One collected Evidence persisted for an incident (subphase 1.5).

    `evidence_id` is the content hash (`ev_*`) the evidence layer stamps;
    hypotheses cite it and the 1.9 provenance record resolves it. Ordering
    by `id` replays the collection order - "the full evidence chain is
    reconstructable from stored state" is this table, not a memory
    artifact. Unique per incident so re-collecting identical content is
    idempotent.
    """

    __tablename__ = "evidence_items"
    __table_args__ = (
        Index(
            "ix_evidence_items_incident_evidence",
            "incident_id",
            "evidence_id",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # No single-column index here: the composite unique index's leftmost
    # prefix already serves incident_id lookups.
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"))
    evidence_id: Mapped[str] = mapped_column(String(32))
    source: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(32))
    service: Mapped[str] = mapped_column(String(128))
    summary: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(_json_doc())
    origin: Mapped[str] = mapped_column(String(16))
    truncated: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    total: Mapped[int | None] = mapped_column(Integer)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    incident: Mapped[Incident] = relationship(back_populates="evidence_items")


class Hypothesis(Base):
    """A recorded diagnosis: claim, cited evidence refs, confidence and
    the signals that contradict it. `attempt` is the incident's
    attempt_count at write time, so rediagnosis rows coexist."""

    __tablename__ = "hypotheses"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    incident_id: Mapped[int] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), index=True
    )
    claim: Mapped[str] = mapped_column(Text)
    evidence_refs: Mapped[list[str]] = mapped_column(_json_doc())
    confidence: Mapped[float] = mapped_column(Float)
    contradicting_signals: Mapped[list[str]] = mapped_column(_json_doc())
    attempt: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    incident: Mapped[Incident] = relationship(back_populates="hypotheses")


class Candidate(Base):
    """One ranked recovery candidate from a planning pass (subphase 1.6).

    The planner persists the FULL ranked list per attempt - not just the
    winner - because the 1.9 provenance timeline's "candidates" stage and
    the 1.11 benchmark both need what was considered, not only what was
    picked. `attempt` is the incident's attempt_count at plan time, so
    replan passes (1.10) land as separate ranked lists. `components`
    carries the deterministic ranker's score breakdown (evidence support,
    expected effect, risk penalty, blast radius) - the audit record of
    WHY the ordering came out the way it did. `selected` marks the row
    the coordinator acted on (rank 1 when a selection was made)."""

    __tablename__ = "candidates"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    incident_id: Mapped[int] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), index=True
    )
    attempt: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    rank: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(64))
    params: Mapped[dict[str, Any]] = mapped_column(_json_doc())
    rationale: Mapped[str] = mapped_column(Text)
    evidence_refs: Mapped[list[str]] = mapped_column(_json_doc())
    score: Mapped[float] = mapped_column(Float)
    risk_tier: Mapped[str] = mapped_column(String(16))
    selected: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    components: Mapped[dict[str, Any]] = mapped_column(_json_doc())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    incident: Mapped[Incident] = relationship(back_populates="candidates")


class ValidationOutcome(StrEnum):
    """The Validation Agent's verdict on one candidate (subphase 1.7).

    `not_validatable` is distinct from `pass` on purpose: it means "no
    meaningful pre-check exists" and tells the coordinator to proceed to
    the policy/execution decision WITHOUT having been proven - provenance
    (1.9) must always be able to tell a checked candidate from an
    uncheckable one.
    """

    PASS = "pass"
    FAIL = "fail"
    NOT_VALIDATABLE = "not_validatable"


def _validation_outcome_type() -> Enum:
    """ValidationOutcome as stored strings; same non-native-enum rationale
    as the incident status type."""
    return Enum(
        ValidationOutcome,
        native_enum=False,
        validate_strings=True,
        values_callable=lambda e: [m.value for m in e],
        name="validation_outcome",
        length=16,
    )


class Validation(Base):
    """One validation verdict on a selected candidate (subphase 1.7).

    Written once per validate-node pass: the action + params checked, the
    candidate row they came from (nullable - a run could in principle
    validate a candidate whose row was never persisted), the verdict, the
    human-readable reason, and the strategy's check-by-check `details`.

    `fingerprint` is sha256(action | canonical(params)) - the candidate's
    identity for repeat-action prevention. The `fail` rows ARE the
    exclusion list: intake rebuilds `WorkflowState.excluded_candidates`
    from them so a failed candidate can never be re-selected, even across
    runs. `attempt` tags the incident's attempt_count like candidates do.
    """

    __tablename__ = "validations"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    incident_id: Mapped[int] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), index=True
    )
    candidate_id: Mapped[int | None] = mapped_column(
        ForeignKey("candidates.id", ondelete="SET NULL"), nullable=True
    )
    attempt: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    action: Mapped[str] = mapped_column(String(64))
    params: Mapped[dict[str, Any]] = mapped_column(_json_doc())
    fingerprint: Mapped[str] = mapped_column(String(64))
    outcome: Mapped[ValidationOutcome] = mapped_column(_validation_outcome_type())
    reason: Mapped[str] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(_json_doc())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    incident: Mapped[Incident] = relationship(back_populates="validations")
    candidate: Mapped[Candidate | None] = relationship()


class RunStatus(StrEnum):
    """Workflow-run lifecycle (workflow_runs.status). `suspended` is the
    D13 state: the checkpoint holds the graph state and any process can
    resume the run. `terminal` means the run reached its current end
    state (for 1.5: a hypothesis at remediation_pending; later:
    recovered/escalated)."""

    RUNNING = "running"
    SUSPENDED = "suspended"
    TERMINAL = "terminal"
    FAILED = "failed"


def _run_status_type() -> Enum:
    """RunStatus as stored strings; same non-native-enum rationale as the
    incident status type."""
    return Enum(
        RunStatus,
        native_enum=False,
        validate_strings=True,
        values_callable=lambda e: [m.value for m in e],
        name="run_status",
        length=16,
    )


class WorkflowRun(Base):
    """One invocation of the workflow graph for an incident.

    The single-active-run guard: `start_run` in agent/runner.py refuses
    while a `running` row exists for the incident, and the partial unique
    index `ux_workflow_runs_one_running` (migration 0002) makes that
    enforceable under concurrency - two transactions cannot both insert a
    running row.

    A `running` row whose process was killed outright never reaches
    `finish_run`, so it would otherwise block the guard forever.
    `stale_reason` records why (and by what rule) `helix reclaim`
    (agent/runner.py::reclaim_stale_run, migration 0010) declared such a
    row stale and marked it `failed`; NULL means the row ended normally
    or was never reclaimed.
    """

    __tablename__ = "workflow_runs"
    __table_args__ = (
        # The single-active-run guard's hard backstop: at most one
        # `running` row per incident, enforced by the database itself.
        # Declared here (not only in migration 0002) so create_all-based
        # test databases enforce it too.
        Index(
            "ux_workflow_runs_one_running",
            "incident_id",
            unique=True,
            postgresql_where=text("status = 'running'"),
            sqlite_where=text("status = 'running'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    incident_id: Mapped[int] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[RunStatus] = mapped_column(_run_status_type(), default=RunStatus.RUNNING)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stale_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    incident: Mapped[Incident] = relationship(back_populates="workflow_runs")


class ApprovalStatus(StrEnum):
    """Approval-gate lifecycle (approvals.status). `pending` is the row a
    REQUIRE_APPROVAL verdict parks on; `approved`/`rejected` are the
    recorded decisions that resume the suspended run."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


def _approval_status_type() -> Enum:
    """ApprovalStatus as stored strings; same non-native-enum rationale as
    the incident status type."""
    return Enum(
        ApprovalStatus,
        native_enum=False,
        validate_strings=True,
        values_callable=lambda e: [m.value for m in e],
        name="approval_status",
        length=16,
    )


class Approval(Base):
    """One approval request for a policy-gated action (subphase 1.8).

    Created `pending` by the coordinator in the same transaction as the
    validating -> awaiting_approval transition, so an incident can never
    sit at awaiting_approval without a row to decide. `fingerprint` is
    sha256(action | canonical(params)) - the same identity validation and
    exclusions use - so a granted approval can only ever bless the exact
    (action, params) it was recorded for.

    `decided_by` is the operator identity (`HELIX_OPERATOR` / `--actor` on
    the CLI, `actor` in the API body); `decided_at`/`note` complete the
    audit. A rejection sends the incident back to remediation_pending with
    the fingerprint excluded - these rows are part of the persisted
    exclusion set the planner consults.
    """

    __tablename__ = "approvals"
    __table_args__ = (
        # At most one pending decision per incident - `helix approve` /
        # the API resolve THE pending row, unambiguously.
        Index(
            "ux_approvals_one_pending",
            "incident_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
            sqlite_where=text("status = 'pending'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    incident_id: Mapped[int] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), index=True
    )
    candidate_id: Mapped[int | None] = mapped_column(
        ForeignKey("candidates.id", ondelete="SET NULL"), nullable=True
    )
    action: Mapped[str] = mapped_column(String(64))
    params: Mapped[dict[str, Any]] = mapped_column(_json_doc())
    fingerprint: Mapped[str] = mapped_column(String(64))
    status: Mapped[ApprovalStatus] = mapped_column(
        _approval_status_type(), default=ApprovalStatus.PENDING
    )
    decided_by: Mapped[str | None] = mapped_column(String(128))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    incident: Mapped[Incident] = relationship(back_populates="approvals")
    candidate: Mapped[Candidate | None] = relationship()


class ExecutionResult(StrEnum):
    """How an execution attempt ended (executions.result).

    `success` - the write transport completed. `error` - the transport
    raised/timed out; the action may or may not have landed. `refused` -
    a code-level bound stopped the write before any call (policy DENY, or
    a REQUIRE_APPROVAL action with no granted approvals row): nothing was
    executed, by design."""

    SUCCESS = "success"
    ERROR = "error"
    REFUSED = "refused"


def _execution_result_type() -> Enum:
    """ExecutionResult as stored strings; same non-native-enum rationale."""
    return Enum(
        ExecutionResult,
        native_enum=False,
        validate_strings=True,
        values_callable=lambda e: [m.value for m in e],
        name="execution_result",
        length=16,
    )


def execution_idempotency_key(
    incident_pk: int, fingerprint: str, attempt: int, compensates_id: int | None = None
) -> str:
    """The once-per-logical-write key (subphase 1.10).

    `{incident}:{candidate fingerprint}:{attempt}:{kind}` where kind is
    `remediation` for a planned write or `comp:{failed execution id}` for
    a compensation. Two executions rows with the same key are the same
    write by definition - `ux_executions_idempotency` makes that a
    database guarantee, and the execute/compensate nodes replay an
    existing row's outcome instead of re-firing the transport."""
    kind = f"comp:{compensates_id}" if compensates_id is not None else "remediation"
    return f"{incident_pk}:{fingerprint}:{attempt}:{kind}"


def _default_idempotency_key(context) -> str:
    """Column default computing the key from the row being inserted, so
    EVERY executions row - including test inserts that bypass
    persist_execution - carries the correct key."""
    params = context.get_current_parameters()
    return execution_idempotency_key(
        params["incident_id"],
        params["fingerprint"],
        params["attempt"],
        params.get("compensates_id"),
    )


class Execution(Base):
    """The execution audit record: one row per attempt (subphase 1.8).

    Written for allowed, approved, AND refused actions alike - the audit
    trail covers what was attempted under which policy decision, not just
    what succeeded. `policy_decision` holds the agent/policy.py verdict
    value (`allow`, `allow_with_checks`, `require_approval`, `deny`); a
    `require_approval` row with NULL `approval_id` is by definition an
    unsafe execution - that is exactly what the benchmark's
    executed-without-approval check (D2) scans for.

    `decided_by` copies the approver identity off the approvals row;
    `output` keeps the transport's result payload - for fixture-mode fake
    transports this is the RECORDED intended write, which is what the
    benchmark scores (D10). `origin` says live vs fixture.

    Subphase 1.10 adds two columns:

    - `idempotency_key` - `{incident}:{fingerprint}:{attempt}:{kind}`,
      unique-indexed. A retried/resumed run that would re-record the same
      logical write finds the existing row by key and replays its outcome
      instead of re-firing the transport; the index is the hard backstop.
    - `compensates_id` - self-FK marking the row as a compensation for
      the referenced verification-failed execution (the 1.10 hook). A
      successful row here is what "pays" the needs_compensation debt.
    """

    __tablename__ = "executions"
    __table_args__ = (
        # The once-per-logical-write guarantee's hard backstop - declared
        # here (not only in migration 0007) so create_all-based test
        # databases enforce it too.
        Index("ux_executions_idempotency", "idempotency_key", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    incident_id: Mapped[int] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), index=True
    )
    candidate_id: Mapped[int | None] = mapped_column(
        ForeignKey("candidates.id", ondelete="SET NULL"), nullable=True
    )
    approval_id: Mapped[int | None] = mapped_column(
        ForeignKey("approvals.id", ondelete="SET NULL"), nullable=True
    )
    compensates_id: Mapped[int | None] = mapped_column(
        ForeignKey("executions.id", ondelete="SET NULL"), nullable=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), default=_default_idempotency_key)
    attempt: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    action: Mapped[str] = mapped_column(String(64))
    params: Mapped[dict[str, Any]] = mapped_column(_json_doc())
    fingerprint: Mapped[str] = mapped_column(String(64))
    policy_decision: Mapped[str] = mapped_column(String(32))
    verification_required: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    decided_by: Mapped[str | None] = mapped_column(String(128))
    origin: Mapped[str] = mapped_column(String(16))
    result: Mapped[ExecutionResult] = mapped_column(_execution_result_type())
    detail: Mapped[str] = mapped_column(Text)
    output: Mapped[dict[str, Any] | None] = mapped_column(_json_doc(), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    incident: Mapped[Incident] = relationship(back_populates="executions")
    candidate: Mapped[Candidate | None] = relationship()
    approval: Mapped[Approval | None] = relationship()
    compensates: Mapped["Execution | None"] = relationship(remote_side="Execution.id")


class VerificationOutcome(StrEnum):
    """The Verification Agent's verdict on one executed action (1.9).

    `pass` - every check that ran held desired state across the window;
    the incident may close as RECOVERED. `fail` - a check observed the
    system still broken, flapped mid-window, or could not run (a check
    that cannot run never counts toward recovery). There is deliberately
    no third outcome: per-check `skipped`/`error` detail lives in
    `checks`, while the verdict stays binary so a failed verification can
    never pretend success.
    """

    PASS = "pass"
    FAIL = "fail"


def _verification_outcome_type() -> Enum:
    """VerificationOutcome as stored strings; same non-native-enum rationale."""
    return Enum(
        VerificationOutcome,
        native_enum=False,
        validate_strings=True,
        values_callable=lambda e: [m.value for m in e],
        name="verification_outcome",
        length=16,
    )


class Verification(Base):
    """One post-action verification pass (subphase 1.9).

    Written once per verify-node run: the execution it verified
    (`execution_id`, nullable - a resumed run verifies the newest
    successful executions row), the action + params + fingerprint checked,
    the per-check record (`checks` is a JSONB list of {name, status,
    detail, evidence_id} where status is pass|fail|skipped|error), the
    binary verdict, the stability window sampled, and the window
    timestamps.

    `needs_compensation` marks a failed verification whose compensable
    action left a persistent change un-undone - the compensation hook
    itself is 1.10; this flag plus the row IS the recorded debt.

    `fail` rows double as repeat-action prevention: intake rebuilds the
    exclusion list from them (alongside failed validations, rejected
    approvals and refused/error executions), so a verified-broken action
    is never blindly re-proposed.
    """

    __tablename__ = "verifications"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    incident_id: Mapped[int] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), index=True
    )
    execution_id: Mapped[int | None] = mapped_column(
        ForeignKey("executions.id", ondelete="SET NULL"), nullable=True
    )
    candidate_id: Mapped[int | None] = mapped_column(
        ForeignKey("candidates.id", ondelete="SET NULL"), nullable=True
    )
    attempt: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    action: Mapped[str] = mapped_column(String(64))
    params: Mapped[dict[str, Any]] = mapped_column(_json_doc())
    fingerprint: Mapped[str] = mapped_column(String(64))
    outcome: Mapped[VerificationOutcome] = mapped_column(_verification_outcome_type())
    detail: Mapped[str] = mapped_column(Text)
    checks: Mapped[list[dict[str, Any]]] = mapped_column(_json_doc())
    window_seconds: Mapped[float] = mapped_column(Float)
    poll_seconds: Mapped[float] = mapped_column(Float)
    needs_compensation: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    incident: Mapped[Incident] = relationship(back_populates="verifications")
    execution: Mapped[Execution | None] = relationship()
    candidate: Mapped[Candidate | None] = relationship()


class EventRecord(Base):
    """One canonical event consumed from the stream (subphase E4).

    The unique index on `event_id` is what makes replay a no-op rather
    than a convention: E3 derives event ids from the vendor payload's
    natural key, so a redelivered webhook produces the same id, and the
    index turns "already handled" into a database fact. The consumer looks
    the id up and returns the incident it landed on the first time instead
    of driving state again.

    `envelope` keeps the whole event as received. E5 builds its durable
    history and replay on this column; storing only the fields E4 happens
    to need would mean re-ingesting to get the rest.
    """

    __tablename__ = "event_records"
    __table_args__ = (Index("ux_event_records_event_id", "event_id", unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    incident_id: Mapped[int] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), index=True
    )
    event_id: Mapped[str] = mapped_column(String(64))
    event_type: Mapped[str] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(32))
    service: Mapped[str] = mapped_column(String(128))
    environment: Mapped[str] = mapped_column(String(32))
    #: When the event happened, per its own envelope - distinct from
    #: created_at, which is when Helix consumed it. Out-of-order delivery
    #: makes the two disagree, and both are worth keeping.
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    envelope: Mapped[dict[str, Any]] = mapped_column(_json_doc())
    #: Set once the outbox drain has produced this row to Kafka (E5).
    #: NULL means unpublished - including for every event CONSUMED from
    #: the stream, which Helix never produced and must not claim to have.
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # App-side default for the same reason as transitions.created_at:
    # the coordinator compares this column against it, and second-precision
    # server strings cannot order same-second events correctly.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    incident: Mapped[Incident] = relationship(back_populates="event_records")


class CorrelationToken(Base):
    """One token linking an incident to the identifiers its events carry
    (subphase E4).

    A row per (token, incident) pair. Lookup is by token, which is how an
    arriving event finds the incident an earlier event from a different
    system already opened.
    """

    __tablename__ = "correlation_tokens"
    __table_args__ = (Index("ux_correlation_tokens_pair", "token", "incident_id", unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    incident_id: Mapped[int] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), index=True
    )
    token: Mapped[str] = mapped_column(String(256), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    incident: Mapped[Incident] = relationship(back_populates="correlation_tokens")


class TriageResult(Base):
    """One Triage Agent pass (subphase 2.5): what happened, what is
    affected, and who owns it.

    `duplicate_of_id` is set when this triage matched an existing OPEN
    incident by the same duplicate-detection signature (2.5's "duplicate
    incident detection against event history") - the row records the
    finding even though the coordinator does not open a second incident
    for it. `owner_source` names which resolution path produced `owner`
    (`codeowners` | `registry` | `graph` | `none`) so a later 2.7 graph
    comparison can tell a graph-backed answer from a pre-graph one
    without re-deriving it.
    """

    __tablename__ = "triage_results"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    incident_id: Mapped[int] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), index=True
    )
    attempt: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    affected_service: Mapped[str] = mapped_column(String(128))
    blast_radius: Mapped[str] = mapped_column(String(16))  # low | medium | high
    duplicate_of_id: Mapped[int | None] = mapped_column(
        ForeignKey("incidents.id", ondelete="SET NULL"), nullable=True
    )
    owner: Mapped[str | None] = mapped_column(String(128))
    owner_source: Mapped[str] = mapped_column(String(16))  # codeowners | registry | graph | none
    rationale: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    incident: Mapped[Incident] = relationship(
        back_populates="triage_results", foreign_keys=[incident_id]
    )
    duplicate_of: Mapped[Incident | None] = relationship(foreign_keys=[duplicate_of_id])


class ReviewRequest(Base):
    """One Review Agent request (subphase 2.6): who should review this
    change, and why.

    `status` reuses `ApprovalStatus` (pending/approved/rejected) - the
    review decision is structurally the same gate the 1.8 approval flow
    already models, decided by a human through the same durable-row
    pattern (Slack 2.2 is a delivery surface, never the record of truth).
    `ux_review_requests_one_pending` mirrors the approvals table's
    single-pending-row guard.
    """

    __tablename__ = "review_requests"
    __table_args__ = (
        Index(
            "ux_review_requests_one_pending",
            "incident_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
            sqlite_where=text("status = 'pending'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    incident_id: Mapped[int] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), index=True
    )
    attempt: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    reviewer: Mapped[str] = mapped_column(String(128))
    rationale: Mapped[str] = mapped_column(Text)
    status: Mapped[ApprovalStatus] = mapped_column(
        _approval_status_type(), default=ApprovalStatus.PENDING
    )
    decided_by: Mapped[str | None] = mapped_column(String(128))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    incident: Mapped[Incident] = relationship(back_populates="review_requests")


class WorkItem(Base):
    """One tracked work item projected onto an external provider
    (subphase 2.3): `WorkItemProvider` is `jira` | `linear` | `trello`
    (2.10), never hardcoded to one vendor. Columns are the
    provider-neutral concepts CONTEXT.md section 11 names -
    `status`/`assignee`/`priority`/`project` - so no agent ever reads or
    writes vendor vocabulary.

    `ux_work_items_one_per_provider` is the duplicate-prevention backstop
    (2.3's "duplicate work item prevention", 2.10's "duplicate work item
    rate" metric): at most one work item per (incident, provider).
    """

    __tablename__ = "work_items"
    __table_args__ = (
        Index("ux_work_items_one_per_provider", "incident_id", "provider", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    incident_id: Mapped[int] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(16))  # jira | linear | trello
    external_id: Mapped[str] = mapped_column(String(64))
    external_key: Mapped[str | None] = mapped_column(String(32))  # e.g. "HELIX-123"
    external_url: Mapped[str | None] = mapped_column(String(512))
    title: Mapped[str] = mapped_column(Text)
    # Provider-neutral status (CONTEXT.md section 11): open | in_progress |
    # blocked | done | closed. A provider lacking a state (2.10's Trello
    # study) maps its native list membership onto this vocabulary.
    status: Mapped[str] = mapped_column(String(16))
    assignee: Mapped[str | None] = mapped_column(String(128))
    priority: Mapped[str | None] = mapped_column(String(16))
    project: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    incident: Mapped[Incident] = relationship(back_populates="work_items")
    # delete-orphan matches the declared ondelete="CASCADE" on
    # work_item_comments.work_item_id: an ORM-level session.delete(item)
    # (integrations.reconcile's missing-remote repair) otherwise nulls
    # the FK on flush and trips the NOT NULL constraint.
    comments: Mapped[list["WorkItemComment"]] = relationship(
        back_populates="work_item",
        order_by="WorkItemComment.id",
        cascade="all, delete-orphan",
    )


class WorkItemComment(Base):
    """One `comment_work_item` call recorded (subphase 2.3) - the audit
    trail of what Helix told the tracker, mirroring how `executions`
    audits what Helix told the cluster."""

    __tablename__ = "work_item_comments"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    work_item_id: Mapped[int] = mapped_column(
        ForeignKey("work_items.id", ondelete="CASCADE"), index=True
    )
    body: Mapped[str] = mapped_column(Text)
    author: Mapped[str] = mapped_column(String(128), default="helix")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    work_item: Mapped[WorkItem] = relationship(back_populates="comments")


class KnowledgePage(Base):
    """One Notion incident page (subphase 2.4): a projection of Postgres
    state, never a source of truth (CONTEXT.md section 13). One row per
    incident - `ux_knowledge_pages_one_per_incident` is 2.4's "no
    duplicate pages" exit criterion as a database guarantee."""

    __tablename__ = "knowledge_pages"
    __table_args__ = (Index("ux_knowledge_pages_one_per_incident", "incident_id", unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    incident_id: Mapped[int] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(16), default="notion")
    external_id: Mapped[str] = mapped_column(String(64))
    external_url: Mapped[str | None] = mapped_column(String(512))
    title: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    incident: Mapped[Incident] = relationship(back_populates="knowledge_page")


class OutboxStatus(StrEnum):
    """Delivery state of one `integration_outbox` row."""

    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    #: The integration was not configured at drain time - an explicit
    #: terminal outcome (delivered-to-nowhere), not a retryable failure
    #: and never a fake `sent`.
    SKIPPED = "skipped"


def _outbox_status_type() -> Enum:
    return Enum(
        OutboxStatus,
        native_enum=False,
        validate_strings=True,
        values_callable=lambda e: [m.value for m in e],
        name="outbox_status",
        length=16,
    )


class IntegrationOutbox(Base):
    """The durable send-queue every external side effect goes through
    (subphases 2.2/2.3/2.4): a Slack notification, a work-item create or
    transition, a knowledge-page sync. Written in the SAME transaction as
    the state change that caused it - exactly `event_records`' outbox
    pattern (E5), extended past Kafka to these three integrations because
    the reasoning is identical: "Slack is not the database" (CONTEXT.md
    section 8) means an integration outage must degrade the send, not the
    state change, and the only way to guarantee that is to commit the
    intent to send atomically with the fact that made it true.

    `integration` (`slack` | `jira` | `notion`) and `op` (e.g.
    `notify`, `create_work_item`, `update_work_item`, `comment_work_item`,
    `sync_page`) are plain strings, not DB enums - 2.10 adds providers
    (`linear`, `trello`) without a migration, matching how
    `event_records.source` stays a string for the same reason.
    `attempts`/`last_error` are the retry ledger `integrations.outbox`
    drains against; `sent_at` NULL means still pending or permanently
    failed (`status` tells the two apart).
    """

    __tablename__ = "integration_outbox"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    incident_id: Mapped[int] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), index=True
    )
    integration: Mapped[str] = mapped_column(String(16))
    op: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict[str, Any]] = mapped_column(_json_doc())
    status: Mapped[OutboxStatus] = mapped_column(
        _outbox_status_type(), default=OutboxStatus.PENDING
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    incident: Mapped[Incident] = relationship(back_populates="outbox_entries")


class Connection(Base):
    """A provider account the team connected (future_plans.md Stage 1),
    one row per provider (`slack`, `jira`). `access_token`/`refresh_token`
    hold Fernet ciphertext, never the token: state/connections.py is the
    only code that encrypts and decrypts them. `access_token` is NULL for a
    GitHub App installation, which holds no long-lived credential (its
    installation id is in `settings`). `settings` is the
    provider's small non-secret config (Slack team id and channel, Jira
    cloud id, site url and project key).
    """

    __tablename__ = "connections"

    provider: Mapped[str] = mapped_column(String(16), primary_key=True)
    account: Mapped[str] = mapped_column(String(256))
    scopes: Mapped[list[str]] = mapped_column(_json_doc())
    access_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    refresh_token: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    settings: Mapped[dict[str, Any]] = mapped_column(_json_doc())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PRBotSettings(Base):
    """The pull request bot's settings, a single row (id 1). A NULL column
    means "use the server default" (state/pr_bot.py resolves them): real
    PRs on/off, the repo and base branch the bot targets, whether it opens
    drafts, and an optional daily limit on real PRs.
    """

    __tablename__ = "pr_bot_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    real_prs: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    repo: Mapped[str | None] = mapped_column(String(256), nullable=True)
    base_branch: Mapped[str | None] = mapped_column(String(256), nullable=True)
    draft: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    daily_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Person(Base):
    """Someone on the team, keyed by their GitHub login exactly as given.
    `role` is `owner` (manages everything), `admin` (manages services,
    connections and members) or `member`. `main_owner` marks the one owner
    nobody else can remove or demote (at most one row, by the partial unique
    index). `slack_id` is the Slack member id to @-mention, if linked.
    """

    __tablename__ = "people"
    __table_args__ = (
        CheckConstraint("role IN ('owner', 'admin', 'member')", name="ck_people_role"),
        Index(
            "uq_people_main_owner",
            "main_owner",
            unique=True,
            postgresql_where=text("main_owner"),
            sqlite_where=text("main_owner"),
        ),
    )

    login: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String, default="", server_default="")
    email: Mapped[str | None] = mapped_column(String, nullable=True)
    slack_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    role: Mapped[str] = mapped_column(String(16))
    main_owner: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_signed_in_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class Invitation(Base):
    """An invite to join the team, like a GitHub collaborator invite. `login`
    is kept as the admin typed it; signing in with that GitHub login (any
    case) while the invite is pending accepts it. Pending means not
    accepted, not cancelled and not past `expires_at`.
    """

    __tablename__ = "invitations"
    __table_args__ = (
        CheckConstraint("role IN ('owner', 'admin', 'member')", name="ck_invitations_role"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    login: Mapped[str] = mapped_column(String(128))
    role: Mapped[str] = mapped_column(String(16))
    email: Mapped[str | None] = mapped_column(String, nullable=True)
    slack_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    invited_by: Mapped[str] = mapped_column(String(128))
    token: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: When Helix last emailed the invite link; None if never or it failed.
    emailed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: When Helix last DMed the invite link in Slack; None if never or it failed.
    #: slack_id alone does not say: it is kept even when the DM fails.
    slack_messaged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class Service(Base):
    """A service the team registered: who owns and reviews it, what it
    depends on and its path rules. `path_owners` is an ordered list of
    {"pattern": str, "owners": [str]} - order matters, CODEOWNERS
    last-match-wins (agent/ops/registry.py).
    """

    __tablename__ = "services"

    name: Mapped[str] = mapped_column(String(128), primary_key=True)
    team: Mapped[str] = mapped_column(String, default="", server_default="")
    repo: Mapped[str] = mapped_column(String, default="", server_default="")
    default_owner: Mapped[str | None] = mapped_column(String, nullable=True)
    owners: Mapped[list[str]] = mapped_column(_json_doc(), default=list)
    default_reviewers: Mapped[list[str]] = mapped_column(_json_doc(), default=list)
    depends_on: Mapped[list[str]] = mapped_column(_json_doc(), default=list)
    path_owners: Mapped[list[dict[str, Any]]] = mapped_column(_json_doc(), default=list)
    namespace: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
