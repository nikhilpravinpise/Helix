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

    def observe(
        self, events: list[EngineeringEvent]
    ) -> tuple[list[EngineeringEvent], Callable[[], None]]:
        current = {_condition(e): e for e in events}
        # Calculate what to clear and what to report, but don't commit state yet.
        to_clear = set(self._seen) - set(current)
        new_seen = dict(self._seen)
        for gone in to_clear:
            del new_seen[gone]
        confirmed: list[EngineeringEvent] = []
        to_report: set[tuple[str, str]] = set()
        for key, event in current.items():
            new_seen[key] = new_seen.get(key, 0) + 1
            if new_seen[key] >= self._polls and key not in self._reported:
                confirmed.append(event)
                to_report.add(key)

        def commit() -> None:
            """Finalize state changes only after a successful publish."""
            for gone in to_clear:
                self._reported.discard(gone)
            self._seen = new_seen
            self._reported.update(to_report)

        return confirmed, commit


def run_watch(
    fetch: Callable[[], list[EngineeringEvent]],
    publish: Callable[[EngineeringEvent], None],
    *,
    interval: float = 10.0,
    polls: int = 3,
    max_cycles: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
    after_publish: Callable[[], None] | None = None,
) -> int:
    """Poll `fetch` every `interval` seconds and `publish` each confirmed
    problem. Returns the number of polls made.

    A fetch that fails is logged and the watch carries on: an API server
    blip must not leave the cluster unwatched until someone notices. The
    failed poll counts as no observation, so it neither confirms nor
    clears a condition. A publish that fails is also logged and does not
    commit observed state, so the same condition is reported again on
    the next successful poll. `after_publish` runs once a poll's events
    are all published, so a caller with its own state (the GitHub PR
    tracker) saves it at the same point.
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
            confirmed_events, commit = gate.observe(events)
            try:
                for event in confirmed_events:
                    publish(event)
            except Exception as exc:  # keep watching through a failed publish
                log.warning("publish failed", error=str(exc))
            else:
                commit()  # Only commit state after successful publish
                if after_publish is not None:
                    after_publish()
        if max_cycles is None or cycles < max_cycles:
            sleep(interval)
    return cycles
