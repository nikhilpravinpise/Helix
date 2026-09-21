"""Ground-truth correlation groups (subphase E7).

Each `CorrelationGroup` names a set of canonical events that should land
on one incident, built by feeding real captured evidence from
`fixtures/faults/` through the same adapters (`events/adapters/*.py`) that
turn a real vendor payload into an `EngineeringEvent`. Nothing here
invents a token, a metadata field, or an adapter rule - `run.py` ingests
exactly what these functions return.

## Why two groups, and why these two

`benchmark/scenarios/*.yaml` each declare one `signal:` block, one event,
which cannot demonstrate cross-source correlation at all - there is
nothing for a second event to correlate with. A group needs at least two
events from at least two sources whose evidence was captured *together*,
so the claim "these belong to one incident" is not invented on top of two
unrelated captures.

Two fixtures qualify without any cross-fixture stitching:

- `github/failed-check.json` already carries both a GitHub check-run
  failure AND the Jenkins build record for the same commit in one
  capture (see its `jenkins_note`: the fixture models the world after a
  transient check failure was rerun to green). One capture, two vendor
  sources, one documented incident.
- `cluster/crashloop.json` carries the failing pod, the cluster's own
  Warning event about that pod, and its error-level log lines in one
  capture. Three sources' worth of evidence about one pod.

No other captured fixture pairs two sources against one documented
incident without inventing a shared timeline across unrelated captures,
so this module does not force a fourth source in or manufacture a
GitHub -> Jenkins -> Kubernetes -> ELK chain the way
`tests/events/test_ingest.py::four_source_chain` does for the unit tests.
That helper is a hand-built example of the mechanism; these groups are
what the mechanism actually does with a real capture.

## ELK is the one synthesized envelope, and only the envelope

`fixtures/faults/*` stores logs as pre-formatted
`<timestamp> [<pod>] <message>` strings - there is no captured raw
Elasticsearch `_search` response for any fault, because nothing in this
project's fixture capture ever queried ES directly (the same gap
`tests/events/test_adapters.py::TestElkAdapter` documents and works
around). The message text, pod name and timestamp below are copied
verbatim from `cluster/crashloop.json`'s `logs_payment_api`; only the
`{hits: {hits: [...]}}` transport shape the ELK adapter expects is
built by hand, exactly as that test class already does.

## Why every event's `timestamp` is set explicitly, backdated to capture time

None of the adapters in `events/adapters/*.py` populate `timestamp` from
the vendor payload - it defaults to "now" (`EngineeringEvent`'s
`default_factory`), which is correct for a real producer processing a
webhook as it arrives. It is wrong here: running every adapter in one
script invocation would stamp both groups within the same few seconds of
each other, when the fixtures were actually captured roughly a day apart
(`cluster/crashloop.json`: 2026-08-28T12:14Z; `github/failed-check.json`:
2026-08-29T10:0xZ). `events.ingest._correlated_incident` only looks at an
open incident's most recent event and `CORRELATION_WINDOW` (30 minutes);
leaving both groups at "now" put them inside that window of each other
and, because both use `service:payment-api:sandbox`, the second group
silently attached to the first group's incident instead of opening its
own - a real instance of the exact risk `events/correlation.py`'s module
docstring names the service token as ("on a single-service sandbox it
would merge every payment-api event ever seen into one incident").

That merge is not a defect in `correlation_accuracy` - each group's own
events still shared one incident, which is everything the metric checks
- but it made this ground truth misrepresent two unrelated fixture
captures as one incident. Restoring each event's real captured timestamp
(from the fixture's own `completed_at` / `lastTimestamp` / `captured_at`
/ log line fields) removes that artifact and is the more accurate ground
truth regardless: these events did not really happen "now", they happened
when the fixture says they did. See `run.py`'s report and
`docs/benchmark/phase-1.5-evaluation.md` for how close this came to
masking a real correlation risk.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from events.adapters import github, jenkins, kubernetes
from events.adapters.elk import log_events
from events.schemas import EngineeringEvent

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_ROOT = REPO_ROOT / "fixtures" / "faults"

#: The pod this project's crashloop fault always names - the one
#: `tests/events/test_ingest.py::four_source_chain` also uses, because it
#: is the pod `cluster/crashloop.json` actually captured failing.
_CRASHLOOP_POD = "payment-api-98867c74d-h6j8v"

#: The exact error line `cluster/crashloop.json` captured for that pod,
#: and the timestamp it was logged at.
_CRASHLOOP_LOG_MESSAGE = (
    "ERROR:payment_api:startup failed: invalid configuration: 1 validation error for Settings"
)
_CRASHLOOP_LOG_TIMESTAMP = "2026-08-28T12:14:22.191Z"


def _load(relative: str) -> dict[str, Any]:
    return json.loads((FIXTURES_ROOT / relative).read_text(encoding="utf-8"))


def _at(event: EngineeringEvent, iso_timestamp: str) -> EngineeringEvent:
    """Replace an adapter-produced event's `timestamp` (adapter default:
    "now") with a real one captured in the fixture. See the module
    docstring for why this is not optional."""
    return event.model_copy(update={"timestamp": datetime.fromisoformat(iso_timestamp)})


@dataclass(frozen=True)
class CorrelationGroup:
    """One ground-truth group: the events that should land on one
    incident, and where their evidence came from."""

    name: str
    source_fixture: str
    events: tuple[EngineeringEvent, ...]


def _failed_check_group() -> CorrelationGroup:
    """GitHub's failing check-run plus the Jenkins build it names, both
    from the one `github/failed-check.json` capture.

    The Jenkins event is `build.passed`, not `build.failed`: the fixture
    captures the state *after* the transient check was rerun to green
    (see its `jenkins_note`). That is still one incident's worth of
    evidence - the failing check and the build record that resolved it -
    which is what this group asserts.
    """
    fixture = _load("github/failed-check.json")
    check_run = fixture["evidence"]["github"]["check_runs"]["items"][0]
    (check_event,) = github.check_run_events(fixture["evidence"]["github"]["check_runs"])
    check_event = _at(check_event, check_run["completed_at"])

    build = jenkins.build_event(fixture)
    assert build is not None  # jenkins_result is SUCCESS in this fixture, always an event
    build = _at(build, fixture["captured_at"])

    return CorrelationGroup(
        name="failed-check",
        source_fixture="github/failed-check.json",
        events=(check_event, build),
    )


def _crashloop_group() -> CorrelationGroup:
    """The crashing pod, the cluster's own Warning about it, and its
    error log line - three sources, one `cluster/crashloop.json` capture.

    `object_events` runs over every Warning `cluster/crashloop.json`
    captured, most of which describe *other* pods from earlier restart
    attempts of the same Deployment (this fault's evidence spans several
    crash cycles). Only the Warning naming the one pod this group is
    about is kept, so the group's claim stays "this pod's Warning
    correlates with this pod's crash", not "some Warning somewhere does".
    """
    fixture = _load("cluster/crashloop.json")
    pod_failures = kubernetes.pod_events(fixture["evidence"]["pods_payment_api"])
    pod_failure = next(e for e in pod_failures if e.metadata.get("pod") == _CRASHLOOP_POD)
    # The container's own terminated.finishedAt - when it actually crashed.
    failing_pod = next(
        p
        for p in fixture["evidence"]["pods_payment_api"]["items"]
        if p["metadata"]["name"] == _CRASHLOOP_POD
    )
    finished_at = failing_pod["status"]["containerStatuses"][0]["lastState"]["terminated"][
        "finishedAt"
    ]
    pod_failure = _at(pod_failure, finished_at)

    warnings = fixture["evidence"]["events"]["items"]
    this_pod_warnings = [
        item
        for item in warnings
        if item.get("type") == "Warning"
        and item.get("involvedObject", {}).get("name") == _CRASHLOOP_POD
    ]
    (warning,) = this_pod_warnings
    object_event = next(iter(kubernetes.object_events({"items": this_pod_warnings})))
    object_event = _at(object_event, warning["lastTimestamp"])

    log_response = {
        "hits": {
            "hits": [
                {
                    "_index": "helix-logs-2026.08.28",
                    "_id": "crashloop-doc0",
                    "_source": {
                        "@timestamp": _CRASHLOOP_LOG_TIMESTAMP,
                        "message": _CRASHLOOP_LOG_MESSAGE,
                        "kubernetes": {"pod": {"name": _CRASHLOOP_POD}},
                    },
                }
            ]
        }
    }
    log_event = next(iter(log_events(log_response)))
    log_event = _at(log_event, _CRASHLOOP_LOG_TIMESTAMP)

    return CorrelationGroup(
        name="crashloop",
        source_fixture="cluster/crashloop.json",
        events=(pod_failure, object_event, log_event),
    )


def build_groups() -> list[CorrelationGroup]:
    """The full set of ground-truth correlation groups."""
    return [_failed_check_group(), _crashloop_group()]
