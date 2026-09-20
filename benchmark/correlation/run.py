"""Correlation accuracy against a live event store (subphase E7).

`scripts/correlation-accuracy.sh`: ingests `groups.build_groups()`'s
events through the real `events.ingest.ingest_event` entry point - the
same one the Kafka consumer and the benchmark harness's event path drive
- then reports `events.metrics.correlation_accuracy` against the ground
truth those groups declare.

Idempotent. Every event id here is derived from vendor payload identity
(`events.schemas.derive_event_id`, called by the adapters in
`groups.py`), so rerunning this against the same database attaches to the
incidents the first run created rather than duplicating anything - the
same replay guarantee `events.ingest` gives any other caller.

## Events are ingested in timestamp order, deliberately

`events.ingest._correlated_incident`'s window check is
`occurred_at - latest <= CORRELATION_WINDOW`, which only bounds the
FUTURE side: if a new event's `occurred_at` is *earlier* than the open
incident's latest event (an out-of-order arrival), the subtraction is
negative and the check passes no matter how far apart the two really are.
Ingesting this module's two groups out of chronological order (the
`failed-check` group, 2026-08-29, before the `crashloop` group,
2026-08-28) reproduces this: the second group's first event lands 22
hours *before* the first group's incident's latest event, the window
check does not catch it, and both unrelated groups silently end up on one
incident. Ingesting oldest-first does not hit it, because every
subsequent event is then further in the future, which the window bounds
correctly. Sorting by `timestamp` here avoids reporting a
`correlation_accuracy` number that is an artifact of this script's own
ingestion order rather than of the ground truth; the order-dependent
false merge itself is a real finding and is documented in
`docs/benchmark/phase-1.5-evaluation.md`, not hidden by the sort.
"""

import argparse
import json
import sys
from dataclasses import asdict
from itertools import combinations
from typing import Any

from sqlalchemy.orm import Session

from benchmark.correlation.groups import CorrelationGroup, build_groups
from events.correlation import tokens_for
from events.ingest import ingest_event
from events.metrics import correlation_accuracy
from state.db import get_engine, session_scope


def _ingest_groups(session: Session, groups: list[CorrelationGroup]) -> None:
    """Ingest every event in every group, oldest occurred_at first - see
    the module docstring for why the order is not arbitrary. Errors are
    not swallowed: a failed ingest here means the ground truth itself
    cannot be measured, which is worth stopping on rather than reporting
    a hollow accuracy."""
    all_events = [(event, group) for group in groups for event in group.events]
    all_events.sort(key=lambda pair: pair[0].timestamp)
    for event, _group in all_events:
        ingest_event(session, event)


def _cross_group_merges(
    groups: list[CorrelationGroup], group_results: list[dict[str, Any]]
) -> list[tuple[str, str, str]]:
    """Pairs of DIFFERENT groups that landed on the same incident.

    `correlation_accuracy` only checks that each group's own events share
    one incident - it has no way to know two groups were meant to be
    different incidents, so this is reported separately rather than
    folded into `correct`/`denominator`.
    """
    incident_of: dict[str, set[str]] = {}
    for group, result in zip(groups, group_results, strict=True):
        incident_of[group.name] = {i for i in result["incident_ids"] if i is not None}
    merges = []
    for (name_a, incidents_a), (name_b, incidents_b) in combinations(incident_of.items(), 2):
        shared = incidents_a & incidents_b
        for incident_id in shared:
            merges.append((name_a, name_b, incident_id))
    return merges


def render_text(
    groups: list[CorrelationGroup],
    report: dict[str, Any],
    merges: list[tuple[str, str, str]],
) -> str:
    lines = [
        "Correlation accuracy (subphase E7) - ground truth from benchmark/correlation/groups.py",
        "",
        f"rate={report['rate']} correct={report['correct']} denominator={report['denominator']}",
        "",
    ]
    # Groups are declared and measured in the same order, so pairing by
    # position recovers the name/source_fixture the raw `GroupResult`
    # (event ids only) does not carry.
    for group, group_result in zip(groups, report["groups"], strict=True):
        status = "OK" if group_result["correct"] else "FAILED TO CORRELATE"
        lines.append(f"[{status}] {group.name} (from {group.source_fixture})")
        for event, incident_id in zip(group.events, group_result["incident_ids"], strict=True):
            lines.append(
                f"    {event.source.value:10s} {event.event_id}  "
                f"-> incident={incident_id}  tokens={sorted(tokens_for(event))}"
            )
        if group_result["missing_event_ids"]:
            lines.append(f"    missing (never ingested): {group_result['missing_event_ids']}")
        lines.append("")
    if merges:
        lines.append(
            "WARNING: groups meant to be separate incidents share one - "
            "correlation_accuracy cannot see this, it only checks a group "
            "against itself:"
        )
        for name_a, name_b, incident_id in merges:
            lines.append(f"    {name_a} + {name_b} both landed on {incident_id}")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m benchmark.correlation.run",
        description="Correlation accuracy (subphase E7), measured against the "
        "ground truth in benchmark/correlation/groups.py on a live event store.",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="state DB URL (default HELIX_DATABASE_URL/settings)",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = parser.parse_args(argv)

    groups = build_groups()
    expected_groups = [[event.event_id for event in group.events] for group in groups]

    engine = get_engine(args.database_url)
    with session_scope(engine) as session:
        _ingest_groups(session, groups)
        result = correlation_accuracy(session, expected_groups)
    report = asdict(result)
    merges = _cross_group_merges(groups, report["groups"])
    report["cross_group_merges"] = [
        {"group_a": a, "group_b": b, "incident_id": i} for a, b, i in merges
    ]

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render_text(groups, report, merges))
    return 0


if __name__ == "__main__":
    sys.exit(main())
