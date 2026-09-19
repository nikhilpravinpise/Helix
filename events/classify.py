"""Signal signature to canonical event type (subphase E1).

Phase 1 signals carry `source` plus a free-form `signature` and no event
type at all; the canonical envelope requires one. This module is the only
place that translation happens.

It takes primitives rather than a `Signal`, so `events` stays free of any
dependency on `state` - intake imports from here, never the other way
round.

**Unclassifiable signals are rejected, not guessed at.** plan.md E1 lists
schema validation as a deliverable, and a signal that cannot be named
cannot become a canonical event. The alternative is filing it under a
label that is false, which every downstream consumer then inherits and
trusts. All twelve benchmark scenarios classify; a signal shape outside
them is a gap to close in this table, and it should be loud.
"""

from typing import Any

from events.schemas import EventType


class UnclassifiedSignal(ValueError):
    """No rule names this signal. Carries the source and signature so the
    gap is visible in the error rather than needing a debugger."""

    def __init__(self, source: str, signature: dict[str, Any]) -> None:
        self.source = source
        self.signature = signature
        super().__init__(f"no event type for a {source} signal with signature {signature!r}")


def _kubernetes(signature: dict[str, Any]) -> EventType | None:
    # Checked before the pod rules: both describe a deployment that never
    # produced a running pod to have a waiting reason.
    if signature.get("reason") == "ApplyRejected":
        # The API server refused the manifest outright - no ReplicaSet, no
        # pod, no Kubernetes event. The failure is the deployment's.
        return EventType.DEPLOYMENT_FAILED
    if signature.get("symptom") == "rollout_wedged":
        # New pods fail readiness while the old ReplicaSet keeps serving,
        # so the service is up and the rollout is what failed.
        return EventType.DEPLOYMENT_FAILED
    if signature.get("waiting_reason"):
        # Any waiting reason at all means the container is not running -
        # CrashLoopBackOff, ImagePullBackOff, CreateContainerConfigError
        # and the rest all resolve here. Keyed on presence rather than an
        # enumerated set, because the set is Kubernetes's to extend and a
        # reason we have not seen is still a pod that did not start.
        return EventType.POD_FAILED
    return None


def _jenkins(signature: dict[str, Any]) -> EventType | None:
    if signature.get("job") and signature.get("stage"):
        # Intake is failure-driven in Phase 1 (D6): a Jenkins signal
        # naming the stage it stopped at is a failed build. build.started
        # and build.passed enter the taxonomy with the E3 adapter, which
        # sees the whole build lifecycle rather than only its failures.
        return EventType.BUILD_FAILED
    return None


def _github(signature: dict[str, Any]) -> EventType | None:
    kind = signature.get("type")
    if kind == "check_run" and signature.get("conclusion") == "failure":
        # A failing check run is CI reporting through GitHub rather than
        # through Jenkins; the event is about the build either way.
        return EventType.BUILD_FAILED
    if kind == "pull_request_review" and signature.get("review_state") == "CHANGES_REQUESTED":
        return EventType.PR_REJECTED
    if kind == "branch_state" and signature.get("comparison") == "diverged":
        return EventType.BRANCH_DIVERGED
    return None


_RULES = {
    "kubernetes": _kubernetes,
    "jenkins": _jenkins,
    "github": _github,
}


def classify(source: str, signature: dict[str, Any]) -> EventType:
    """The canonical event type for one signal, or raise.

    Rules are per source and ordered most specific first within a source.
    """
    rule = _RULES.get(str(source))
    event_type = rule(signature or {}) if rule else None
    if event_type is None:
        raise UnclassifiedSignal(str(source), signature or {})
    return event_type
