"""Block Kit payloads for the Slack surface (subphase 2.2).

The outbox drain asks this module for blocks so Slack JSON vocabulary
stays in one place - neither the drain nor the coordinator assembles
vendor payloads. `approval_blocks` is the interactive half of the 1.8
gate: the Approve/Reject action_ids carry the approvals-row id, and
POST /slack/interactions turns a button press back into the same
record_decision call the CLI and /approvals endpoints use.
"""

from typing import Any


def approval_blocks(approval_id: int, text: str) -> list[dict[str, Any]]:
    """One section of context plus an actions row whose buttons decide
    approvals row `approval_id`. `value` duplicates the id so the
    interaction handler can read it from either field; `action_id` is
    what the handler actually matches on."""
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": text},
        },
        {
            "type": "actions",
            "block_id": f"helix_approval:{approval_id}",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Approve"},
                    "style": "primary",
                    "action_id": f"helix_approve:{approval_id}",
                    "value": str(approval_id),
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Reject"},
                    "style": "danger",
                    "action_id": f"helix_reject:{approval_id}",
                    "value": str(approval_id),
                },
            ],
        },
    ]


def review_blocks(review_id: int, text: str) -> list[dict[str, Any]]:
    """The 2.6 review gate's interactive half: same shape as
    `approval_blocks`, with `helix_review_*` action_ids carrying the
    review_requests row id so a button press records the reviewer's
    decision on the durable row."""
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": text},
        },
        {
            "type": "actions",
            "block_id": f"helix_review:{review_id}",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Approve"},
                    "style": "primary",
                    "action_id": f"helix_review_approve:{review_id}",
                    "value": str(review_id),
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Request changes"},
                    "style": "danger",
                    "action_id": f"helix_review_reject:{review_id}",
                    "value": str(review_id),
                },
            ],
        },
    ]


def incident_text(incident_id: str, status: str, cause: str = "") -> str:
    """The one-line threaded update format lifecycle transitions post."""
    line = f"{incident_id}: -> {status}"
    return f"{line} - {cause}" if cause else line
