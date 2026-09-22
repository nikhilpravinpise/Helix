"""Shared change-lifecycle context (subphase 2.1/2.5/2.6).

Triage and Review both need the same facts about the PR/push that opened
a `change` incident - number, branch, files touched, author - and neither
owns them: a `change` incident carries no `state.models.Signal` row (it
was opened by `events.ingest`, not `state.intake`), so the context lives
on the incident's own event history. One helper, used by both agents,
keeps that reconstruction in one place rather than two slightly different
copies.
"""

from typing import Any

from events.schemas import EventSource
from state import models


def change_context(incident: models.Incident) -> dict[str, Any]:
    """The newest change-bearing event's metadata for `incident`, or
    `{}` if the incident carries no such event yet (a defensive case -
    every `change` incident is created BY one).

    "Change-bearing" matters: events can arrive out of order, and a
    `build.failed` or deploy event landing after the PR's own event must
    not shadow it - its envelope carries no `pr`/`branch`/`files` and its
    actor is CI, not the author. Scanning newest-first for an event that
    actually carries the change metadata keeps a late CI event from
    wiping the author Review's self-review exclusion depends on."""
    events = [r for r in incident.event_records if r.source != EventSource.HELIX.value]
    if not events:
        return {}
    chosen = None
    for record in reversed(events):
        metadata = (record.envelope or {}).get("metadata") or {}
        if metadata.get("pr") is not None or metadata.get("branch"):
            chosen = record
            break
    if chosen is None:
        chosen = events[-1]
    metadata = (chosen.envelope or {}).get("metadata") or {}
    return {
        "pr": metadata.get("pr"),
        "title": metadata.get("title"),
        "branch": metadata.get("branch"),
        "sha": metadata.get("sha"),
        "base": metadata.get("base"),
        "files": list(metadata.get("files") or []),
        "author": (chosen.envelope or {}).get("actor") or "system",
    }
