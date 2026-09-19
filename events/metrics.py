"""What the event layer bought (subphase E7).

E1-E6 built ingestion, correlation, durable history and replay. E7 does
not add behaviour - it measures the four things plan.md names for the
event layer's exit criterion: correlation accuracy, duplicate handling,
end-to-end latency, and replay correctness. Everything here reads
already-durable state; nothing ingests, replays into, or otherwise
mutates it, so running these metrics against a live database is safe to
do repeatedly.

## The rule every function here follows

A measurement that cannot be made for some rows is not the same as those
rows passing. Each metric below therefore reports three things, not one:
how many were measured, how many of those passed, and how many could not
be assessed at all - counted separately, never folded into either side of
the rate. A `None` rate (rather than a manufactured `0.0` or `1.0`) is
what an empty or entirely-unassessable input looks like; a rate is only a
real number when it was computed from at least one measured row.

## What each metric excludes, and why

- **`correlation_accuracy`** takes ground truth as an argument - there is
  no way to derive "should have correlated" from the database itself,
  only "did correlate". An event named in a group that was never even
  ingested is not a correlation failure, it is a missing measurement, and
  is reported as such rather than counted against the group.

- **`duplicate_handling_rate`** never re-ingests. `event_records.event_id`
  carries a unique index (state/models.py), so ingest already prevents a
  literal second row; what this metric checks is that the events named as
  having been re-ingested really did converge on exactly one row rather
  than zero (never actually ingested - not assessable) or, if the unique
  constraint were ever weakened, more than one.

- **`event_latency`** excludes `source == "helix"` records. Helix's own
  lifecycle events (E5) are written and consumed in the same transaction
  that produces them, so their `created_at - occurred_at` is a
  measurement of the write path's own clock, not of how long it took
  Helix to notice something a vendor system reported. Including them
  would pull the whole distribution toward a number that flatters
  detection latency without actually being detection latency, so they
  are counted and excluded rather than silently dropped.

- **`replay_correctness`** treats an incident with no Helix lifecycle
  events as "not replayable", never as a failure. `events.history`
  already establishes that replay is reconstructed purely from stored
  Helix events (`project_from_events`) and returns `None` when there are
  none; scoring that `None` against live state as a mismatch would
  conflate "there was nothing to replay" with "replay got it wrong",
  which are different findings.

## Why `event_layer_report` needs no arguments beyond ground truth

`duplicate_handling_rate` and `replay_correctness` both take an explicit
scope elsewhere in this module, because a targeted call (e.g. against the
event ids a benchmark scenario deliberately redelivered) is the
informative one. The rollup has no such scenario-specific list to hand
it, so it scopes each to everything currently recorded: every event id in
`event_records` for duplicate handling, every incident for replay. That
makes the rollup a whole-store consistency check, which is what "rerun
the full benchmark through the event path and see if anything drifted"
(plan.md E7) needs; a caller who wants the sharper, scenario-specific
measurement calls the two functions directly.
"""

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from events.history import IncidentProjection, project_from_state, replay_incident
from events.schemas import EventSource
from state import models
from state.db import get_engine, session_scope


def _aware(value: datetime) -> datetime:
    """SQLite hands back naive datetimes where Postgres keeps the offset;
    comparing or subtracting the two raises. Normalize to UTC rather than
    forbidding the SQLite test engine (same convention as
    `events.ingest._aware`)."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _percentile(sorted_values: Sequence[float], fraction: float) -> float:
    """Linear-interpolation percentile over an already-sorted sequence.

    Only called with a non-empty `sorted_values`; the empty case is
    handled by callers so this never has to invent a value for "no data".
    """
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = (len(sorted_values) - 1) * fraction
    lower, upper = int(rank), min(int(rank) + 1, len(sorted_values) - 1)
    if lower == upper:
        return sorted_values[lower]
    weight = rank - lower
    return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight


def _projection_to_dict(projection: IncidentProjection) -> dict[str, Any]:
    """`IncidentProjection` carries enum members; a mismatch report needs
    to be JSON-serializable, so render them to their string values."""
    return {
        "incident_id": projection.incident_id,
        "service": projection.service,
        "environment": projection.environment,
        "severity": projection.severity,
        "lifecycle_status": projection.lifecycle_status.value,
        "milestones": [milestone.value for milestone in projection.milestones],
    }


# --- 1. Correlation accuracy ---------------------------------------------------


@dataclass(frozen=True)
class GroupResult:
    """One ground-truth group, checked against where its events actually
    landed."""

    event_ids: tuple[str, ...]
    #: True only when every event in the group was found, and all of them
    #: share one incident.
    correct: bool
    #: The incident each event landed on, in `event_ids` order. `None`
    #: where the event id was never ingested at all.
    incident_ids: tuple[str | None, ...]
    #: Event ids from this group that have no row in `event_records` -
    #: not a correlation failure, a missing measurement.
    missing_event_ids: tuple[str, ...]


@dataclass(frozen=True)
class CorrelationAccuracy:
    rate: float | None
    correct: int
    #: Number of groups - the rate's denominator.
    denominator: int
    groups: list[GroupResult]


def correlation_accuracy(
    session: Session, expected_groups: Sequence[Sequence[str]]
) -> CorrelationAccuracy:
    """Fraction of `expected_groups` whose events all landed on one incident.

    `expected_groups` is ground truth: each inner list names event ids
    that a correlation scenario says should share an incident. A group
    naming zero or one event id is trivially "one incident" as long as
    that event was actually ingested; an empty group has nothing to
    correlate and is scored as not correct, since "one incident" is not a
    claim that can be true of nothing.
    """
    all_ids = {event_id for group in expected_groups for event_id in group}
    incident_by_event: dict[str, str] = {}
    if all_ids:
        rows = session.execute(
            select(models.EventRecord.event_id, models.Incident.incident_id)
            .join(models.Incident)
            .where(models.EventRecord.event_id.in_(all_ids))
        ).all()
        incident_by_event = dict(rows)

    groups: list[GroupResult] = []
    correct = 0
    for group in expected_groups:
        incident_ids = tuple(incident_by_event.get(event_id) for event_id in group)
        missing = tuple(event_id for event_id in group if event_id not in incident_by_event)
        landed = {incident_id for incident_id in incident_ids if incident_id is not None}
        is_correct = not missing and len(landed) == 1
        if is_correct:
            correct += 1
        groups.append(
            GroupResult(
                event_ids=tuple(group),
                correct=is_correct,
                incident_ids=incident_ids,
                missing_event_ids=missing,
            )
        )

    denominator = len(expected_groups)
    rate = correct / denominator if denominator else None
    return CorrelationAccuracy(rate=rate, correct=correct, denominator=denominator, groups=groups)


# --- 2. Duplicate handling rate --------------------------------------------------


@dataclass(frozen=True)
class DuplicateHandlingRate:
    rate: float | None
    #: How many times the caller delivered an event, repeats included.
    #: Without this the metric cannot tell a stream that never redelivered
    #: anything from one whose redeliveries were all absorbed.
    deliveries: int
    #: deliveries - distinct events: how much redelivery actually occurred.
    #: A rate of 1.00 over zero redeliveries has proven nothing.
    redeliveries: int
    #: Appeared in `event_records` exactly once - correctly deduplicated.
    correctly_deduplicated: int
    #: Appeared more than once - the unique index did not hold. Only
    #: possible if that constraint is ever weakened; kept measured rather
    #: than assumed impossible.
    duplicated: int
    #: Appeared zero times - never actually ingested, so this event says
    #: nothing about duplicate handling either way.
    missing: int
    #: correctly_deduplicated + duplicated - the rate's denominator.
    denominator: int
    #: Distinct event ids requested, including the unassessable ones.
    requested: int


def duplicate_handling_rate(session: Session, event_ids: Sequence[str]) -> DuplicateHandlingRate:
    """Of the events in `event_ids` - the delivery log, repeats included -
    the fraction recorded exactly once in `event_records`.

    Reads the store as it stands; does not re-ingest anything.

    **Read `redeliveries` before reading `rate`.** One row per event id is
    enforced by the UNIQUE index on `event_records.event_id`, so this rate
    is 1.00 by construction whenever every id was ingested: it confirms
    the accounting, it does not discover a defect. A rate of 1.00 over
    `redeliveries=0` says only that nothing was redelivered.

    The behavioural half of duplicate handling - that a redelivered event
    creates no second incident, no extra correlation token and no extra
    transition, none of which the index protects - is asserted directly by
    `tests/events/test_ingest.py::TestReplay`. It is a property to be
    tested, not a rate to be averaged, which is why it is not folded in
    here.

    Pass the log with repeats intact. Deduplicating before calling makes
    `redeliveries` zero and quietly turns the metric into a tautology.
    """
    distinct_ids = sorted(set(event_ids))
    counts: dict[str, int] = {}
    if distinct_ids:
        rows = session.execute(
            select(models.EventRecord.event_id, func.count())
            .where(models.EventRecord.event_id.in_(distinct_ids))
            .group_by(models.EventRecord.event_id)
        ).all()
        counts = dict(rows)

    correctly_deduplicated = duplicated = missing = 0
    for event_id in distinct_ids:
        count = counts.get(event_id, 0)
        if count == 0:
            missing += 1
        elif count == 1:
            correctly_deduplicated += 1
        else:
            duplicated += 1

    denominator = correctly_deduplicated + duplicated
    rate = correctly_deduplicated / denominator if denominator else None
    return DuplicateHandlingRate(
        rate=rate,
        deliveries=len(event_ids),
        redeliveries=len(event_ids) - len(distinct_ids),
        correctly_deduplicated=correctly_deduplicated,
        duplicated=duplicated,
        missing=missing,
        denominator=denominator,
        requested=len(distinct_ids),
    )


# --- 3. End-to-end event latency -------------------------------------------------


@dataclass(frozen=True)
class EventLatency:
    #: Non-helix records the statistics below are computed over - the
    #: sample size, not a rate, but still the number that makes every
    #: other field here meaningful rather than a bare average.
    count: int
    median_seconds: float | None
    mean_seconds: float | None
    p95_seconds: float | None
    min_seconds: float | None
    max_seconds: float | None
    #: Helix's own lifecycle events, counted but never mixed into the
    #: statistics above - see the module docstring.
    excluded_helix_count: int


def event_latency(session: Session) -> EventLatency:
    """`created_at - occurred_at`, in seconds, over every vendor event
    Helix has recorded."""
    excluded_helix_count = (
        session.scalar(
            select(func.count())
            .select_from(models.EventRecord)
            .where(models.EventRecord.source == EventSource.HELIX.value)
        )
        or 0
    )
    rows = session.scalars(
        select(models.EventRecord).where(models.EventRecord.source != EventSource.HELIX.value)
    ).all()
    latencies = sorted(
        (_aware(row.created_at) - _aware(row.occurred_at)).total_seconds() for row in rows
    )

    if not latencies:
        return EventLatency(
            count=0,
            median_seconds=None,
            mean_seconds=None,
            p95_seconds=None,
            min_seconds=None,
            max_seconds=None,
            excluded_helix_count=excluded_helix_count,
        )

    mid = len(latencies) // 2
    median = latencies[mid] if len(latencies) % 2 else (latencies[mid - 1] + latencies[mid]) / 2
    return EventLatency(
        count=len(latencies),
        median_seconds=median,
        mean_seconds=sum(latencies) / len(latencies),
        p95_seconds=_percentile(latencies, 0.95),
        min_seconds=latencies[0],
        max_seconds=latencies[-1],
        excluded_helix_count=excluded_helix_count,
    )


# --- 4. Replay correctness -------------------------------------------------------


@dataclass(frozen=True)
class ReplayMismatch:
    incident_id: str
    state_projection: dict[str, Any]
    replay_projection: dict[str, Any]


@dataclass(frozen=True)
class ReplayCorrectness:
    rate: float | None
    correct: int
    incorrect: int
    #: correct + incorrect - the rate's denominator.
    denominator: int
    #: Incidents with no Helix lifecycle events to replay - `events.history`
    #: reconstructs nothing for these, and that is not a mismatch.
    not_replayable: int
    #: denominator + not_replayable.
    total_incidents: int
    mismatches: list[ReplayMismatch] = field(default_factory=list)


def replay_correctness(
    session: Session, incident_ids: Sequence[str] | None = None
) -> ReplayCorrectness:
    """Fraction of incidents whose `replay_incident` projection equals
    `project_from_state`.

    `incident_ids` scopes the check; `None` checks every incident in the
    store.
    """
    stmt = select(models.Incident)
    if incident_ids is not None:
        stmt = stmt.where(models.Incident.incident_id.in_(incident_ids))
    incidents = session.scalars(stmt).all()

    correct = incorrect = not_replayable = 0
    mismatches: list[ReplayMismatch] = []
    for incident in incidents:
        live = project_from_state(incident)
        replayed = replay_incident(session, incident.incident_id)
        if replayed is None:
            not_replayable += 1
            continue
        if replayed == live:
            correct += 1
        else:
            incorrect += 1
            mismatches.append(
                ReplayMismatch(
                    incident_id=incident.incident_id,
                    state_projection=_projection_to_dict(live),
                    replay_projection=_projection_to_dict(replayed),
                )
            )

    denominator = correct + incorrect
    rate = correct / denominator if denominator else None
    return ReplayCorrectness(
        rate=rate,
        correct=correct,
        incorrect=incorrect,
        denominator=denominator,
        not_replayable=not_replayable,
        total_incidents=len(incidents),
        mismatches=mismatches,
    )


# --- Rollup -----------------------------------------------------------------------


def event_layer_report(
    session: Session, expected_groups: Sequence[Sequence[str]] | None = None
) -> dict[str, Any]:
    """All four E7 measurements, as one JSON-serializable dict.

    `expected_groups` is optional because correlation accuracy is the one
    measurement with no way to derive its own ground truth from the
    store; without it, `correlation_accuracy` is reported as not
    measured - `denominator: 0`, `rate: null` - rather than silently
    skipped or scored as a pass. The other three are always scoped to
    everything currently recorded; see the module docstring for why.
    """
    if expected_groups is None:
        correlation: dict[str, Any] = {
            "rate": None,
            "correct": 0,
            "denominator": 0,
            "groups": [],
            "note": "not measured: no expected_groups (ground truth) provided",
        }
    else:
        correlation = asdict(correlation_accuracy(session, expected_groups))

    all_event_ids = session.scalars(select(models.EventRecord.event_id)).all()

    return {
        "correlation_accuracy": correlation,
        "duplicate_handling_rate": asdict(duplicate_handling_rate(session, all_event_ids)),
        "event_latency": asdict(event_latency(session)),
        "replay_correctness": asdict(replay_correctness(session)),
    }


# --- CLI (scripts/event-metrics.sh) -----------------------------------------------


def render_report_text(report: dict[str, Any]) -> str:
    """Human-readable form of `event_layer_report`'s output. Every rate is
    printed next to the denominator and counts that make it meaningful,
    per the module docstring's rule against a bare number."""
    corr = report["correlation_accuracy"]
    dup = report["duplicate_handling_rate"]
    lat = report["event_latency"]
    rep = report["replay_correctness"]

    lines = [
        "Event layer report (subphase E7)",
        "",
        f"correlation_accuracy: rate={corr['rate']} correct={corr['correct']} "
        f"denominator={corr['denominator']}" + (f" - {corr['note']}" if "note" in corr else ""),
        f"duplicate_handling_rate: rate={dup['rate']} "
        f"correctly_deduplicated={dup['correctly_deduplicated']} duplicated={dup['duplicated']} "
        f"missing={dup['missing']} denominator={dup['denominator']} "
        f"deliveries={dup['deliveries']} redeliveries={dup['redeliveries']}",
        f"event_latency: count={lat['count']} median_seconds={lat['median_seconds']} "
        f"mean_seconds={lat['mean_seconds']} p95_seconds={lat['p95_seconds']} "
        f"min_seconds={lat['min_seconds']} max_seconds={lat['max_seconds']} "
        f"excluded_helix_count={lat['excluded_helix_count']}",
        f"replay_correctness: rate={rep['rate']} correct={rep['correct']} "
        f"incorrect={rep['incorrect']} not_replayable={rep['not_replayable']} "
        f"denominator={rep['denominator']} total_incidents={rep['total_incidents']}",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """`scripts/event-metrics.sh`: `event_layer_report` against a live
    database in one command.

    No `expected_groups` argument - an ad hoc CLI invocation has no
    scenario-specific ground truth to hand `correlation_accuracy`, so it
    reports itself not measured rather than guessing one, exactly as
    `event_layer_report` already does when called without it.
    """
    parser = argparse.ArgumentParser(
        prog="python -m events.metrics",
        description="Event layer report (subphase E7): correlation accuracy, "
        "duplicate handling rate, event latency, and replay correctness, "
        "read from the live event store.",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="state DB URL (default HELIX_DATABASE_URL/settings)",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = parser.parse_args(argv)

    engine = get_engine(args.database_url)
    with session_scope(engine) as session:
        report = event_layer_report(session)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render_report_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
