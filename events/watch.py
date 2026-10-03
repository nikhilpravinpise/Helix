"""Watch the live cluster and publish confirmed failures (`helix watch`).

The E3 adapters turn an already-fetched pod list into canonical events, and
D18 deliberately left the fetching to `integrations/` - but no loop ever
called them, so a crashing pod only reached Helix through
`scripts/emit-signal.sh`. This is that loop: fetch pods, adapt, filter,
publish to Kafka, where `helix consume --run` picks them up. Nothing here
decides or executes anything.

The filter is the part that matters. A healthy `rollout restart` of
payment-api, sampled 60 times, read as running-but-not-Ready in 48 of the
samples while the new pod warmed up. Published as-is, every deploy - every
fix Helix itself deploys included - would open an incident. A condition is
therefore published only once it has been seen on `polls` consecutive
polls, once per occurrence: it is published again only if it clears and
comes back.
"""

import time
from collections.abc import Callable

from events.schemas import EngineeringEvent
from helix.logging import get_logger


def _condition(event: EngineeringEvent) -> tuple[str, str]:
    """What persists across polls: the pod and the kind of problem. Not the
    event id - a crash loop mints a new id per restart, and that must not
    reset the count."""
    return (str(event.metadata.get("pod") or event.event_id), str(event.event_type))


class PersistenceFilter:
    """Passes an event only when its condition has persisted for `polls`
    consecutive observations, and only once while it lasts."""

    def __init__(self, polls: int = 3) -> None:
        if polls < 1:
            raise ValueError("polls must be at least 1")
        self._polls = polls
        self._seen: dict[tuple[str, str], int] = {}
        self._reported: set[tuple[str, str]] = set()

    def observe(self, events: list[EngineeringEvent]) -> list[EngineeringEvent]:
        current = {_condition(e): e for e in events}
        # A condition missing from this poll has cleared: forget it, so a
        # recurrence starts counting - and is reported - afresh.
        for gone in set(self._seen) - set(current):
            del self._seen[gone]
            self._reported.discard(gone)
        confirmed: list[EngineeringEvent] = []
        for key, event in current.items():
            self._seen[key] = self._seen.get(key, 0) + 1
            if self._seen[key] >= self._polls and key not in self._reported:
                self._reported.add(key)
                confirmed.append(event)
        return confirmed


def run_watch(
    fetch: Callable[[], list[EngineeringEvent]],
    publish: Callable[[EngineeringEvent], None],
    *,
    interval: float = 10.0,
    polls: int = 3,
    max_cycles: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Poll `fetch` every `interval` seconds and `publish` each confirmed
    problem. Returns the number of polls made.

    A fetch that fails is logged and the watch carries on: an API server
    blip must not leave the cluster unwatched until someone notices. The
    failed poll counts as no observation, so it neither confirms nor
    clears a condition.
    """
    log = get_logger()
    gate = PersistenceFilter(polls=polls)
    cycles = 0
    while max_cycles is None or cycles < max_cycles:
        cycles += 1
        try:
            events = fetch()
        except Exception as exc:  # keep watching through a failed poll
            log.warning("watch poll failed", error=str(exc))
        else:
            for event in gate.observe(events):
                publish(event)
        if max_cycles is None or cycles < max_cycles:
            sleep(interval)
    return cycles
