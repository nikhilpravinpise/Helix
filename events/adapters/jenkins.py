"""Jenkins payloads to canonical events (subphase E3).

Consumes a build record - job name, build number, result, and optionally
the console output - and emits one event per build. Jenkins' result
vocabulary (`FAILURE`, `SUCCESS`, `UNSTABLE`, `ABORTED`, null while
running) stops here.

`UNSTABLE` maps to `build.failed`. Jenkins uses it for a build whose
steps ran but whose tests failed, which is precisely the `test-regression`
scenario, and treating it as anything softer would mean Helix ignored a
real CI failure.
"""

from typing import Any

from events.adapters._common import vendor_event
from events.schemas import (
    EngineeringEvent,
    EventSource,
    EventType,
    derive_event_id,
)

#: Jenkins result to canonical type. ABORTED is deliberately absent: a
#: human cancelled the build, which is not a failure of the system and
#: not something to open an incident for.
_RESULTS = {
    "FAILURE": EventType.BUILD_FAILED,
    "UNSTABLE": EventType.BUILD_FAILED,
    "SUCCESS": EventType.BUILD_PASSED,
}

#: Console output is evidence, not envelope. Clipped so one runaway build
#: log cannot push a record past the broker's message limit; the full log
#: stays available through the Jenkins evidence path.
_CONSOLE_LIMIT = 4000


def _clip(text: str, limit: int = _CONSOLE_LIMIT) -> tuple[str, bool]:
    """Clip a console log from the middle, keeping both ends.

    Keeping only the head would be the same mistake `query-logs.sh` made:
    truncating toward one end of a window that has evidence at both. The
    captured `test-regression` log shows why - its failing assertion is in
    the first 4000 characters while `ERROR: script returned exit code 1`
    and `Finished: FAILURE` are in the last 400, so a head-only clip keeps
    the diagnosis and drops the verdict, and a tail-only clip does the
    reverse. The elision is marked inline, and `console_truncated` says so
    in the metadata, because the one unacceptable option is doing it
    quietly.
    """
    if len(text) <= limit:
        return text, False
    half = limit // 2
    elided = len(text) - (half * 2)
    return f"{text[:half]}\n...[{elided} characters elided]...\n{text[-half:]}", True


def build_event(
    payload: dict[str, Any],
    service: str | None = None,
    environment: str = "sandbox",
) -> EngineeringEvent | None:
    """One canonical event for a Jenkins build, or None if it is not a
    terminal outcome worth an event.

    Accepts both the fixture envelope (`jenkins_job`, `jenkins_result`)
    and the plain API shape (`job`, `result`), because those are the two
    forms this payload actually arrives in.
    """
    job = payload.get("jenkins_job") or payload.get("job") or payload.get("fullDisplayName")
    number = payload.get("jenkins_build_number") or payload.get("number")
    result = payload.get("jenkins_result") or payload.get("result")

    if result is None:
        # Still building. build.started belongs to a webhook on build
        # start, which has no result field at all - a null result on a
        # record that should have one is not something to guess at.
        return None
    event_type = _RESULTS.get(str(result).upper())
    if event_type is None:
        return None

    console = payload.get("console_output") or (payload.get("evidence") or {}).get("console_output")
    metadata: dict[str, Any] = {
        "job": job,
        "build_number": number,
        "result": result,
        "url": payload.get("jenkins_build_url") or payload.get("url"),
    }
    if console:
        clipped, truncated = _clip(console)
        metadata["console_output"] = clipped
        metadata["console_truncated"] = truncated

    return vendor_event(
        # Natural key: a job plus build number is unique and immutable in
        # Jenkins, and it is exactly what a redelivered webhook repeats.
        event_id=derive_event_id("jenkins", job, number),
        event_type=event_type,
        source=EventSource.JENKINS,
        # The job builds the service, so absent an explicit mapping the
        # job name is the best available answer.
        service=service or str(job or "unknown"),
        environment=environment,
        metadata=metadata,
    )
