"""Kubernetes evidence capability over the python kubernetes client.

Returns normalized `Evidence` only - V1Pod/V1Event/etc never cross the
boundary (C8). Raw API objects are sanitized to plain dicts and run through
the module-level `normalize_*` functions, which the fixture implementation
in integrations/fixtures.py reuses so live and fixture evidence carry the
identical shape.

Read-only: every method is a get/list. Error mapping (uniform across
capabilities): ApiException 401/403 -> auth, 404 -> not found, 429 ->
rate limit, other statuses -> EvidenceError; urllib3 timeouts -> timeout,
other transport failures -> EvidenceError.
"""

from typing import Any

import urllib3.exceptions
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

from helix.config import get_settings
from integrations.evidence import (
    Evidence,
    EvidenceAuthError,
    EvidenceError,
    EvidenceKind,
    EvidenceNotFoundError,
    EvidenceOrigin,
    EvidencePartialError,
    EvidenceRateLimitError,
    EvidenceSource,
    EvidenceTimeoutError,
    build_evidence,
)


def _as_dict(obj: Any) -> dict:
    """Convert a kubernetes client model to a plain dict (same camelCase
    shape as `kubectl -o json`). Plain dicts pass through unchanged, which
    also makes stubbed tests easy."""
    if isinstance(obj, dict):
        return obj
    return client.ApiClient().sanitize_for_serialization(obj)


def _map_api_error(exc: ApiException) -> EvidenceError:
    msg = f"kubernetes API error {exc.status}: {exc.reason or exc}"
    if exc.status in (401, 403):
        return EvidenceAuthError(msg)
    if exc.status == 404:
        return EvidenceNotFoundError(msg)
    if exc.status == 429:
        return EvidenceRateLimitError(msg)
    return EvidenceError(msg)


def _container_state(status: dict) -> dict | None:
    """The single interesting member of a containerStatus.state map."""
    state = status.get("state") or {}
    for name in ("waiting", "terminated", "running"):
        if name in state:
            return {name: state[name]}
    return None


def normalize_pod(pod: dict) -> dict:
    """One pod down to the fields diagnosis needs: phase, per-container
    state and last termination, env source references (names only), owner
    chain and conditions."""
    metadata = pod.get("metadata") or {}
    spec = pod.get("spec") or {}
    status = pod.get("status") or {}

    containers = []
    env_from = []
    env_vars = []
    for cs in status.get("containerStatuses") or []:
        last = (cs.get("lastState") or {}).get("terminated")
        containers.append(
            {
                "name": cs.get("name"),
                "image": cs.get("image"),
                "ready": cs.get("ready", False),
                "restart_count": cs.get("restartCount", 0),
                "state": _container_state(cs),
                "last_terminated": (
                    {
                        "exit_code": last.get("exitCode"),
                        "reason": last.get("reason"),
                        "finished_at": last.get("finishedAt"),
                    }
                    if last
                    else None
                ),
            }
        )

    for container in spec.get("containers") or []:
        for source in container.get("envFrom") or []:
            if "configMapRef" in source:
                env_from.append({"type": "configmap", "name": source["configMapRef"].get("name")})
            if "secretRef" in source:
                env_from.append({"type": "secret", "name": source["secretRef"].get("name")})
        for env in container.get("env") or []:
            entry = {"name": env.get("name")}
            if "valueFrom" in env:
                ref = env["valueFrom"]
                if "secretKeyRef" in ref:
                    entry["value_from"] = "secret"
                elif "configMapKeyRef" in ref:
                    entry["value_from"] = "configmap"
                else:
                    entry["value_from"] = "field"
            elif "value" in env:
                # A literal env value could itself be a credential - record
                # presence, never the value.
                entry["value_from"] = "literal"
            env_vars.append(entry)

    owner = None
    for ref in metadata.get("ownerReferences") or []:
        if ref.get("controller"):
            owner = {"kind": ref.get("kind"), "name": ref.get("name")}
            break

    return {
        "name": metadata.get("name"),
        "namespace": metadata.get("namespace"),
        "phase": status.get("phase"),
        "pod_ip": status.get("podIP"),
        "node": spec.get("nodeName"),
        "start_time": status.get("startTime"),
        # deletionTimestamp set means Kubernetes is removing this pod - it
        # is leaving the serving set, not part of the desired state.
        "terminating": metadata.get("deletionTimestamp") is not None,
        "owner": owner,
        "ready": bool(containers) and all(c["ready"] for c in containers),
        "restart_count": sum(c["restart_count"] for c in containers),
        "containers": containers,
        "env_from": env_from,
        "env": env_vars,
        "conditions": [
            {
                "type": c.get("type"),
                "status": c.get("status"),
                "reason": c.get("reason"),
                "message": c.get("message"),
            }
            for c in status.get("conditions") or []
        ],
    }


def normalize_event(event: dict) -> dict:
    """One Event down to what diagnosis needs."""
    involved = event.get("involvedObject") or {}
    return {
        "involved_kind": involved.get("kind"),
        "involved_name": involved.get("name"),
        "namespace": involved.get("namespace") or (event.get("metadata") or {}).get("namespace"),
        "reason": event.get("reason"),
        "message": event.get("message"),
        "type": event.get("type"),
        "count": event.get("count"),
        "first_timestamp": event.get("firstTimestamp") or event.get("eventTime"),
        "last_timestamp": event.get("lastTimestamp") or event.get("eventTime"),
        "reporting_component": event.get("reportingComponent")
        or (event.get("source") or {}).get("component"),
    }


def normalize_deployment(dep: dict) -> dict:
    """A Deployment's status - desired vs actual pod state plus rollout
    conditions (Progressing/Available, ProgressDeadlineExceeded)."""
    metadata = dep.get("metadata") or {}
    spec = dep.get("spec") or {}
    status = dep.get("status") or {}
    return {
        "name": metadata.get("name"),
        "namespace": metadata.get("namespace"),
        "replicas": spec.get("replicas"),
        "updated_replicas": status.get("updatedReplicas"),
        "ready_replicas": status.get("readyReplicas"),
        "available_replicas": status.get("availableReplicas"),
        "unavailable_replicas": status.get("unavailableReplicas"),
        "observed_generation": status.get("observedGeneration"),
        "conditions": [
            {
                "type": c.get("type"),
                "status": c.get("status"),
                "reason": c.get("reason"),
                "message": c.get("message"),
            }
            for c in status.get("conditions") or []
        ],
    }


def normalize_secret(secret: dict) -> dict:
    """Secret metadata only (D4): name, namespace, type, key names and
    per-key value presence. Data values are never read into the payload."""
    metadata = secret.get("metadata") or {}
    data = secret.get("data") or {}
    string_data = secret.get("stringData") or {}
    keys = sorted(set(data) | set(string_data))
    return {
        "name": metadata.get("name"),
        "namespace": metadata.get("namespace"),
        "type": secret.get("type"),
        "keys": keys,
        "key_count": len(keys),
        "value_presence": {k: k in data or k in string_data for k in keys},
    }


class KubernetesEvidenceClient:
    """Live Kubernetes evidence capability.

    Kubeconfig is loaded lazily on first call so importing or constructing
    the client never fails on a missing cluster. `core_v1`/`apps_v1` are an
    injection seam for tests - pass fakes and no config is ever loaded.
    """

    def __init__(
        self,
        context: str | None = None,
        *,
        core_v1: Any = None,
        apps_v1: Any = None,
    ) -> None:
        self._context = context or get_settings().kube_context
        self._core = core_v1
        self._apps = apps_v1

    def _core_v1(self) -> Any:
        if self._core is None:
            config.load_kube_config(context=self._context)
            self._core = client.CoreV1Api()
        return self._core

    def _apps_v1(self) -> Any:
        if self._apps is None:
            config.load_kube_config(context=self._context)
            self._apps = client.AppsV1Api()
        return self._apps

    def _call(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except ApiException as exc:
            raise _map_api_error(exc) from exc
        except (urllib3.exceptions.TimeoutError, TimeoutError) as exc:
            raise EvidenceTimeoutError(f"kubernetes call timed out: {exc}") from exc
        except urllib3.exceptions.HTTPError as exc:
            raise EvidenceError(f"kubernetes transport error: {exc}") from exc

    def list_pods_raw(self, namespace: str) -> dict:
        """The namespace's pods exactly as `kubectl get pods -o json` shapes
        them - the input `events.adapters.kubernetes.pod_events` expects.
        Unlike `get_pods` this is not evidence for a diagnosis but the
        watcher's raw feed (D18: fetching lives here, adapting there)."""
        return _as_dict(self._call(self._core_v1().list_namespaced_pod, namespace=namespace))

    def get_pods(
        self,
        namespace: str,
        *,
        service: str | None = None,
        label_selector: str | None = None,
    ) -> Evidence:
        selector = label_selector or (f"app={service}" if service else None)
        result = _as_dict(
            self._call(
                self._core_v1().list_namespaced_pod,
                namespace=namespace,
                label_selector=selector,
            )
        )
        items = result.get("items")
        if items is None:
            raise EvidencePartialError("pod list response had no items field")
        pods = [normalize_pod(p) for p in items]
        name = service or namespace
        return build_evidence(
            source=EvidenceSource.KUBERNETES,
            kind=EvidenceKind.POD_LIST,
            service=name,
            summary=summarize_pods(pods, namespace),
            payload={"namespace": namespace, "selector": selector, "items": pods},
            origin=EvidenceOrigin.LIVE,
            total=len(pods),
        )

    def get_pod_status(self, namespace: str, pod_name: str) -> Evidence:
        result = _as_dict(
            self._call(self._core_v1().read_namespaced_pod, name=pod_name, namespace=namespace)
        )
        pod = normalize_pod(result)
        return build_evidence(
            source=EvidenceSource.KUBERNETES,
            kind=EvidenceKind.POD_STATUS,
            service=pod["name"] or pod_name,
            summary=pod_one_liner(pod),
            payload=pod,
            origin=EvidenceOrigin.LIVE,
        )

    def get_deployment_status(self, namespace: str, name: str) -> Evidence:
        result = _as_dict(
            self._call(self._apps_v1().read_namespaced_deployment, name=name, namespace=namespace)
        )
        dep = normalize_deployment(result)
        summary = (
            f"deployment {name}: {dep['ready_replicas'] or 0}/{dep['replicas']} ready"
            f", {dep['unavailable_replicas'] or 0} unavailable"
        )
        return build_evidence(
            source=EvidenceSource.KUBERNETES,
            kind=EvidenceKind.POD_STATUS,
            service=name,
            summary=summary,
            payload=dep,
            origin=EvidenceOrigin.LIVE,
        )

    def get_events(self, namespace: str, *, involved_object: str | None = None) -> Evidence:
        result = _as_dict(self._call(self._core_v1().list_namespaced_event, namespace=namespace))
        items = result.get("items")
        if items is None:
            raise EvidencePartialError("event list response had no items field")
        events = [normalize_event(e) for e in items]
        if involved_object is not None:
            events = [e for e in events if e["involved_name"] == involved_object]
        events.sort(key=lambda e: e["last_timestamp"] or "")
        return build_evidence(
            source=EvidenceSource.KUBERNETES,
            kind=EvidenceKind.EVENT_LIST,
            service=involved_object or namespace,
            summary=f"{len(events)} event(s) in namespace {namespace}",
            payload={
                "namespace": namespace,
                "involved_object": involved_object,
                "items": events,
            },
            origin=EvidenceOrigin.LIVE,
            total=len(events),
        )

    def get_secret_info(self, namespace: str, name: str) -> Evidence:
        result = _as_dict(
            self._call(self._core_v1().read_namespaced_secret, name=name, namespace=namespace)
        )
        info = normalize_secret(result)
        return build_evidence(
            source=EvidenceSource.KUBERNETES,
            kind=EvidenceKind.SECRET_INFO,
            service=name,
            summary=(
                f"secret {name} in {namespace}: {info['key_count']} key(s)"
                f" ({', '.join(info['keys']) or 'none'})"
            ),
            payload=info,
            origin=EvidenceOrigin.LIVE,
        )


def pod_one_liner(pod: dict) -> str:
    reasons = [
        f"{c['name']}={list(c['state'])[0]}:{list(c['state'].values())[0].get('reason')}"
        for c in pod["containers"]
        if c["state"] and "waiting" in c["state"]
    ]
    suffix = f" ({'; '.join(reasons)})" if reasons else ""
    return f"pod {pod['name']} in {pod['namespace']}: {pod['phase']}{suffix}"


def summarize_pods(pods: list[dict], namespace: str) -> str:
    notable = [
        pod_one_liner(p)
        for p in pods
        if p["phase"] != "Running" or not p["ready"] or p["restart_count"] > 0
    ]
    base = f"{len(pods)} pod(s) in namespace {namespace}"
    return f"{base}; notable: {'; '.join(notable)}" if notable else base
