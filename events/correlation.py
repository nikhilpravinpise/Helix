"""Correlation token extraction (subphase E4).

The problem E4 exists to solve: one underlying failure surfaces as several
events from different systems. A commit lands, its CI build fails, the
deployment that build produced wedges, its pods crash, and those pods emit
error logs. Phase 1 saw four unrelated incidents, because intake dedup is
same-source only - a limitation documented since 1.4.

Correlation here is token-based rather than graph-based (the graph is
2.7). Each event yields a set of tokens drawn from the dimensions plan.md
E4 names - service, environment, commit, PR, build, deployment - and two
events belong together if they share any token with an event already
attached to an open incident. Linking is therefore transitive in effect:
a GitHub check and a Jenkins build share a commit, that build and a
Kubernetes pod share a service, that pod and a log line share a pod name,
so all four converge on one incident without any pair needing a token in
common.

## Why the service token is scoped, not free

`service:<name>:<environment>` is the token that carries the
Jenkins-to-Kubernetes hop, and it is also the most dangerous one: on a
single-service sandbox it would merge every payment-api event ever seen
into one incident. Two things bound it.

1. Only OPEN incidents are candidates. A terminal incident never absorbs a
   new event, the same rule intake dedup already follows.
2. A correlation window (`CORRELATION_WINDOW`). An event correlates only
   with an incident whose most recent event is inside that window.

The window is not decoration. A stale open incident left behind by a
killed run is a documented occurrence in this project, and without the
window one of those would silently swallow every subsequent event for its
service, which looks exactly like correlation working.

Strong tokens - a commit sha, a PR number, a build - are identity. The
service token is proximity, and proximity needs a bound.
"""

from datetime import timedelta
from typing import Any

from events.schemas import EngineeringEvent

#: How long an incident stays open to correlation after its latest event.
#: Generous enough to span a commit -> build -> deploy -> crash chain,
#: short enough that a forgotten incident stops accreting.
CORRELATION_WINDOW = timedelta(minutes=30)

#: Metadata keys that name a commit, in the spellings the adapters emit.
_COMMIT_KEYS = ("ref", "sha", "commit", "head_sha")


def _add(tokens: set[str], prefix: str, value: Any) -> None:
    if value not in (None, "", []):
        tokens.add(f"{prefix}:{value}")


def tokens_for(event: EngineeringEvent) -> set[str]:
    """Every correlation token an event carries.

    Missing dimensions simply yield fewer tokens; an event with only its
    service token still correlates, just more weakly.
    """
    metadata = event.metadata or {}
    tokens: set[str] = set()

    # Proximity. Always present, and the hop that links a build to the
    # workload it deployed.
    _add(tokens, "service", f"{event.service}:{event.environment}")

    # Identity.
    for key in _COMMIT_KEYS:
        _add(tokens, "commit", metadata.get(key))
    _add(tokens, "pr", metadata.get("pr"))
    _add(tokens, "branch", metadata.get("branch"))
    if metadata.get("job") and metadata.get("build_number") is not None:
        _add(tokens, "build", f"{metadata['job']}:{metadata['build_number']}")
    _add(tokens, "pod", metadata.get("pod"))
    # A Kubernetes Event names the object it is about. Its token has to be
    # spelled the same way that object's own events spell it, or the two
    # never match on identity: a pod failure emits `pod:<name>` while the
    # cluster's Warning about that same pod was emitting `object:<name>`,
    # so the only thing correlating them was the service token. That is
    # invisible on a single-service sandbox, where the service token
    # matches everything, and would split one fault across two incidents
    # on a cluster where it does not.
    involved_name = metadata.get("involved_name")
    if involved_name:
        kind = str(metadata.get("involved_kind") or "").lower()
        _add(tokens, "pod" if kind == "pod" else "object", involved_name)
    _add(tokens, "deployment", metadata.get("deployment"))
    return tokens


def strong_tokens(event: EngineeringEvent) -> set[str]:
    """Tokens that assert identity rather than proximity.

    Exposed because a correlation made on identity alone is trustworthy
    outside the time window, while one made on the service token is not.
    """
    return {t for t in tokens_for(event) if not t.startswith("service:")}
