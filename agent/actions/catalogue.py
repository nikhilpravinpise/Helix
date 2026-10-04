"""The bounded action catalogue: ActionSpec registry (subphase 1.6).

Every action Helix can take is declared here as an `ActionSpec`: a pydantic
`params_model` that bounds what the planner can express, a `risk_tier`
function OF PARAMS (not a constant - `reapply_manifest` on the Secret
manifest is a secret change rating `high`, on the ConfigMap it rates
`medium`; `open_fix_pr` touching the Secret file rates `high` likewise),
`preconditions` for the prompt and the 1.7 validator, `expected_effect` for
the ranker, and the three-class `reversibility` model 1.10 consumes:

- `stateless`: nothing persistent changed, nothing to undo (restart,
  rerun_pipeline) - failed verification replans directly.
- `compensable`: a declared `compensating_action` undoes the change
  (scale -> scale back; reapply -> apply the previous manifest;
  open_fix_pr -> close the PR; rollback -> roll forward) - compensate,
  then replan.
- `irreversible`: escalate, never retry. Nothing in the Phase 1 catalogue
  is irreversible; the class exists so a later registration must say so.

`validator` and `executor` are string keys the 1.7 validation registry and
the 1.8 executor registry resolve - the catalogue names the binding, the
implementations land with their subphases.

Param bounds are enforced at model validation, so unsafe shapes cannot
even be proposed: `scale_deployment` refuses replicas < 1 (scale-to-0 is
an outage), `reapply_manifest` refuses any path outside the committed
k8s manifest allowlist, `open_fix_pr` refuses branches outside
`helix/INC-*` and files outside `sandbox/payment-api/`.
"""

import posixpath
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator

#: Repo-relative root every controlled GitHub write stays under (D5).
REPO_WRITE_ROOT = "sandbox/payment-api/"

#: The committed Kubernetes manifests `reapply_manifest` may apply - an
#: explicit allowlist, not a directory glob, so a proposal can never name
#: a file that is not one of these seven objects.
MANIFEST_ALLOWLIST: frozenset[str] = frozenset(
    f"sandbox/payment-api/k8s/{name}"
    for name in (
        "00-namespace.yaml",
        "01-downstream-stub.yaml",
        "02-configmap.yaml",
        "03-secret.yaml",
        "04-deployment.yaml",
        "05-service.yaml",
        "06-ingress.yaml",
    )
)

#: Allowlisted manifests whose kind is Secret - applying one is a secret
#: change under the access table (CONTEXT.md section 5), hence `high`.
#: A test asserts this set matches the files' real `kind:` fields.
SECRET_MANIFEST_PATHS: frozenset[str] = frozenset({"sandbox/payment-api/k8s/03-secret.yaml"})

#: Fix branches live only under helix/INC-* (D5: controlled writes to
#: helix/INC-* branches touching sandbox/payment-api/ paths).
_FIX_BRANCH_RE = re.compile(r"^helix/INC-[A-Za-z0-9-]+$")


class RiskTier(StrEnum):
    """Access-table tiers (CONTEXT.md section 5). `high` actions are
    approval-gated by the 1.8 policy engine; nothing is denied by tier
    alone in Phase 1's non-prod sandbox."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Reversibility(StrEnum):
    """What a failed verification means for the executed action (1.10)."""

    STATELESS = "stateless"  # nothing to undo; replan directly
    COMPENSABLE = "compensable"  # run compensating_action, then replan
    IRREVERSIBLE = "irreversible"  # escalate, never retry


class ActionName(StrEnum):
    """The closed set of action names. The planning prompt offers exactly
    these; a proposal naming anything else is dropped before ranking."""

    RESTART_DEPLOYMENT = "restart_deployment"
    ROLLBACK_DEPLOYMENT = "rollback_deployment"
    SCALE_DEPLOYMENT = "scale_deployment"
    REAPPLY_MANIFEST = "reapply_manifest"
    OPEN_FIX_PR = "open_fix_pr"
    RERUN_PIPELINE = "rerun_pipeline"


class DeploymentParams(BaseModel):
    """Target one deployment in one namespace. Params are deliberately NOT
    hardcoded to payment-api - a dependency failure remediates
    downstream-stub, not the demo service."""

    deployment: str = Field(min_length=1, max_length=128)
    namespace: str = Field(min_length=1, max_length=128)


class ScaleDeploymentParams(DeploymentParams):
    """replicas >= 1: scale-to-0 is an outage, so the bound lives in the
    param model itself - the planner cannot even express it."""

    replicas: int = Field(ge=1, le=10)


class ReapplyManifestParams(BaseModel):
    """Apply one committed manifest from the k8s allowlist."""

    path: str = Field(min_length=1, max_length=256)

    @field_validator("path")
    @classmethod
    def _in_allowlist(cls, value: str) -> str:
        normalized = value.replace("\\", "/").lstrip("./")
        if normalized not in MANIFEST_ALLOWLIST:
            raise ValueError(
                f"path {value!r} is not in the manifest allowlist "
                f"(sandbox/payment-api/k8s/ committed files)"
            )
        return normalized


class FileChange(BaseModel):
    """One file in a fix PR: path under the write root plus the FULL new
    content (the planner produces complete contents; 1.7 validates them,
    1.8 writes them via the GitHub API)."""

    path: str = Field(min_length=1, max_length=256)
    content: str = Field(min_length=1)

    @field_validator("path")
    @classmethod
    def _under_write_root(cls, value: str) -> str:
        normalized = value.replace("\\", "/").lstrip("./")
        if ".." in normalized.split("/"):
            raise ValueError(f"path {value!r} may not contain '..'")
        # Collapse `./` and `//` inside the path too. Policy compares the
        # result to the Secret manifest's path as an exact string, and
        # `sandbox/payment-api/./k8s/03-secret.yaml` skipped its approval.
        normalized = posixpath.normpath(normalized)
        if not normalized.startswith(REPO_WRITE_ROOT):
            raise ValueError(f"path {value!r} is outside {REPO_WRITE_ROOT}")
        return normalized


class OpenFixPrParams(BaseModel):
    """Open a fix PR on a helix/INC-* branch (never a protected branch)."""

    branch: str = Field(min_length=1, max_length=128)
    files: list[FileChange] = Field(min_length=1, max_length=16)
    title: str = Field(min_length=1, max_length=256)
    body: str = ""

    @field_validator("branch")
    @classmethod
    def _incident_branch(cls, value: str) -> str:
        if not _FIX_BRANCH_RE.match(value):
            raise ValueError("branch must match helix/INC-*")
        return value


class RerunPipelineParams(BaseModel):
    """Re-run a CI pipeline job by name."""

    job: str = Field(min_length=1, max_length=128)


def _const_tier(tier: RiskTier) -> Callable[[BaseModel], RiskTier]:
    return lambda _params: tier


def _scale_tier(params: BaseModel) -> RiskTier:
    """Restoring a single-replica baseline is low risk; sizing beyond (or
    below-then-back-up) changes capacity materially."""
    replicas = getattr(params, "replicas", 1)
    return RiskTier.LOW if replicas == 1 else RiskTier.MEDIUM


def _reapply_tier(params: BaseModel) -> RiskTier:
    """Applying the Secret manifest writes stringData - a secret change is
    `high` under the access table; other objects are `medium`."""
    if getattr(params, "path", "") in SECRET_MANIFEST_PATHS:
        return RiskTier.HIGH
    return RiskTier.MEDIUM


def _open_fix_pr_tier(params: BaseModel) -> RiskTier:
    """A PR touching the Secret manifest is a secret change even though it
    only lands through review - `high` keeps the access table honest."""
    if any(f.path in SECRET_MANIFEST_PATHS for f in getattr(params, "files", [])):
        return RiskTier.HIGH
    return RiskTier.MEDIUM


@dataclass(frozen=True)
class ActionSpec:
    """One catalogue entry. `risk_tier` is a FUNCTION of the validated
    params, so a param-dependent rating is impossible to misdeclare.
    `compensating_action` names the undo operation (required iff
    `compensable`); it may name another catalogue action (scale ->
    scale_deployment again) or an executor-side operation the planner can
    never propose (open_fix_pr -> `close_pr`, wired in 1.8/1.10).

    `blast_radius` is a coarse 0..1 spread estimate the deterministic
    ranker penalizes; `params_hint` is the one-line param description the
    planning prompt renders."""

    name: str
    params_model: type[BaseModel]
    risk_tier: Callable[[BaseModel], RiskTier]
    preconditions: tuple[str, ...]
    expected_effect: str
    reversibility: Reversibility
    compensating_action: str | None
    validator: str
    executor: str
    blast_radius: float
    params_hint: str


_REGISTRY: dict[str, ActionSpec] = {}


def register_action(spec: ActionSpec) -> ActionSpec:
    """Add `spec` to the catalogue, enforcing registry integrity:
    a compensable action without a compensating_action - or a
    stateless/irreversible action WITH one - is a registration bug, not a
    runtime condition."""
    if spec.reversibility == Reversibility.COMPENSABLE:
        if not spec.compensating_action:
            raise ValueError(f"{spec.name}: compensable actions must declare compensating_action")
    elif spec.compensating_action is not None:
        raise ValueError(
            f"{spec.name}: compensating_action is only meaningful for compensable actions"
        )
    if spec.name in _REGISTRY:
        raise ValueError(f"action {spec.name!r} is already registered")
    _REGISTRY[spec.name] = spec
    return spec


def get_action(name: str) -> ActionSpec | None:
    """The spec for `name`, or None when the proposal named something the
    catalogue does not contain."""
    return _REGISTRY.get(name)


def all_actions() -> tuple[ActionSpec, ...]:
    """Every registered spec, in registration order."""
    return tuple(_REGISTRY.values())


def catalogue_view() -> str:
    """The catalogue rendered for the planning prompt: name, params,
    tier, reversibility, effect, preconditions - derived from the
    registered specs so the menu cannot drift from the registry."""
    lines = []
    for spec in _REGISTRY.values():
        comp = (
            f"compensable via {spec.compensating_action}"
            if spec.reversibility == Reversibility.COMPENSABLE
            else spec.reversibility.value
        )
        lines.append(
            f"- {spec.name}: params {{{spec.params_hint}}}; "
            f"reversibility {comp}; effect: {spec.expected_effect}"
        )
        for pre in spec.preconditions:
            lines.append(f"    requires: {pre}")
    return "\n".join(lines)


register_action(
    ActionSpec(
        name=ActionName.RESTART_DEPLOYMENT.value,
        params_model=DeploymentParams,
        risk_tier=_const_tier(RiskTier.LOW),
        preconditions=("target deployment exists in the namespace",),
        expected_effect="rolls the deployment's pods; transient workload faults clear, "
        "persistent config/content defects re-fail on the next pod",
        reversibility=Reversibility.STATELESS,
        compensating_action=None,
        validator="kubernetes_target",
        executor="kubernetes",
        blast_radius=0.3,
        params_hint="deployment: str, namespace: str",
    )
)

register_action(
    ActionSpec(
        name=ActionName.ROLLBACK_DEPLOYMENT.value,
        params_model=DeploymentParams,
        risk_tier=_const_tier(RiskTier.MEDIUM),
        preconditions=(
            "target deployment exists in the namespace",
            "a prior revision exists to roll back to",
        ),
        expected_effect="reverts the deployment's pod template to the previous revision, "
        "undoing out-of-band set image / set env / set resources changes",
        reversibility=Reversibility.COMPENSABLE,
        # rollout undo is its own inverse: undoing the undo rolls forward
        # to the revision that was live before the rollback.
        compensating_action=ActionName.ROLLBACK_DEPLOYMENT.value,
        validator="kubernetes_target",
        executor="kubernetes",
        blast_radius=0.4,
        params_hint="deployment: str, namespace: str",
    )
)

register_action(
    ActionSpec(
        name=ActionName.SCALE_DEPLOYMENT.value,
        params_model=ScaleDeploymentParams,
        risk_tier=_scale_tier,
        preconditions=("target deployment exists in the namespace",),
        expected_effect="sets the deployment's replica count (e.g. restore a "
        "scaled-to-zero dependency to its baseline)",
        reversibility=Reversibility.COMPENSABLE,
        compensating_action=ActionName.SCALE_DEPLOYMENT.value,  # scale back
        validator="kubernetes_target",
        executor="kubernetes",
        blast_radius=0.4,
        params_hint="deployment: str, namespace: str, replicas: int >= 1",
    )
)

register_action(
    ActionSpec(
        name=ActionName.REAPPLY_MANIFEST.value,
        params_model=ReapplyManifestParams,
        risk_tier=_reapply_tier,
        preconditions=(
            "path is a committed file under sandbox/payment-api/k8s/",
            "the manifest still describes the desired state",
        ),
        expected_effect="applies the committed manifest over live state, reverting "
        "drift in the fields the manifest owns (NOT out-of-band env overrides)",
        reversibility=Reversibility.COMPENSABLE,
        compensating_action=ActionName.REAPPLY_MANIFEST.value,  # apply the previous manifest
        validator="manifest_dry_run",
        executor="kubernetes",
        blast_radius=0.5,
        params_hint="path: one of the committed sandbox/payment-api/k8s/*.yaml files",
    )
)

register_action(
    ActionSpec(
        name=ActionName.OPEN_FIX_PR.value,
        params_model=OpenFixPrParams,
        risk_tier=_open_fix_pr_tier,
        preconditions=(
            "branch must match helix/INC-* (the incident branch)",
            "every file path stays under sandbox/payment-api/ and carries "
            "the full intended new content",
        ),
        expected_effect="opens a reviewed PR carrying a corrected file - the "
        "persistent fix path for code, config and manifest defects",
        reversibility=Reversibility.COMPENSABLE,
        # Not a catalogue action: an executor-side operation (1.8/1.10) the
        # planner can never propose as a remediation.
        compensating_action="close_pr",
        validator="fix_content",
        executor="github",
        blast_radius=0.3,
        params_hint="branch: helix/INC-*, files: [{path, content}], title: str, body: str",
    )
)

register_action(
    ActionSpec(
        name=ActionName.RERUN_PIPELINE.value,
        params_model=RerunPipelineParams,
        risk_tier=_const_tier(RiskTier.LOW),
        preconditions=("the job exists and has a previous build to re-run",),
        expected_effect="re-runs the pipeline job; remediates transient CI failures, "
        "re-fails deterministically on content defects",
        reversibility=Reversibility.STATELESS,
        compensating_action=None,
        validator="pipeline_exists",
        executor="jenkins",
        blast_radius=0.2,
        params_hint="job: str",
    )
)


def params_model_for(name: str) -> type[BaseModel] | None:
    """Convenience for tests and the planner: the params model registered
    under `name`, or None for an unknown action."""
    spec = get_action(name)
    return spec.params_model if spec else None
