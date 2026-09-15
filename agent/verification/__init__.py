"""Desired-state checks for the Verification Agent (subphase 1.9).

"Recovered" differs by action, so checks are per-action desired-state
assertions dispatched on the executed action:

- k8s runtime actions (restart/rollback/scale/reapply_manifest): the
  target's pods are Ready with no bad waiting reasons, the rollout has
  completed, ELK shows the error rate under threshold, and the functional
  leg proves the service actually answers.
- `rerun_pipeline`: the job's latest build is green.
- `open_fix_pr`: the PR exists and carries the intended files - the PR IS
  the remediation for persistent fixes (CONTEXT.md section 3), so no
  runtime leg applies to it.

Two check shapes exist behind the same contract:

- `SampledCheck.check()` returns one `Sample` observation; the framework
  (`sample_check`) repeats it across the stability window.
- a one-shot `OneShotCheck` runs once and returns a `CheckResult`
  directly (PR existence, the port-forward functional leg).

Stability semantics, deliberately explicit: a sampled check passes only
when desired state was observed AND held at every sample after first
observation. Samples before first-healthy are the action's grace period
(a restarted pod takes seconds to come up); a sample that fails AFTER
healthy is flapping and fails the check; never healthy within the window
fails; a check that could not run at all records `error` - none of these
are a pass. `skipped` is recorded when a check is intentionally not run
(fixture mode, disabled functional leg) - never counted as a pass.

Checks see only capability Protocols, `Evidence`, and plain dicts - never
vendor SDK types (C8). The verify node calls `run_verification`; it never
executes remediation actions itself.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from agent.actions.catalogue import ActionName, Reversibility, get_action
from integrations.capabilities import (
    ElkEvidence,
    GitHubEvidence,
    JenkinsEvidence,
    KubernetesEvidence,
)
from integrations.evidence import EvidenceError


class CheckStatus(StrEnum):
    """Per-check result recorded in `verifications.checks`.

    `pass` observed desired state; `fail` observed the system still
    broken (including flapping); `skipped` intentionally not run (fixture
    mode, disabled leg); `error` the check itself could not run (evidence
    error, missing browser, missing port-forward). Only `pass` and
    `skipped` can appear under a `pass` verification outcome - and a
    verification where nothing ran at all is never a pass.
    """

    PASS = "pass"
    FAIL = "fail"
    SKIPPED = "skipped"
    ERROR = "error"


@dataclass(frozen=True)
class CheckResult:
    """One check's verdict for the `verifications.checks` JSONB list."""

    name: str
    status: CheckStatus
    detail: str
    evidence_id: str | None = None  # the Evidence id the verdict rests on

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status.value,
            "detail": self.detail,
            "evidence_id": self.evidence_id,
        }


@dataclass(frozen=True)
class Sample:
    """One observation inside the stability window."""

    status: CheckStatus  # pass | fail | error (skipped ends sampling)
    detail: str
    evidence_id: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == CheckStatus.PASS


class SampledCheck(Protocol):
    """A check sampled every HELIX_VERIFY_POLL across HELIX_VERIFY_WINDOW."""

    name: str

    def check(self) -> Sample:
        """One observation of desired state."""
        ...


#: A one-shot check: zero-arg callable returning its CheckResult.
OneShotCheck = Callable[[], CheckResult]


@dataclass(frozen=True)
class VerificationContext:
    """Everything the checks may consult, in Helix vocabulary only: the
    kubernetes/elk/jenkins/github evidence capabilities, the incident's
    service + namespace, the evidence origin (live|fixture - fixture mode
    verifies against recorded state), the ELK error threshold, the
    functional-leg toggle, and an optional functional-probe seam for
    tests."""

    kubernetes: KubernetesEvidence
    elk: ElkEvidence
    jenkins: JenkinsEvidence
    github: GitHubEvidence
    service: str
    namespace: str
    origin: str  # "live" | "fixture" - mirrors HELIX_EVIDENCE_SOURCE
    functional_enabled: bool
    elk_error_threshold: int
    functional_probe: Callable[["VerificationContext"], CheckResult] | None = None

    @property
    def is_fixture(self) -> bool:
        return self.origin == "fixture"


@dataclass(frozen=True)
class VerificationReport:
    """The full record of one verification pass - what lands in the
    `verifications` row and in `state["verification"]`."""

    action: str
    params: dict[str, Any]
    outcome: str  # "pass" | "fail" (state.models.VerificationOutcome)
    detail: str
    checks: list[CheckResult] = field(default_factory=list)
    window_seconds: float = 0.0
    poll_seconds: float = 0.0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    reversibility: str | None = None
    needs_compensation: bool = False

    def checks_json(self) -> list[dict[str, Any]]:
        return [c.to_dict() for c in self.checks]


def sample_check(
    check: SampledCheck,
    window_s: float,
    poll_s: float,
    sleep: Callable[[float], None],
) -> CheckResult:
    """Sample `check` across the stability window and fold to a verdict.

    Samples at t=0 then every `poll_s` until the window closes. Semantics:

    - pass: desired state observed and held at every sample after first
      observation (early not-yet-healthy samples are the action's grace).
    - fail (flapping): a real negative observation AFTER a healthy one -
      a pod Ready then not-Ready inside the window is not recovered.
    - fail (never healthy): no sample observed desired state.
    - error: every sample failed to run (evidence error), or evidence
      collection broke mid-window after healthy samples - "stability
      could not be confirmed" is not a pass.
    - skipped: the check reported itself not runnable in this mode; the
      window is not spent on it.
    """
    samples = 0
    saw_state = False  # at least one sample produced a real observation
    seen_ok = False
    last = Sample(CheckStatus.ERROR, "no sample taken")
    t = 0.0
    while True:
        try:
            last = check.check()
        except EvidenceError as exc:
            last = Sample(CheckStatus.ERROR, f"check could not run: {exc}")
        except Exception as exc:  # a broken check never counts as a pass
            last = Sample(CheckStatus.ERROR, f"{type(exc).__name__}: {exc}")
        samples += 1
        if last.status == CheckStatus.SKIPPED:
            return CheckResult(check.name, CheckStatus.SKIPPED, last.detail, last.evidence_id)
        if last.status == CheckStatus.ERROR:
            if seen_ok:
                return CheckResult(
                    check.name,
                    CheckStatus.ERROR,
                    f"held for {samples - 1} sample(s) then the check could not "
                    f"run (~{t:.0f}s in): {last.detail}",
                    last.evidence_id,
                )
        elif last.ok:
            saw_state = True
            seen_ok = True
        else:
            saw_state = True
            if seen_ok:
                return CheckResult(
                    check.name,
                    CheckStatus.FAIL,
                    f"flapping: held then failed at sample {samples} (~{t:.0f}s in): {last.detail}",
                    last.evidence_id,
                )
        if t >= window_s:
            break
        # poll_s <= 0 degenerates to "sample at t=0 and at window end".
        step = window_s - t if poll_s <= 0 else min(poll_s, window_s - t)
        sleep(step)
        t += step
    if not seen_ok:
        if not saw_state:
            return CheckResult(
                check.name,
                CheckStatus.ERROR,
                f"check never ran successfully within {window_s:.0f}s: {last.detail}",
                last.evidence_id,
            )
        return CheckResult(
            check.name,
            CheckStatus.FAIL,
            f"desired state not observed within {window_s:.0f}s "
            f"({samples} sample(s)): {last.detail}",
            last.evidence_id,
        )
    return CheckResult(
        check.name,
        CheckStatus.PASS,
        f"held across {window_s:.0f}s ({samples} sample(s)): {last.detail}",
        last.evidence_id,
    )


#: Catalogue actions whose desired state is Kubernetes workload health.
K8S_RUNTIME_ACTIONS: frozenset[str] = frozenset(
    {
        ActionName.RESTART_DEPLOYMENT.value,
        ActionName.ROLLBACK_DEPLOYMENT.value,
        ActionName.SCALE_DEPLOYMENT.value,
        ActionName.REAPPLY_MANIFEST.value,
    }
)


def build_checks(
    ctx: VerificationContext,
    action: str,
    params: dict[str, Any],
    execution_output: dict[str, Any] | None,
    poll_s: float,
) -> tuple[list[SampledCheck], list[OneShotCheck]]:
    """The per-action desired-state legs, as (sampled, one-shot) lists."""
    from agent.verification import functional, pipeline, pull_request, runtime

    if action in K8S_RUNTIME_ACTIONS:
        sampled = runtime.workload_checks(ctx, action, params)
        sampled.append(
            runtime.ElkErrorCheck(
                ctx.elk,
                ctx.service,
                ctx.elk_error_threshold,
                # Each sample answers "is the service still logging errors
                # NOW" - a poll-sized window, so coverage is the whole
                # stability window without double-counting old lines.
                window=f"{max(int(poll_s), 1)}s",
            )
        )
        return sampled, [lambda: functional.run(ctx)]
    if action == ActionName.RERUN_PIPELINE.value:
        return [pipeline.BuildGreenCheck(ctx.jenkins, str(params.get("job") or ""))], []
    if action == ActionName.OPEN_FIX_PR.value:
        return [], [lambda: pull_request.run(ctx, params, execution_output)]
    # A non-catalogue action cannot have executed (the executor refuses),
    # so this is a defensive path - recorded as an un-runnable check.
    return [], [
        lambda: CheckResult(
            "action_support",
            CheckStatus.ERROR,
            f"no desired-state verification is defined for action {action!r}",
        )
    ]


def run_verification(
    ctx: VerificationContext,
    action: str,
    params: dict[str, Any],
    execution_output: dict[str, Any] | None,
    *,
    window_s: float,
    poll_s: float,
    sleep: Callable[[float], None],
) -> VerificationReport:
    """Run every leg for `action` and fold the check list to a verdict.

    Sampled legs run through the stability window first; one-shot legs
    then record their single result. The verdict is `pass` only when at
    least one check ran and every check that ran passed - `error`, `fail`
    and all-skipped all mean NOT recovered.
    """
    started = datetime.now(UTC)
    sampled, oneshots = build_checks(ctx, action, params, execution_output, poll_s)
    checks = [sample_check(c, window_s, poll_s, sleep) for c in sampled]
    checks.extend(run() for run in oneshots)
    ran = [c for c in checks if c.status not in (CheckStatus.SKIPPED,)]
    outcome = "pass" if ran and all(c.status == CheckStatus.PASS for c in ran) else "fail"
    spec = get_action(action)
    finished = datetime.now(UTC)
    detail = "; ".join(f"{c.name}={c.status.value}" for c in checks)
    return VerificationReport(
        action=action,
        params=params,
        outcome=outcome,
        detail=detail,
        checks=checks,
        window_seconds=window_s,
        poll_seconds=poll_s,
        started_at=started,
        finished_at=finished,
        reversibility=spec.reversibility.value if spec else None,
        # A failed verification on a compensable action leaves its
        # persistent change un-undone - the recorded debt 1.10 consumes.
        needs_compensation=bool(
            spec and spec.reversibility == Reversibility.COMPENSABLE and outcome == "fail"
        ),
    )
