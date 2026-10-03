"""What kind of evidence an incident rests on, as canonical event types.

One answer for every caller that has to ask. The policy gate uses it to
decide whether an action is coherent with the evidence (EXECUTOR_EVIDENCE),
and verification uses it to decide whether "recovered" must include the
running workload. If the two derived it separately, they could disagree
about the same incident.
"""

from events.classify import UnclassifiedSignal, classify
from events.schemas import EventType
from state import models

_EVENT_TYPES = frozenset(t.value for t in EventType)


def incident_evidence_types(incident: models.Incident) -> frozenset[str]:
    """Canonical types of the incident's own evidence: vendor event records
    as recorded (Helix's lifecycle events excluded - they say when Helix
    reacted, not what broke) plus each Phase 1 signal classified. A signal
    `classify` cannot name contributes nothing rather than a guess."""
    kinds: set[str] = set()
    for record in incident.event_records:
        if record.source != "helix" and record.event_type in _EVENT_TYPES:
            kinds.add(record.event_type)
    for signal in incident.signals:
        try:
            kinds.add(classify(signal.source, dict(signal.signature or {})).value)
        except UnclassifiedSignal:
            pass
    return frozenset(kinds)
