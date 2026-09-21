"""Prior-incident context for the reasoning nodes (subphase E6).

The bridge between `events.retrieval` and the agent nodes. Kept in one
place so both the arm-E diagnosis node and the arm-B/C fused node offer
context the same way, and so the flag is honoured in exactly one spot.

**Retrieval never fails a run.** It is an optional aid, so an error
building context is swallowed into an empty string rather than failing
diagnosis. A run that would have succeeded without retrieval must not
start failing because retrieval was switched on - otherwise the flag
measures fragility instead of usefulness.
"""

from typing import TYPE_CHECKING, Any

from helix.config import get_settings

if TYPE_CHECKING:  # pragma: no cover - typing only
    from events.retrieval import RetrievalResult


def retrieval_query(incident: dict[str, Any], signature: dict[str, Any] | None = None) -> str:
    """The text a new incident is matched on.

    Its own fault signature plus service and environment: the fields that
    describe what is broken, and nothing Helix has concluded yet. Using a
    hypothesis here would make retrieval depend on the answer it is meant
    to help produce.
    """
    parts = [
        str(incident.get("service") or ""),
        str(incident.get("environment") or ""),
        str(incident.get("severity") or ""),
    ]
    for key, value in (signature or {}).items():
        parts.append(f"{key} {value}")
    return " ".join(p for p in parts if p)


def prior_incident_context(
    deps: Any,
    incident: dict[str, Any],
    signature: dict[str, Any] | None = None,
    *,
    enabled: bool | None = None,
) -> str:
    """Rendered prior-incident context, or "" when retrieval is off.

    `enabled` overrides the setting, which is how arm C turns retrieval on
    for itself without turning it on globally.
    """
    settings = get_settings()
    if not (settings.retrieval_enabled if enabled is None else enabled):
        return ""

    from events.retrieval import render_context, similar_incidents

    try:
        with deps.session_scope() as session:
            results: list[RetrievalResult] = similar_incidents(
                session,
                retrieval_query(incident, signature),
                service=incident.get("service"),
                exclude=incident.get("incident_id"),
                limit=settings.retrieval_limit,
            )
            return render_context(results)
    except Exception:
        # See the module docstring: an aid that can fail the run is worse
        # than no aid.
        return ""
