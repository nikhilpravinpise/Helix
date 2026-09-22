"""Diagnosis Agent: bounded evidence collection + LLM interpretation.

Flow per run of the node:

1. Deterministic pre-selection by signal source - a kubernetes signal
   collects pods + events + secret_info + service logs; a jenkins signal
   collects console + test_results (+ the build's diff when the signal
   carries a commit sha); a github signal collects commit/diff/check_runs
   when a sha is known. Missing pieces are recorded in `state.errors`,
   never fabricated.
2. A bounded request-more-evidence loop: the LLM sees evidence SUMMARIES
   and may request items from a fixed requestable menu, capped at
   `deps.max_evidence_rounds` (HELIX_MAX_EVIDENCE_ROUNDS) so the
   investigation cannot run away.
3. Interpretation via `complete_structured` into `Hypothesis`. Every
   `evidence_refs` entry must resolve to a collected evidence id - a
   citation of never-collected evidence is a structured-validation
   failure: one re-prompt naming the rejected ids, then the hypothesis is
   recorded anyway with confidence clamped to LOW_CONFIDENCE and its
   dangling refs dropped (full unhappy-path handling lands in 1.10).

Every collected Evidence is persisted to `evidence_items` (deduplicated
by its content-hash id) and the hypothesis to `hypotheses`, so the full
evidence chain is reconstructable from stored state.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field
from sqlalchemy import select

from agent.prompts.diagnosis import diagnosis_prompt, evidence_request_prompt
from agent.retrieval_context import prior_incident_context
from agent.state import Hypothesis, WorkflowState
from helix.config import get_settings
from helix.llm import LLMError
from helix.observability import trace_workflow_step
from integrations.evidence import (
    Evidence,
    EvidenceError,
    EvidenceKind,
    EvidenceSource,
)
from state import models

if TYPE_CHECKING:
    from agent.graph import GraphDeps

#: Confidence ceiling for a hypothesis that still cites dangling refs
#: after its one re-prompt - recorded, but flagged unreliable.
LOW_CONFIDENCE = 0.2

#: Default ELK window for log collection (ES date-math offset).
_DEFAULT_LOG_WINDOW = "1h"


class EvidenceRequest(BaseModel):
    """One request-more-evidence item from the requestable menu.

    `target` is the object the request is about (pod, secret, service,
    job, sha - whatever the kind needs); `detail` is an optional
    qualifier such as a log window or level."""

    source: EvidenceSource
    kind: EvidenceKind
    target: str = ""
    detail: str = ""
    rationale: str = ""


class EvidencePlan(BaseModel):
    """The model's answer to one request-more-evidence round. An empty
    `requests` list means it has enough evidence to interpret."""

    requests: list[EvidenceRequest] = Field(default_factory=list)


@dataclass(frozen=True)
class _SignalContext:
    """What the signal tells collection: where things live."""

    source: str
    service: str
    namespace: str
    job: str | None
    build_number: int | None
    commit_sha: str | None
    # 1.11: github signals may carry the PR number / branch under scrutiny
    # - preselection reads those artifacts alongside commit-derived ones.
    pr_number: int | None
    branch: str | None
    base: str | None


def _signal_context(state: WorkflowState) -> _SignalContext:
    signal = state.get("signal") or {}
    raw = signal.get("raw") or {}
    signature = signal.get("signature") or {}
    service = state.get("service") or raw.get("service") or "unknown"
    namespace = raw.get("namespace") or signature.get("namespace") or service
    job = signature.get("job") or raw.get("job") or service
    raw_build = raw.get("build_number") or signature.get("build_number")
    try:
        build_number = int(raw_build) if raw_build is not None else None
    except (TypeError, ValueError):
        build_number = None
    commit_sha = (
        raw.get("commit")
        or raw.get("sha")
        or raw.get("git_sha")
        or signature.get("commit")
        or signature.get("sha")
        or signature.get("ref")
    )
    raw_pr = signature.get("pr") or raw.get("pr") or signature.get("pr_number")
    try:
        pr_number = int(raw_pr) if raw_pr is not None else None
    except (TypeError, ValueError):
        pr_number = None
    return _SignalContext(
        source=str(signal.get("source") or ""),
        service=service,
        namespace=namespace,
        job=job,
        build_number=build_number,
        commit_sha=commit_sha,
        pr_number=pr_number,
        branch=signature.get("branch") or raw.get("branch"),
        base=signature.get("base") or raw.get("base"),
    )


def _incident_view(state: WorkflowState) -> dict[str, Any]:
    """The incident fields prompts render."""
    return {
        "incident_id": state.get("incident_id"),
        "service": state.get("service"),
        "environment": state.get("environment"),
        "severity": state.get("severity"),
        "signal": state.get("signal") or {},
    }


def _evidence_view(evidence: list[Evidence]) -> list[dict[str, Any]]:
    return [e.model_dump(mode="json") for e in evidence]


def _try_collect(call, errors: list[str], label: str) -> Evidence | None:
    """Run one capability call; failures land in errors, never raise."""
    try:
        return call()
    except EvidenceError as exc:
        errors.append(f"{label}: {exc}")
        return None


def _secret_names(pod_list: Evidence) -> list[str]:
    """Secret names referenced by the pods (env_from secretRef entries
    survive normalization as {"type": "secret", "name": ...})."""
    names: list[str] = []
    for pod in pod_list.payload.get("items") or []:
        for ref in pod.get("env_from") or []:
            if ref.get("type") == "secret" and ref.get("name"):
                names.append(ref["name"])
    return sorted(set(names))


def _preselect(ctx: _SignalContext, deps: "GraphDeps", errors: list[str]) -> list[Evidence]:
    """The deterministic first pass, per signal source."""
    out: list[Evidence] = []

    def add(call, label: str) -> Evidence | None:
        ev = _try_collect(call, errors, label)
        if ev is not None:
            out.append(ev)
        return ev

    if ctx.source == "kubernetes":
        pods = add(
            lambda: deps.kubernetes.get_pods(ctx.namespace, service=ctx.service),
            "pods pre-selection",
        )
        add(lambda: deps.kubernetes.get_events(ctx.namespace), "events pre-selection")
        if pods is not None:
            for name in _secret_names(pods):
                add(
                    lambda name=name: deps.kubernetes.get_secret_info(ctx.namespace, name),
                    f"secret_info pre-selection ({name})",
                )
        add(
            lambda: deps.elk.get_logs(ctx.service, _DEFAULT_LOG_WINDOW),
            "logs pre-selection",
        )
    elif ctx.source == "jenkins":
        if ctx.job and ctx.build_number is not None:
            add(
                lambda: deps.jenkins.get_console_log(ctx.job, ctx.build_number),
                "console pre-selection",
            )
            add(
                lambda: deps.jenkins.get_test_results(ctx.job, ctx.build_number),
                "test_results pre-selection",
            )
        else:
            errors.append("jenkins pre-selection: signal carries no job/build_number")
        if ctx.commit_sha:
            add(lambda: deps.github.get_diff(ctx.commit_sha), "diff pre-selection")
    elif ctx.source == "github":
        if ctx.commit_sha:
            add(lambda: deps.github.get_commit(ctx.commit_sha), "commit pre-selection")
            add(lambda: deps.github.get_diff(ctx.commit_sha), "diff pre-selection")
            add(lambda: deps.github.get_check_runs(ctx.commit_sha), "check_runs pre-selection")
        if ctx.pr_number is not None:
            add(
                lambda: deps.github.get_pull_request(ctx.pr_number),
                "pr_info pre-selection",
            )
            add(
                lambda: deps.github.get_pr_reviews(ctx.pr_number),
                "pr_reviews pre-selection",
            )
        if ctx.branch:
            add(
                lambda: deps.github.get_branch_state(ctx.branch, base=ctx.base),
                "branch_state pre-selection",
            )
        if not (ctx.commit_sha or ctx.pr_number is not None or ctx.branch):
            errors.append("github pre-selection: signal carries no commit sha, pr, or branch")
    else:
        errors.append(f"no pre-selection for signal source {ctx.source or 'unknown'}")
    return out


def _need(value, what: str):
    """A request parameter that must be present; absent means the request
    is unsatisfiable and is recorded as an error by the caller."""
    if value is None or value == "":
        raise EvidenceError(f"request needs a {what}")
    return value


def _requested(req: EvidenceRequest, ctx: _SignalContext, deps: "GraphDeps") -> Evidence:
    """Map one EvidenceRequest onto a concrete capability call. Only the
    (source, kind) pairs offered in the prompt menu are supported."""
    if req.source == EvidenceSource.KUBERNETES:
        if req.kind == EvidenceKind.POD_LIST:
            return deps.kubernetes.get_pods(ctx.namespace, service=req.target or ctx.service)
        if req.kind == EvidenceKind.POD_STATUS:
            return deps.kubernetes.get_pod_status(ctx.namespace, _need(req.target, "pod name"))
        if req.kind == EvidenceKind.EVENT_LIST:
            return deps.kubernetes.get_events(ctx.namespace, involved_object=req.target or None)
        if req.kind == EvidenceKind.SECRET_INFO:
            return deps.kubernetes.get_secret_info(ctx.namespace, _need(req.target, "secret name"))
    elif req.source == EvidenceSource.ELK:
        if req.kind == EvidenceKind.LOG_LINES:
            return deps.elk.get_logs(req.target or ctx.service, req.detail or _DEFAULT_LOG_WINDOW)
    elif req.source == EvidenceSource.JENKINS:
        job = _need(req.target or ctx.job, "job")
        build = _need(int(req.detail) if req.detail.isdigit() else ctx.build_number, "build number")
        if req.kind == EvidenceKind.BUILD_CONSOLE:
            return deps.jenkins.get_console_log(job, build)
        if req.kind == EvidenceKind.TEST_RESULTS:
            return deps.jenkins.get_test_results(job, build)
    elif req.source == EvidenceSource.GITHUB:
        sha = req.target or req.detail or ctx.commit_sha
        if req.kind == EvidenceKind.DIFF:
            return deps.github.get_diff(_need(sha, "commit sha"))
        if req.kind == EvidenceKind.COMMIT:
            return deps.github.get_commit(_need(sha, "commit sha"))
        if req.kind == EvidenceKind.CHECK_RUN:
            return deps.github.get_check_runs(_need(sha, "ref"))
        if req.kind == EvidenceKind.PR_INFO:
            return deps.github.get_pull_request(int(_need(req.target, "PR number")))
    elif req.source == EvidenceSource.DOCKER:
        if req.kind == EvidenceKind.IMAGE_INFO:
            return deps.docker.get_image_info(_need(req.target, "image name"))
        if req.kind == EvidenceKind.CONTAINER_STATE:
            return deps.docker.get_container_state(_need(req.target, "container name"))
    raise EvidenceError(f"unsupported evidence request {req.source}/{req.kind}")


def _interpret(
    deps: "GraphDeps",
    incident: dict[str, Any],
    evidence: list[Evidence],
    errors: list[str],
    prior_context: str = "",
) -> Hypothesis | None:
    """Structured interpretation with the one-shot bad-ref re-prompt.

    `prior_context` is retrieved prior-incident context (E6) and is empty
    unless HELIX_RETRIEVAL_ENABLED is set, so arm E's prompt is unchanged
    by default and retrieval's effect stays something measured rather
    than assumed."""
    known = {e.id for e in evidence}
    view = _evidence_view(evidence)
    try:
        hypothesis = deps.llm.complete_structured(
            diagnosis_prompt(incident, view, prior_context=prior_context), Hypothesis
        )
    except LLMError as exc:
        errors.append(f"diagnosis interpretation failed: {exc}")
        return None

    bad = [ref for ref in hypothesis.evidence_refs if ref not in known]
    if not bad:
        return hypothesis

    # One re-prompt naming the rejected refs; then record what we have,
    # clamped, with dangling refs dropped so every stored ref resolves.
    try:
        retry = deps.llm.complete_structured(
            diagnosis_prompt(incident, view, invalid_refs=bad, prior_context=prior_context),
            Hypothesis,
        )
        bad_retry = [ref for ref in retry.evidence_refs if ref not in known]
        if not bad_retry:
            return retry
        hypothesis, bad = retry, bad_retry
    except LLMError as exc:
        errors.append(f"evidence-refs re-prompt failed: {exc}")
    errors.append(
        f"hypothesis cited never-collected evidence ({', '.join(bad)}); recorded at low confidence"
    )
    hypothesis.evidence_refs = [ref for ref in hypothesis.evidence_refs if ref in known]
    hypothesis.confidence = min(hypothesis.confidence, LOW_CONFIDENCE)
    return hypothesis


def _persist(
    deps: "GraphDeps",
    state: WorkflowState,
    new_evidence: list[Evidence],
    hypothesis: Hypothesis | None,
) -> None:
    """Write evidence_items + hypotheses rows. Evidence dedups on its
    content-hash id (the unique index backstops this); each run's
    hypothesis is a new row tagged with the attempt count."""
    incident_pk = state["incident_pk"]
    with deps.session_scope() as session:
        existing = set(
            session.scalars(
                select(models.EvidenceItem.evidence_id).where(
                    models.EvidenceItem.incident_id == incident_pk
                )
            )
        )
        for ev in new_evidence:
            if ev.id in existing:
                continue
            session.add(
                models.EvidenceItem(
                    incident_id=incident_pk,
                    evidence_id=ev.id,
                    source=ev.source.value,
                    kind=ev.kind.value,
                    service=ev.service,
                    summary=ev.summary,
                    payload=ev.payload,
                    origin=ev.origin.value,
                    truncated=ev.truncated,
                    total=ev.total,
                    collected_at=datetime.fromisoformat(ev.collected_at),
                )
            )
        if hypothesis is not None:
            session.add(
                models.Hypothesis(
                    incident_id=incident_pk,
                    claim=hypothesis.claim,
                    evidence_refs=hypothesis.evidence_refs,
                    confidence=hypothesis.confidence,
                    contradicting_signals=hypothesis.contradicting_signals,
                    attempt=state.get("attempts", 0),
                )
            )


def diagnosis_node(deps: "GraphDeps"):
    """The diagnosis graph node: collect, maybe request more, interpret,
    persist. Writes evidence/hypothesis/errors into WorkflowState; the
    coordinator turns those into transitions."""

    def _diagnosis(state: WorkflowState) -> dict:
        with trace_workflow_step("diagnosis"):
            ctx = _signal_context(state)
            errors: list[str] = list(state.get("errors", []))
            prior = [Evidence.model_validate(e) for e in state.get("evidence", [])]
            known_ids = {e.id for e in prior}
            collected: list[Evidence] = list(prior)
            fresh: list[Evidence] = []

            def absorb(ev: Evidence | None) -> None:
                """Add to the run's evidence set once - content-hash ids make
                re-collecting identical evidence a no-op."""
                if ev is not None and ev.id not in known_ids:
                    known_ids.add(ev.id)
                    collected.append(ev)
                    fresh.append(ev)

            for ev in _preselect(ctx, deps, errors):
                absorb(ev)

            # Bounded request-more-evidence loop.
            rounds = 0
            incident = _incident_view(state)
            while rounds < deps.max_evidence_rounds:
                try:
                    plan = deps.llm.complete_structured(
                        evidence_request_prompt(incident, _evidence_view(collected)),
                        EvidencePlan,
                    )
                except LLMError as exc:
                    errors.append(f"evidence planning round failed: {exc}")
                    break
                if not plan.requests:
                    break
                rounds += 1
                for req in plan.requests:
                    absorb(
                        _try_collect(
                            lambda req=req: _requested(req, ctx, deps),
                            errors,
                            f"evidence request {req.source}/{req.kind}",
                        )
                    )

            prior = prior_incident_context(
                deps, incident, (state.get("signal") or {}).get("signature")
            )
            if get_settings().graph_enabled:
                # 2.8: knowledge context (event timeline + graph
                # neighborhood) augments retrieval when the graph layer is
                # on; include_retrieval=False avoids duplicating the E6
                # block already in `prior`.
                from agent.knowledge_context import knowledge_context

                extra = knowledge_context(
                    deps,
                    incident,
                    (state.get("signal") or {}).get("signature"),
                    include_retrieval=False,
                )
                if extra:
                    prior = f"{prior}\n\n{extra}" if prior else extra
            hypothesis = _interpret(
                deps,
                incident,
                collected,
                errors,
                prior_context=prior,
            )
            _persist(deps, state, fresh, hypothesis)

            return {
                "evidence": _evidence_view(collected),
                "evidence_rounds": rounds,
                "hypothesis": hypothesis.model_dump(mode="json") if hypothesis else None,
                "diagnosis_complete": True,
                "errors": errors,
            }

    return _diagnosis
