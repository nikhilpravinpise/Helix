"""Kubernetes payloads to canonical events (subphase E3).

Consumes the shapes `kubectl get -o json` returns - a `{items: [...]}`
list of Pods, or of Events - and emits `pod.failed` and
`service.unhealthy`. All knowledge of `containerStatuses`, `lastState`
and friends stops here.

The split between the two event types is the same distinction E1 recorded
when it accepted `service.unhealthy` into the taxonomy:

- A container that is not running - waiting with a reason, or terminated
  non-zero - is `pod.failed`.
- A pod whose containers are all running but whose `Ready` condition is
  false is `service.unhealthy`. Nothing died; it is up and not serving.
  This adapter is the producer E1 said would eventually supply that type,
  and the captured `config-mismatch` fault is where it fires: the new
  ReplicaSet's pod is Running with `Ready: False` while the old one still
  serves.

Note that `config-mismatch` yields `service.unhealthy` here while its
Phase 1 *signal* classifies as `deployment.failed`. Both are true of
different objects - the pod is up and not ready, the rollout is wedged -
and collapsing two views of one fault into a single incident is precisely
what E4 correlation is for. Neither reading is the other's bug.

A healthy pod produces nothing. Adapters emit failures, not a heartbeat
per object.
"""

from typing import Any

from events.adapters._common import vendor_event
from events.schemas import (
    EngineeringEvent,
    EventSource,
    EventType,
    derive_event_id,
)

#: Pods in these phases are finished on purpose, not broken.
_TERMINAL_PHASES = {"Succeeded"}


def _service_of(pod: dict[str, Any], default: str) -> str:
    labels = (pod.get("metadata") or {}).get("labels") or {}
    return labels.get("app") or labels.get("app.kubernetes.io/name") or default


def _failing_container(pod: dict[str, Any]) -> dict[str, Any] | None:
    """The first container that is not running, with why.

    Waiting-with-a-reason covers CrashLoopBackOff, ImagePullBackOff and
    CreateContainerConfigError alike; `lastState.terminated` is what
    distinguishes an OOM kill from an ordinary non-zero exit, and losing
    it is what made crashloop and oom-killed indistinguishable in Phase 1
    dedup.
    """
    for cs in (pod.get("status") or {}).get("containerStatuses") or []:
        waiting = (cs.get("state") or {}).get("waiting") or {}
        terminated = (cs.get("lastState") or {}).get("terminated") or {}
        if waiting.get("reason"):
            return {
                "container": cs.get("name"),
                "waiting_reason": waiting.get("reason"),
                "message": waiting.get("message"),
                "terminated_reason": terminated.get("reason"),
                "exit_code": terminated.get("exitCode"),
                "restart_count": cs.get("restartCount", 0),
                "image": cs.get("image"),
            }
    return None


def _is_ready(pod: dict[str, Any]) -> bool:
    for condition in (pod.get("status") or {}).get("conditions") or []:
        if condition.get("type") == "Ready":
            return condition.get("status") == "True"
    return True  # no Ready condition reported: do not invent a failure


def _deleting(pod: dict[str, Any]) -> bool:
    return bool((pod.get("metadata") or {}).get("deletionTimestamp"))


def pod_events(
    payload: dict[str, Any],
    service: str = "payment-api",
    environment: str = "sandbox",
) -> list[EngineeringEvent]:
    """Canonical events for every unhealthy pod in a `kubectl get pods -o
    json` payload."""
    out: list[EngineeringEvent] = []
    for pod in payload.get("items") or []:
        status = pod.get("status") or {}
        metadata = pod.get("metadata") or {}
        if status.get("phase") in _TERMINAL_PHASES or _deleting(pod):
            # A pod mid-deletion is the intended end of a rollout, not a
            # failure - the same exclusion the 1.9 verification leg makes.
            continue

        uid = metadata.get("uid") or metadata.get("name")
        common = {
            "source": EventSource.KUBERNETES,
            "service": _service_of(pod, service),
            "environment": environment,
        }
        failing = _failing_container(pod)
        if failing is not None:
            out.append(
                vendor_event(
                    # Natural key: the pod's uid plus which container and
                    # which restart. Every field is one the API server
                    # repeats verbatim, and including the restart count
                    # makes each distinct failure its own event rather
                    # than collapsing a crash loop into one.
                    event_id=derive_event_id(
                        "kubernetes", uid, failing["container"], failing["restart_count"]
                    ),
                    event_type=EventType.POD_FAILED,
                    metadata={
                        "pod": metadata.get("name"),
                        "namespace": metadata.get("namespace"),
                        "phase": status.get("phase"),
                        **failing,
                    },
                    **common,
                )
            )
        elif not _is_ready(pod):
            out.append(
                vendor_event(
                    event_id=derive_event_id("kubernetes", uid, "not-ready"),
                    event_type=EventType.SERVICE_UNHEALTHY,
                    metadata={
                        "pod": metadata.get("name"),
                        "namespace": metadata.get("namespace"),
                        "phase": status.get("phase"),
                        "reason": "pod is running but its Ready condition is false",
                    },
                    **common,
                )
            )
    return out


def object_events(
    payload: dict[str, Any],
    service: str = "payment-api",
    environment: str = "sandbox",
) -> list[EngineeringEvent]:
    """Canonical events for Warning-type Kubernetes Events.

    Normal events are the cluster narrating itself and are not failures;
    emitting one per Scheduled or Pulled would bury the Warnings that
    matter.
    """
    out: list[EngineeringEvent] = []
    for item in payload.get("items") or []:
        if item.get("type") != "Warning":
            continue
        involved = item.get("involvedObject") or {}
        metadata = item.get("metadata") or {}
        out.append(
            vendor_event(
                # A Kubernetes Event has its own uid. `count` is included
                # because the API server coalesces repeats into one object
                # and bumps the counter, so the uid alone would make the
                # tenth occurrence indistinguishable from the first.
                event_id=derive_event_id("kubernetes", metadata.get("uid"), item.get("count", 1)),
                event_type=EventType.POD_FAILED
                if involved.get("kind") == "Pod"
                else EventType.DEPLOYMENT_FAILED,
                source=EventSource.KUBERNETES,
                service=service,
                environment=environment,
                metadata={
                    "reason": item.get("reason"),
                    "message": item.get("message"),
                    "involved_kind": involved.get("kind"),
                    "involved_name": involved.get("name"),
                    "namespace": involved.get("namespace"),
                    "count": item.get("count", 1),
                },
            )
        )
    return out
