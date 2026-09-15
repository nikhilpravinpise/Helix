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

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

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
    running row. Full lifecycle hardening (stale-run reclamation) is 1.10.
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
