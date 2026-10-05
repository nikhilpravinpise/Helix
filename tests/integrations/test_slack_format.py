"""Slack message formatting: what an incident says in the channel.

Before this, every message was `INC-x: -> received - new recovery incident`:
no description of what broke, no links, nobody pinged. The formatter turns
an incident's own evidence into a readable card and mentions the people
the registry names, by Slack id when it has one.
"""

from types import SimpleNamespace

from agent.ops.registry import Person
from integrations.slack.format import (
    approval_card,
    describe_incident,
    incident_card,
    mention,
    review_card,
    update_text,
)

PEOPLE = {
    "VrajVed": Person(name="Vraj", slack="U01VRAJ"),
    "nikhilpravinpise": Person(name="Nikhil"),
}


def _incident(**over):
    base = dict(
        incident_id="INC-1",
        service="payment-api",
        environment="sandbox",
        severity="high",
        kind="recovery",
        owner="VrajVed",
        signals=[],
        event_records=[],
        work_items=[],
    )
    base.update(over)
    return SimpleNamespace(**base)


def _signal(source, signature, raw=None):
    return SimpleNamespace(source=source, signature=signature, raw=raw or {})


def _event(event_type, metadata, actor="system"):
    return SimpleNamespace(
        event_type=event_type, source="github", envelope={"metadata": metadata, "actor": actor}
    )


class TestMention:
    def test_a_person_with_a_slack_id_is_pinged(self):
        assert mention("VrajVed", PEOPLE) == "<@U01VRAJ>"

    def test_a_person_without_one_is_named_not_pinged(self):
        assert mention("nikhilpravinpise", PEOPLE) == "*Nikhil*"

    def test_an_unknown_handle_is_shown_as_is(self):
        assert mention("carol", PEOPLE) == "*carol*"

    def test_no_handle_says_so(self):
        assert mention(None, PEOPLE) == "_unassigned_"


class TestDescribe:
    def test_a_jenkins_failure_names_the_build_and_stage(self):
        view = describe_incident(
            _incident(
                signals=[
                    _signal(
                        "jenkins",
                        {"job": "payment-api", "stage": "Test"},
                        {"build_number": 4, "build_url": "http://jenkins/job/payment-api/4/"},
                    )
                ]
            ),
            repo="VrajVed/helix-sandbox",
            ui_base="http://172.17.0.1:8787",
        )
        assert view.summary == "Jenkins build #4 of `payment-api` failed at stage *Test*"
        assert ("Jenkins build #4", "http://jenkins/job/payment-api/4/") in view.links

    def test_a_crashing_pod_names_the_pod_and_reason(self):
        view = describe_incident(
            _incident(
                signals=[
                    _signal(
                        "kubernetes",
                        {"waiting_reason": "CrashLoopBackOff", "terminated_reason": "Error"},
                        {"pod": "payment-api-7f9", "restart_count": 3},
                    )
                ]
            ),
            repo="r",
            ui_base="u",
        )
        assert "`payment-api-7f9`" in view.summary
        assert "*CrashLoopBackOff*" in view.summary
        assert "Error" in view.summary and "3 restarts" in view.summary

    def test_a_pr_names_the_pr_author_and_links_it(self):
        view = describe_incident(
            _incident(
                kind="change",
                event_records=[
                    _event(
                        "pr.opened",
                        {"pr": 19, "title": "Document payment idempotency", "branch": "feature/x"},
                        actor="VrajVed",
                    )
                ],
            ),
            repo="VrajVed/helix-sandbox",
            ui_base="u",
            people=PEOPLE,
        )
        assert view.summary == "PR #19 *Document payment idempotency* opened by Vraj on `feature/x`"
        assert ("PR #19", "https://github.com/VrajVed/helix-sandbox/pull/19") in view.links

    def test_every_incident_links_its_helix_page_and_any_ticket(self):
        view = describe_incident(
            _incident(
                work_items=[
                    SimpleNamespace(
                        external_key="KAN-118",
                        external_url="https://x.atlassian.net/browse/KAN-118",
                    )
                ]
            ),
            repo="r",
            ui_base="http://172.17.0.1:8787",
        )
        assert ("Helix INC-1", "http://172.17.0.1:8787/ui/incidents/INC-1") in view.links
        assert ("Jira KAN-118", "https://x.atlassian.net/browse/KAN-118") in view.links


def _text(blocks):
    return "\n".join(str(b) for b in blocks)


class TestCards:
    def _view(self, **over):
        return describe_incident(_incident(**over), repo="r", ui_base="http://h")

    def test_the_incident_card_carries_summary_owner_and_links(self):
        blocks = incident_card(self._view(), PEOPLE)
        text = _text(blocks)
        assert "INC-1" in text and "payment-api" in text and "high" in text
        assert "<@U01VRAJ>" in text
        assert "http://h/ui/incidents/INC-1" in text

    def test_an_escalation_pings_the_owner(self):
        text = update_text("escalated", "no safe action left", owner="VrajVed", people=PEOPLE)
        assert "<@U01VRAJ>" in text and "no safe action left" in text

    def test_a_routine_update_does_not_ping(self):
        text = update_text("diagnosing", "begin diagnosis", owner="VrajVed", people=PEOPLE)
        assert "<@" not in text

    def test_the_review_card_pings_the_reviewer_and_keeps_the_buttons(self):
        blocks = review_card(
            self._view(), review_id=37, reviewer="VrajVed", rationale="next reviewer", people=PEOPLE
        )
        text = _text(blocks)
        assert "<@U01VRAJ>" in text and "next reviewer" in text
        assert "helix_review_approve:37" in text and "helix_review_reject:37" in text

    def test_the_approval_card_pings_the_owner_says_why_and_keeps_the_buttons(self):
        blocks = approval_card(
            self._view(),
            approval_id=17,
            action="restart_deployment",
            reason="kubernetes write proposed on evidence of only [build.failed]",
            people=PEOPLE,
        )
        text = _text(blocks)
        assert "<@U01VRAJ>" in text and "`restart_deployment`" in text
        assert "build.failed" in text
        assert "helix_approve:17" in text and "helix_reject:17" in text


class TestOutsideTextIsEscaped:
    """Audit 2026-10-05: a PR title, a pod reason or a transition cause is
    text from outside Helix, and it went into Slack mrkdwn as-is. A PR
    titled `<!channel> fix <https://evil.example|Re-login>` pinged the
    whole channel and planted a fake link inside Helix's own card."""

    HOSTILE = "<!channel> fix <https://evil.example|Re-login to Helix> & co"

    def _pr_view(self):
        return describe_incident(
            _incident(
                kind="change",
                event_records=[
                    _event("pr.opened", {"pr": 7, "title": self.HOSTILE, "branch": "<!here>"})
                ],
            ),
            repo="VrajVed/helix-sandbox",
            ui_base="http://helix",
        )

    def test_a_pr_title_cannot_ping_or_link(self):
        summary = self._pr_view().summary
        assert "<!channel>" not in summary and "<!here>" not in summary
        assert "<https://evil.example" not in summary
        assert "&lt;!channel&gt;" in summary and "&amp; co" in summary

    def test_a_transition_cause_cannot_ping(self):
        line = update_text("escalated", "<!everyone> look", owner="VrajVed", people=PEOPLE)
        assert "<!everyone>" not in line
        assert "<@U01VRAJ>" in line  # Helix's own mention still pings

    def test_cards_carry_the_escaped_summary(self):
        view = self._pr_view()
        blocks = approval_card(
            view, approval_id=1, action="<!channel>", reason="<!here>", people=PEOPLE
        )
        assert "<!channel>" not in str(blocks) and "<!here>" not in str(blocks)
