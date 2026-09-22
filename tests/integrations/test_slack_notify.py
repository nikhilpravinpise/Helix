"""Block Kit payload tests (subphase 2.2).

The action_id format is the contract the interactions endpoint matches
on - pin it here so a payload edit cannot silently sever the
approve/reject loop.
"""

from integrations.slack.notify import approval_blocks, incident_text


class TestApprovalBlocks:
    def test_section_carries_the_text(self):
        blocks = approval_blocks(7, "INC-1: approval required for reapply_manifest")
        assert blocks[0] == {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "INC-1: approval required for reapply_manifest"},
        }

    def test_actions_block_carries_approve_and_reject(self):
        blocks = approval_blocks(7, "ctx")
        actions = blocks[1]
        assert actions["type"] == "actions"
        buttons = {b["action_id"]: b for b in actions["elements"]}
        assert set(buttons) == {"helix_approve:7", "helix_reject:7"}
        approve, reject = buttons["helix_approve:7"], buttons["helix_reject:7"]
        assert approve["type"] == "button" and approve["style"] == "primary"
        assert reject["type"] == "button" and reject["style"] == "danger"
        assert approve["value"] == "7" and reject["value"] == "7"
        assert approve["text"]["text"] == "Approve"
        assert reject["text"]["text"] == "Reject"

    def test_blocks_are_plain_json(self):
        import json

        json.dumps(approval_blocks(3, "ctx"))  # must not raise


class TestIncidentText:
    def test_with_cause(self):
        assert incident_text("INC-1", "escalated", "timeout") == "INC-1: -> escalated - timeout"

    def test_without_cause(self):
        assert incident_text("INC-1", "recovered") == "INC-1: -> recovered"
