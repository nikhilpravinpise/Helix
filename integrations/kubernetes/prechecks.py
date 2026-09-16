"""Read-side Kubernetes prechecks for the Validation Agent (subphase 1.7).

Two admission-level checks, both strictly non-mutating:

- `can_i(verb, resource, namespace, group)` - a SelfSubjectAccessReview
  asking the API server whether the configured credentials may perform
  the verb the action's executor will need (rollout restart/undo and
  scale are PATCH/UPDATE on the deployment or its scale subresource).
- `dry_run_apply(manifest_text)` - literally `kubectl apply -f -
  --dry-run=server`: client-side apply with the full admission chain and
  nothing persisted. Client-side semantics matter - a pure server-side
  apply dry-run reports field-ownership conflicts against objects managed
  by `kubectl-client-side-apply` that the real `kubectl apply` executor
  would never hit; the dry-run must predict execution, not a stricter
  code path.

Contract: a `PrecheckOutcome` is returned when the cluster answered
(allowed/denied, accepted/rejected); a check that could not run at all -
no kubeconfig, unreachable cluster, the SSAR call itself denied - raises
the usual EvidenceError family (same mapping as the evidence client).
Strategies map `ok=False` to FAIL and a raised error to
NOT_VALIDATABLE, so "we could not check" is never reported as a pass.

Both calls take injectable seams (`authz_v1`, `apply_fn`) so unit tests
exercise them without a cluster.
"""

import json
import subprocess
from dataclasses import dataclass, field
from typing import Any

import urllib3.exceptions
import yaml
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException
from kubernetes.config.config_exception import ConfigException
from kubernetes.utils.create_from_yaml import FailToCreateError

from helix.config import get_settings
from integrations.evidence import EvidenceError, EvidenceTimeoutError
from integrations.kubernetes.client import _as_dict, _map_api_error


@dataclass(frozen=True)
class PrecheckOutcome:
    """One precheck verdict. `ok` is the cluster's answer; `detail`
    carries the server's reason (RBAC denial text, admission rejection
    message) or a short summary - it feeds ValidationResult.reason, so it
    should say WHAT was decided, not just that something failed."""

    ok: bool
    detail: str = ""
    checks: dict[str, Any] = field(default_factory=dict)


class _DryRunRejected(Exception):
    """`kubectl apply --dry-run=server` exited non-zero - the message is
    the API server's admission rejection text."""


def _failure_detail(exc: FailToCreateError) -> str:
    """Pull the API server's message out of each wrapped ApiException -
    `e.body` is the JSON Status object, whose `message` is the admission
    rejection text a human needs."""
    parts = []
    for api_exc in exc.api_exceptions:
        message = getattr(api_exc, "reason", None) or str(api_exc)
        body = getattr(api_exc, "body", None)
        if body:
            try:
                message = json.loads(body).get("message") or message
            except (ValueError, AttributeError):
                pass
        parts.append(str(message))
    return "; ".join(parts) or "server-side apply dry run rejected the manifest"


class KubernetesPrechecksClient:
    """Live prechecks over the python kubernetes client.

    Kubeconfig loads lazily like the evidence client, so construction
    never fails on a missing cluster. `authz_v1` and `apply_fn` are the
    test seams: an AuthorizationV1Api-shaped object and a
    `callable(list[dict]) -> None` that raises on rejection.
    """

    def __init__(
        self,
        context: str | None = None,
        *,
        authz_v1: Any = None,
        apply_fn: Any = None,
        kubectl_bin: str = "kubectl",
    ) -> None:
        self._context = context or get_settings().kube_context
        self._authz = authz_v1
        self._apply_fn = apply_fn
        self._kubectl = kubectl_bin

    def _authz_api(self) -> Any:
        if self._authz is None:
            config.load_kube_config(context=self._context)
            self._authz = client.AuthorizationV1Api()
        return self._authz

    def _server_apply(self, docs: list[dict], manifest_text: str) -> None:
        """Client-side apply with --dry-run=server via the kubectl CLI -
        the same code path the executor's `kubectl apply -f` takes, so
        the dry-run predicts what execution will actually do (including
        per-document metadata.namespace handling, which `kubectl apply`
        resolves natively)."""
        if self._apply_fn is not None:
            self._apply_fn(docs)
            return
        proc = subprocess.run(
            [
                self._kubectl,
                "--context",
                self._context,
                "apply",
                "-f",
                "-",
                "--dry-run=server",
            ],
            input=manifest_text,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode != 0:
            raise _DryRunRejected(
                (proc.stderr or proc.stdout or "kubectl apply dry-run failed").strip()
            )

    def can_i(
        self,
        verb: str,
        resource: str,
        namespace: str,
        group: str = "",
    ) -> PrecheckOutcome:
        """Whether the current credentials may `verb` `resource` in
        `namespace`. `resource` may carry a subresource as
        "deployments/scale"; `group` is the API group ("apps" for
        workloads, "" for core)."""
        resource_name, _, subresource = resource.partition("/")
        review = client.V1SelfSubjectAccessReview(
            spec=client.V1SelfSubjectAccessReviewSpec(
                resource_attributes=client.V1ResourceAttributes(
                    verb=verb,
                    group=group or None,
                    resource=resource_name,
                    subresource=subresource or None,
                    namespace=namespace,
                )
            )
        )
        try:
            status = (
                _as_dict(self._authz_api().create_self_subject_access_review(body=review)).get(
                    "status"
                )
                or {}
            )
        except ApiException as exc:
            raise _map_api_error(exc) from exc
        except (urllib3.exceptions.TimeoutError, TimeoutError) as exc:
            raise EvidenceTimeoutError(f"can-i precheck timed out: {exc}") from exc
        except (urllib3.exceptions.HTTPError, ConfigException) as exc:
            raise EvidenceError(f"can-i precheck failed: {exc}") from exc
        allowed = bool(status.get("allowed"))
        detail = (
            status.get("reason")
            or status.get("evaluationError")
            or ("allowed" if allowed else "denied")
        )
        return PrecheckOutcome(
            ok=allowed,
            detail=detail,
            checks={"verb": verb, "resource": resource, "namespace": namespace},
        )

    def dry_run_apply(self, manifest_text: str) -> PrecheckOutcome:
        """Server-side apply dry-run of a manifest document. `ok=False`
        when the manifest is unparseable, structurally not a k8s object,
        or rejected by admission; machinery failures raise EvidenceError."""
        try:
            docs = [d for d in yaml.safe_load_all(manifest_text) if d is not None]
        except yaml.YAMLError as exc:
            return PrecheckOutcome(ok=False, detail=f"manifest does not parse: {exc}")
        if not docs:
            return PrecheckOutcome(ok=False, detail="manifest contains no objects")
        malformed = [
            d
            for d in docs
            if not isinstance(d, dict) or not d.get("apiVersion") or not d.get("kind")
        ]
        if malformed:
            return PrecheckOutcome(
                ok=False,
                detail="manifest contains objects without apiVersion/kind",
            )
        try:
            self._server_apply(docs, manifest_text)
        except _DryRunRejected as exc:
            return PrecheckOutcome(ok=False, detail=str(exc))
        except FailToCreateError as exc:
            return PrecheckOutcome(ok=False, detail=_failure_detail(exc))
        except ApiException as exc:
            raise _map_api_error(exc) from exc
        except (
            subprocess.TimeoutExpired,
            urllib3.exceptions.TimeoutError,
            TimeoutError,
        ) as exc:
            raise EvidenceTimeoutError(f"dry-run apply timed out: {exc}") from exc
        except (OSError, urllib3.exceptions.HTTPError, ConfigException) as exc:
            raise EvidenceError(f"dry-run apply failed: {exc}") from exc
        return PrecheckOutcome(
            ok=True,
            detail=f"{len(docs)} object(s) accepted by server dry-run",
            checks={"objects": len(docs)},
        )
