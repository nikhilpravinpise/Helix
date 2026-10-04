"""Poll GitHub for pull request activity (`helix watch --source github`).

The change-review lifecycle (2.5-2.9) starts from a PR event, and those had
only ever come from the benchmark's own event dicts. GitHub can push them
as webhooks, but a webhook needs a public URL into this machine. Polling
needs nothing inbound: ask GitHub for the open PRs every few seconds and
turn what changed into the same webhook-shaped payload GitHub would have
pushed, so polled and pushed PRs share one adapter path
(`events.adapters.github.pull_request_events`).

Only change is reported. A PR seen for the first time is `opened`; one
that closed and came back is `reopened`; a PR whose head sha moved is
`synchronize`; anything else is silent. The adapter
derives each event's id from the PR number and head sha, so even a restart
that re-reports a PR cannot drive anything twice - ingest records it as a
duplicate.
"""

from typing import Any


class PullRequestTracker:
    """Turns successive open-PR listings into webhook payloads for what
    changed between them.

    The first listing is a baseline: PRs already open when the watcher
    starts are not announced unless `include_existing` - on the sandbox at
    the time that was 17 Helix fix PRs, each of which would otherwise have
    become a review incident at once.
    """

    def __init__(self, *, include_existing: bool = False) -> None:
        self._heads: dict[int, str] | None = None
        self._include_existing = include_existing
        #: Every PR number ever seen open, so one that closes and comes
        #: back is reported as `reopened`, not as a fresh `opened`.
        self._seen: set[int] = set()

    def observe(self, open_prs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        current = {int(pr["number"]): pr for pr in open_prs if pr.get("number") is not None}
        heads = {
            number: str((pr.get("head") or {}).get("sha") or "") for number, pr in current.items()
        }
        first = self._heads is None
        previous = self._heads or {}
        self._heads = heads
        seen_before = set(self._seen)
        self._seen.update(heads)
        if first and not self._include_existing:
            return []

        payloads: list[dict[str, Any]] = []
        for number, pr in sorted(current.items()):
            if number not in previous:
                action = "reopened" if number in seen_before else "opened"
            elif previous[number] != heads[number]:
                action = "synchronize"
            else:
                continue
            payloads.append(
                {
                    "action": action,
                    "number": number,
                    "pull_request": pr,
                    "sender": pr.get("user") or {},
                }
            )
        return payloads
