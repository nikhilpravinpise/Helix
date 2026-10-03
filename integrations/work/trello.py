"""Trello implementation of the work-management contract (subphase 2.10).

Trello REST (api.trello.com/1, key + token on every request, JSON bodies
for writes). Trello has no workflow engine - board LISTS are the whole
workflow - which is exactly the declared-difference story 2.10 exists
to surface:

- `capabilities().priority` is False: Trello has no native priority, so
  create/update accept-and-ignore it per the contract.
- `capabilities().states` is DERIVED from the board's actual lists - a
  neutral status no list can express is undeclared and raises
  WorkItemError at transition, so a missing Blocked list is a declared
  finding, never a silent approximation. `closed` is always declared:
  card archival is Trello's native closed concept (a Closed-named list
  is preferred when the board has one).
- The dedup marker is `helix-incident:<id>` in the card description;
  `find_by_incident` scans the board's cards for it, and
  `update_work_item` preserves the marker across description rewrites.

Every failure path raises WorkItemError - the single exception
vocabulary the outbox drain records.
"""

import re
from typing import Any

import httpx

from integrations.evidence import EvidenceError
from integrations.http import raise_for_status
from integrations.work.contract import (
    NEUTRAL_STATUSES,
    ProviderCapabilities,
    WorkItemError,
    WorkItemRef,
    WorkItemRejected,
    WorkItemSpec,
)
from integrations.work.memory import NEUTRAL_LINK_TYPES

TRELLO_API = "https://api.trello.com/1"

#: The dedup marker embedded in card descriptions (find_by_incident's
#: search term); preserved across description updates by
#: `_preserve_marker`.
_MARKER_PREFIX = "helix-incident:"
_MARKER_RE = re.compile(r"helix-incident:\S+")

#: Neutral status -> acceptable board-list names, matched
#: case-insensitively. `closed` additionally falls back to card
#: archival when no Closed-named list exists.
_NEUTRAL_TO_TRELLO_LIST: dict[str, tuple[str, ...]] = {
    "open": ("to do", "todo", "backlog", "open", "new"),
    "in_progress": ("doing", "in progress", "in-progress"),
    "blocked": ("blocked", "on hold"),
    "done": ("done", "complete", "completed"),
    "closed": ("closed", "cancelled", "canceled"),
}


def _preserve_marker(old_description: str, new_description: str) -> str:
    """Keep the dedup marker on a description rewrite - losing it would
    let a later find_by_incident miss and the drain mint a duplicate."""
    if _MARKER_RE.search(new_description):
        return new_description
    match = _MARKER_RE.search(old_description or "")
    return f"{new_description}\n\n{match.group(0)}" if match else new_description


class TrelloProvider:
    """WorkManagementProvider over the Trello REST API."""

    name = "trello"

    def __init__(
        self,
        api_key: str,
        api_token: str,
        board_id: str,
        *,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._auth = {"key": api_key, "token": api_token}
        self._board_id = board_id
        self._client = client or httpx.Client(timeout=timeout)
        self._lists: list[dict[str, Any]] | None = None
        self._members: list[dict[str, Any]] | None = None
        self._labels: list[dict[str, Any]] | None = None
        self._caps: ProviderCapabilities | None = None

    # -- plumbing ---------------------------------------------------

    def _raw(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        params = {**self._auth, **(kwargs.pop("params", None) or {})}
        try:
            return self._client.request(method, f"{TRELLO_API}{path}", params=params, **kwargs)
        except httpx.TimeoutException as exc:
            raise WorkItemError(f"trello timed out: {exc}") from exc
        except httpx.TransportError as exc:
            raise WorkItemError(f"trello transport error: {exc}") from exc

    @staticmethod
    def _check(response: httpx.Response) -> None:
        try:
            raise_for_status(response)
        except EvidenceError as exc:
            raise WorkItemError(str(exc)) from exc

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        response = self._raw(method, path, **kwargs)
        self._check(response)
        return response

    @staticmethod
    def _json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise WorkItemError("trello returned a non-JSON response") from exc

    # -- board data (lists, members, labels) --------------------------

    def _board_lists(self) -> list[dict[str, Any]]:
        """The board's lists - the workflow itself. Cached: they drive
        both capabilities() and transition mapping."""
        if self._lists is None:
            self._lists = self._json(self._request("GET", f"/boards/{self._board_id}/lists")) or []
        return self._lists

    def _board_members(self) -> list[dict[str, Any]]:
        if self._members is None:
            self._members = (
                self._json(self._request("GET", f"/boards/{self._board_id}/members")) or []
            )
        return self._members

    def _board_labels(self) -> list[dict[str, Any]]:
        if self._labels is None:
            self._labels = (
                self._json(self._request("GET", f"/boards/{self._board_id}/labels")) or []
            )
        return self._labels

    def _list_for(self, status: str) -> dict[str, Any] | None:
        names = _NEUTRAL_TO_TRELLO_LIST[status]
        return next(
            (lst for lst in self._board_lists() if str(lst.get("name") or "").lower() in names),
            None,
        )

    def _member_id(self, assignee: str) -> str:
        wanted = assignee.lower()
        for member in self._board_members():
            names = {
                str(member.get("username") or "").lower(),
                str(member.get("fullName") or "").lower(),
            }
            if wanted in names:
                return str(member["id"])
        raise WorkItemRejected(f"no trello member matches {assignee!r}")

    def _label_ids(self, names: list[str]) -> list[str]:
        labels = self._board_labels()
        ids = []
        for name in names:
            found = next(
                (lab for lab in labels if str(lab.get("name") or "").lower() == name.lower()),
                None,
            )
            if found is None:
                found = self._json(
                    self._request("POST", "/labels", json={"idBoard": self._board_id, "name": name})
                )
                labels.append(found)
            ids.append(found["id"])
        return ids

    # -- refs ---------------------------------------------------------

    def _card_status(self, card: dict[str, Any]) -> str:
        if card.get("closed"):
            return "closed"
        current = next(
            (lst for lst in self._board_lists() if lst.get("id") == card.get("idList")),
            None,
        )
        name = str((current or {}).get("name") or "").lower()
        for neutral, names in _NEUTRAL_TO_TRELLO_LIST.items():
            if name in names:
                return neutral
        return "open"

    def _ref(self, card: dict[str, Any]) -> WorkItemRef:
        member = next(
            (m for m in self._board_members() if m.get("id") in (card.get("idMembers") or [])),
            None,
        )
        return WorkItemRef(
            external_id=str(card.get("id") or ""),
            external_key=card.get("shortLink"),
            external_url=card.get("url"),
            status=self._card_status(card),
            assignee=(member or {}).get("username") or (member or {}).get("fullName"),
            priority=None,  # declared gap: Trello has no native priority
            raw=card,
        )

    # -- contract -----------------------------------------------------

    def capabilities(self) -> ProviderCapabilities:
        """Derived from the board's real lists: the neutral words a
        Trello board can express are exactly the ones its lists map to,
        plus `closed` - card archival is always expressible."""
        if self._caps is None:
            states = sorted(
                s for s in NEUTRAL_STATUSES if s == "closed" or self._list_for(s) is not None
            )
            self._caps = ProviderCapabilities(priority=False, states=states)
        return self._caps

    def create_work_item(self, spec: WorkItemSpec) -> WorkItemRef:
        target = self._list_for("open")
        if target is None:
            raise WorkItemError("trello board has no list for 'open' work items")
        marker = f"{_MARKER_PREFIX}{spec.incident_id}"
        body: dict[str, Any] = {
            "idList": target["id"],
            "name": spec.title,
            "desc": f"{spec.description}\n\n{marker}" if spec.description else marker,
        }
        if spec.labels:
            body["idLabels"] = self._label_ids(spec.labels)
        if spec.assignee:
            body["idMembers"] = [self._member_id(spec.assignee)]
        card = self._json(self._request("POST", "/cards", json=body))
        return self._ref(card)

    def get_work_item(self, external_id: str) -> WorkItemRef | None:
        response = self._raw("GET", f"/cards/{external_id}")
        if response.status_code == 404:
            return None
        self._check(response)
        return self._ref(self._json(response))

    def find_by_incident(self, incident_id: str) -> WorkItemRef | None:
        marker = f"{_MARKER_PREFIX}{incident_id}"
        cards = self._json(self._request("GET", f"/boards/{self._board_id}/cards")) or []
        for card in cards:
            if marker in str(card.get("desc") or ""):
                return self._ref(card)
        return None

    def update_work_item(
        self,
        external_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
        priority: str | None = None,
    ) -> WorkItemRef:
        # `priority` is a declared-absent capability: accepted and
        # ignored per the contract, never silently approximated.
        body: dict[str, Any] = {}
        if title is not None:
            body["name"] = title
        if description is not None:
            current = self.get_work_item(external_id)
            if current is None:
                raise WorkItemError(f"trello card {external_id!r} not found")
            body["desc"] = _preserve_marker(str(current.raw.get("desc") or ""), description)
        if not body:
            ref = self.get_work_item(external_id)
            if ref is None:
                raise WorkItemError(f"trello card {external_id!r} not found")
            return ref
        card = self._json(self._request("PUT", f"/cards/{external_id}", json=body))
        return self._ref(card)

    def assign_work_item(self, external_id: str, assignee: str | None) -> WorkItemRef:
        body = {"idMembers": [self._member_id(assignee)] if assignee else []}
        card = self._json(self._request("PUT", f"/cards/{external_id}", json=body))
        return self._ref(card)

    def transition_work_item(self, external_id: str, status: str) -> WorkItemRef:
        if status not in NEUTRAL_STATUSES:
            raise WorkItemError(f"unknown neutral status {status!r}")
        target = self._list_for(status)
        if target is not None:
            # Moving onto a workflow list un-archives - a card parked as
            # closed can be reopened through the same neutral vocabulary.
            body: dict[str, Any] = {"idList": target["id"], "closed": False}
        elif status == "closed":
            body = {"closed": True}  # archival IS Trello's native closed
        else:
            raise WorkItemRejected(
                f"trello board has no list for {status!r} - an undeclared capability gap"
            )
        card = self._json(self._request("PUT", f"/cards/{external_id}", json=body))
        return self._ref(card)

    def comment_work_item(self, external_id: str, body: str, *, author: str = "helix") -> None:
        self._request("POST", f"/cards/{external_id}/actions/comments", json={"text": body})

    def link_work_item(
        self, external_id: str, other_external_id: str, *, link_type: str = "relates"
    ) -> None:
        if link_type not in NEUTRAL_LINK_TYPES:
            raise WorkItemError(f"unknown neutral link type {link_type!r}")
        other = self.get_work_item(other_external_id)
        if other is None or not other.external_url:
            raise WorkItemError(f"cannot link: trello card {other_external_id!r} not found")
        self._request(
            "POST",
            f"/cards/{external_id}/attachments",
            json={"url": other.external_url, "name": link_type},
        )
