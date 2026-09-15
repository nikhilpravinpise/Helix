"""Kubernetes-runtime verification legs (subphase 1.9): the desired state
a restart/rollback/scale/reapply must leave behind.

Three sampled legs plus a Secret-presence variant for `reapply_manifest`
on a Secret object:

- `PodHealthCheck` - every pod for the target workload is Running with
  all containers ready and no bad waiting reason; a pod's restart_count
  rising across samples - or a pod that reported Ready then vanished -
  is a failed sample, so crash-looping is caught even when a sample
  happens to catch the pod mid-"Running".
- `RolloutCheck` - the target Deployment reports desired == updated ==
  ready == available replicas and no ProgressDeadlineExceeded.
- `ElkErrorCheck` - error-level log lines for the incident service in the
  poll-sized post-action window stay under HELIX_ELK_ERROR_THRESHOLD.
- `SecretPresentCheck` - a re-applied Secret exists (metadata + key
  presence via `secret_info`, never values - D4).

The verify target: `params.deployment`/`params.namespace` for the
deployment actions; for `reapply_manifest` the committed manifest itself
is parsed (Deployment -> its name/namespace; Secret -> the incident
service's pods plus a SecretPresentCheck; anything else -> the incident
service's workload). Pod listing uses the `app=<name>` label the sandbox
manifests all carry.

Fixture mode: these read the fixture's recorded pods_*/logs_* evidence,
so a fixture-mode run verifies against recorded state. A fixture that
simply does not carry the needed artifact reports `skipped` (recorded,
never a pass); a live cluster that cannot answer reports `error`.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from agent.actions.catalogue import ActionName
from agent.verification import CheckStatus, Sample, SampledCheck, VerificationContext
from integrations.capabilities import ElkEvidence, KubernetesEvidence
from integrations.evidence import EvidenceNotFoundError

#: Container waiting reasons that mean the workload is still broken.
BAD_WAITING_REASONS: frozenset[str] = frozenset(
    {
        "CrashLoopBackOff",
        "CreateContainerConfigError",
        "ImagePullBackOff",
        "ErrImagePull",
        "CreateContainerError",
        "RunContainerError",
        "InvalidImageName",
    }
)


@dataclass
class PodHealthCheck:
    """`pods_ready`: every pod for `app=<deployment>` Ready, no bad
    waiting reasons, and no mid-window instability (restart-count rise,
    a previously-Ready pod disappearing)."""

    kubernetes: KubernetesEvidence
    namespace: str
    deployment: str
    fixture: bool = False
    _ready_seen: set[str] = field(default_factory=set)
    _last_restarts: dict[str, int] = field(default_factory=dict)

    name: str = "pods_ready"

    def check(self) -> Sample:
        try:
            evidence = self.kubernetes.get_pods(
                self.namespace, label_selector=f"app={self.deployment}"
            )
        except EvidenceNotFoundError as exc:
            # Fixture replay simply does not carry the pods_* artifact -
            # skipped, not a verdict about the world. A live list call
            # never 404s (empty items are a real observation below).
            if self.fixture:
                return Sample(CheckStatus.SKIPPED, f"no pods evidence recorded: {exc}")
            raise
        pods = evidence.payload.get("items") or []
        if not pods:
            return Sample(
                CheckStatus.FAIL,
                f"no pods for app={self.deployment} in {self.namespace}",
                evidence.id,
            )
        problems: list[str] = []
        restarts: dict[str, int] = {}
        names: set[str] = set()
        for pod in pods:
            name = pod.get("name") or "?"
            names.add(name)
            restarts[name] = pod.get("restart_count") or 0
            if pod.get("phase") != "Running":
                problems.append(f"{name} phase={pod.get('phase')}")
                continue
            for container in pod.get("containers") or []:
                waiting = (container.get("state") or {}).get("waiting") or {}
                reason = waiting.get("reason")
                if reason in BAD_WAITING_REASONS:
                    problems.append(f"{name}/{container.get('name')} waiting {reason}")
                elif not container.get("ready"):
                    problems.append(f"{name}/{container.get('name')} not ready")
            if name in self._ready_seen and restarts[name] > self._last_restarts.get(name, 0):
                problems.append(
                    f"{name} restart_count rose "
                    f"{self._last_restarts[name]}->{restarts[name]} (still crashing)"
                )
        gone = sorted(self._ready_seen - names)
        if gone:
            problems.append(f"pod(s) previously Ready now absent: {', '.join(gone)}")
        self._last_restarts = restarts
        if problems:
            return Sample(CheckStatus.FAIL, "; ".join(problems), evidence.id)
        self._ready_seen |= names
        return Sample(CheckStatus.PASS, f"{len(pods)} pod(s) ready", evidence.id)


@dataclass
class RolloutCheck:
    """`rollout_complete`: the Deployment reports desired == updated ==
    ready == available replicas and no failed-progress condition."""

    kubernetes: KubernetesEvidence
    namespace: str
    deployment: str
    fixture: bool = False

    name: str = "rollout_complete"

    def check(self) -> Sample:
        try:
            evidence = self.kubernetes.get_deployment_status(self.namespace, self.deployment)
        except EvidenceNotFoundError as exc:
            # A live 404 means the action's target is gone - a real
            # failure. A fixture without deployments_* simply cannot
            # answer - skipped, never counted.
            if self.fixture:
                return Sample(CheckStatus.SKIPPED, f"no deployment evidence recorded: {exc}")
            return Sample(CheckStatus.FAIL, f"deployment not found: {exc}")
        p = evidence.payload
        desired = p.get("replicas") or 0
        ready = p.get("ready_replicas") or 0
        updated = p.get("updated_replicas") or 0
        available = p.get("available_replicas") or 0
        stalled = [
            c
            for c in p.get("conditions") or []
            if c.get("reason") == "ProgressDeadlineExceeded"
            or (c.get("type") == "Progressing" and c.get("status") == "False")
        ]
        if stalled:
            return Sample(
                CheckStatus.FAIL,
                f"rollout stalled: {stalled[0].get('reason')} - {stalled[0].get('message')}",
                evidence.id,
            )
        if desired > 0 and ready == updated == available == desired:
            return Sample(CheckStatus.PASS, f"{ready}/{desired} replicas ready", evidence.id)
        return Sample(
            CheckStatus.FAIL,
            f"rollout incomplete: {ready}/{desired} ready, {updated} updated, "
            f"{available} available",
            evidence.id,
        )


@dataclass
class ElkErrorCheck:
    """`elk_errors`: error-level log lines for the incident service stay
    at or under the threshold. The leg queries a poll-sized window - each
    sample answers "is the service still logging errors NOW", so a pod
    that is Ready but still serving errors fails verification."""

    elk: ElkEvidence
    service: str
    threshold: int
    window: str = "60s"
    fixture: bool = False

    name: str = "elk_errors"

    def check(self) -> Sample:
        try:
            evidence = self.elk.get_logs(self.service, self.window, level="error")
        except EvidenceNotFoundError as exc:
            if self.fixture:
                return Sample(CheckStatus.SKIPPED, f"no logs evidence recorded: {exc}")
            raise
        lines = evidence.payload.get("lines") or []
        count = evidence.total if evidence.total is not None else len(lines)
        if count > self.threshold:
            return Sample(
                CheckStatus.FAIL,
                f"{count} error line(s) in last {self.window} (threshold {self.threshold})",
                evidence.id,
            )
        return Sample(
            CheckStatus.PASS,
            f"{count} error line(s) in last {self.window} (threshold {self.threshold})",
            evidence.id,
        )


@dataclass
class SecretPresentCheck:
    """`secret_present`: a re-applied Secret exists - via `secret_info`
    metadata only, never values (D4)."""

    kubernetes: KubernetesEvidence
    namespace: str
    secret: str
    fixture: bool = False

    name: str = "secret_present"

    def check(self) -> Sample:
        try:
            evidence = self.kubernetes.get_secret_info(self.namespace, self.secret)
        except EvidenceNotFoundError as exc:
            if self.fixture:
                return Sample(CheckStatus.SKIPPED, f"no secret evidence recorded: {exc}")
            return Sample(CheckStatus.FAIL, f"secret {self.secret} not found: {exc}")
        keys = evidence.payload.get("key_count") or 0
        return Sample(CheckStatus.PASS, f"secret {self.secret} present ({keys} keys)", evidence.id)


def _manifest_target(repo_root: Path, path: str) -> tuple[str | None, str, str | None]:
    """The (kind, namespace, name) a reapply_manifest path manages, read
    from the committed file - the path is already allowlisted by the
    catalogue, so this only ever reads the seven committed manifests."""
    try:
        docs = [
            d for d in yaml.safe_load_all((repo_root / path).read_text()) if isinstance(d, dict)
        ]
    except (OSError, yaml.YAMLError):
        return None, "", None
    if not docs:
        return None, "", None
    meta = docs[0].get("metadata") or {}
    return docs[0].get("kind"), meta.get("namespace") or "", meta.get("name")


def workload_checks(
    ctx: VerificationContext, action: str, params: dict[str, Any]
) -> list[SampledCheck]:
    """The sampled k8s legs for one runtime action, resolved to a target
    workload (deployment name + namespace)."""
    namespace = str(params.get("namespace") or ctx.namespace)
    deployment = str(params.get("deployment") or "")
    checks: list[SampledCheck] = []

    if action == ActionName.REAPPLY_MANIFEST.value:
        from agent.agents.validation import REPO_ROOT

        kind, manifest_ns, name = _manifest_target(REPO_ROOT, str(params.get("path") or ""))
        namespace = manifest_ns or namespace
        if kind == "Deployment" and name:
            deployment = name
        elif kind == "Secret" and name:
            checks.append(SecretPresentCheck(ctx.kubernetes, namespace, name, ctx.is_fixture))
            deployment = deployment or ctx.service
        else:
            # ConfigMap/Service/Ingress/... - desired state is the
            # incident service's workload healthy again.
            deployment = deployment or ctx.service
    if not deployment:
        deployment = ctx.service

    checks.append(PodHealthCheck(ctx.kubernetes, namespace, deployment, ctx.is_fixture))
    checks.append(RolloutCheck(ctx.kubernetes, namespace, deployment, ctx.is_fixture))
    return checks
