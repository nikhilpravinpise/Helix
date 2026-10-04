"""The live cluster watcher (`helix watch`).

A pod is only reported once its problem has persisted across several
polls. A healthy `rollout restart` of payment-api was sampled 60 times and
its new pod read as running-but-not-Ready in 48 of them: reported
immediately, every deploy - including every fix Helix itself deploys -
would open an incident.
"""

from events.schemas import EngineeringEvent, EventSource, EventType
from events.watch import PersistenceFilter, run_watch


def _pod_failed(pod: str, restart: int) -> EngineeringEvent:
    return EngineeringEvent(
        event_id=f"evt_{pod}_{restart}",
        event_type=EventType.POD_FAILED,
        source=EventSource.KUBERNETES,
        service="payment-api",
        environment="sandbox",
        metadata={"pod": pod, "waiting_reason": "CrashLoopBackOff", "restart_count": restart},
    )


def _unready(pod: str) -> EngineeringEvent:
    return EngineeringEvent(
        event_id=f"evt_{pod}_unready",
        event_type=EventType.SERVICE_UNHEALTHY,
        source=EventSource.KUBERNETES,
        service="payment-api",
        environment="sandbox",
        metadata={"pod": pod},
    )


class TestPersistenceFilter:
    def test_a_brief_not_ready_window_is_never_reported(self):
        """The rollout case: not Ready for two polls, then Ready."""
        f = PersistenceFilter(polls=3)
        assert f.observe([_unready("new")]) == []
        assert f.observe([_unready("new")]) == []
        assert f.observe([]) == []
        assert f.observe([_unready("new")]) == []

    def test_a_persisting_problem_is_reported_once(self):
        f = PersistenceFilter(polls=3)
        f.observe([_unready("stuck")])
        f.observe([_unready("stuck")])
        reported = f.observe([_unready("stuck")])
        assert [e.metadata["pod"] for e in reported] == ["stuck"]
        assert f.observe([_unready("stuck")]) == []
        assert f.observe([_unready("stuck")]) == []

    def test_a_crashloop_counts_as_one_condition_across_restarts(self):
        """Each restart is a new event id; the condition is the same, so
        the restart count must not reset the persistence count."""
        f = PersistenceFilter(polls=3)
        assert f.observe([_pod_failed("p", 1)]) == []
        assert f.observe([_pod_failed("p", 2)]) == []
        reported = f.observe([_pod_failed("p", 3)])
        assert [e.event_id for e in reported] == ["evt_p_3"]

    def test_a_problem_that_clears_and_returns_is_reported_again(self):
        f = PersistenceFilter(polls=2)
        f.observe([_unready("x")])
        assert len(f.observe([_unready("x")])) == 1
        f.observe([])
        f.observe([_unready("x")])
        assert len(f.observe([_unready("x")])) == 1


def test_run_watch_publishes_only_confirmed_problems():
    snapshots = [[_unready("a")], [_unready("a")], [_unready("a")], [_unready("a")]]
    published = []
    cycles = run_watch(
        fetch=lambda: snapshots.pop(0),
        publish=published.append,
        interval=0,
        polls=3,
        max_cycles=4,
        sleep=lambda s: None,
    )
    assert cycles == 4
    assert [e.metadata["pod"] for e in published] == ["a"]


def test_a_failed_fetch_does_not_stop_the_watch():
    """The watcher outlives an unreachable API server - a blip must not
    leave the cluster unwatched until someone notices."""
    calls = iter([RuntimeError("api server unreachable"), [], []])

    def fetch():
        item = next(calls)
        if isinstance(item, Exception):
            raise item
        return item

    assert (
        run_watch(
            fetch=fetch,
            publish=lambda e: None,
            interval=0,
            polls=3,
            max_cycles=3,
            sleep=lambda s: None,
        )
        == 3
    )


def test_list_pods_raw_returns_the_kubectl_shape_the_adapter_reads():
    from events.adapters.kubernetes import pod_events
    from integrations.kubernetes.client import KubernetesEvidenceClient

    crashing = {
        "metadata": {"name": "payment-api-x", "uid": "u-9", "namespace": "payment-api"},
        "status": {
            "phase": "Running",
            "containerStatuses": [
                {
                    "name": "payment-api",
                    "state": {"waiting": {"reason": "CrashLoopBackOff"}},
                    "lastState": {"terminated": {"reason": "Error", "exitCode": 1}},
                    "restartCount": 4,
                }
            ],
        },
    }

    class FakeCore:
        def list_namespaced_pod(self, namespace):
            assert namespace == "payment-api"
            return {"items": [crashing]}

    raw = KubernetesEvidenceClient(core_v1=FakeCore()).list_pods_raw("payment-api")
    events = pod_events(raw)
    assert [e.metadata["waiting_reason"] for e in events] == ["CrashLoopBackOff"]


# --- GitHub pull requests (`helix watch --source github`) -------------------


def _open_pr(number: int, sha: str, author: str = "dana") -> dict:
    """One entry of GET /repos/{repo}/pulls, trimmed to what is read."""
    return {
        "number": number,
        "title": f"change {number}",
        "user": {"login": author},
        "head": {"ref": f"feature/{number}", "sha": sha},
        "base": {"ref": "main"},
    }


class TestPullRequestTracker:
    def test_prs_already_open_at_start_are_not_announced(self):
        """Starting the watcher must not turn every open PR - 17 Helix fix
        PRs on the sandbox at the time - into a review incident at once."""
        from events.github_watch import PullRequestTracker

        tracker = PullRequestTracker()
        assert tracker.observe([_open_pr(1, "a" * 40)]) == []

    def test_existing_prs_can_be_announced_on_request(self):
        from events.github_watch import PullRequestTracker

        tracker = PullRequestTracker(include_existing=True)
        [hook] = tracker.observe([_open_pr(1, "a" * 40)])
        assert hook["action"] == "opened"

    def test_a_new_pr_is_opened_and_a_new_commit_is_synchronize(self):
        from events.github_watch import PullRequestTracker

        tracker = PullRequestTracker()
        tracker.observe([])
        [opened] = tracker.observe([_open_pr(7, "a" * 40)])
        assert opened["action"] == "opened" and opened["number"] == 7
        assert tracker.observe([_open_pr(7, "a" * 40)]) == []
        [pushed] = tracker.observe([_open_pr(7, "b" * 40)])
        assert pushed["action"] == "synchronize"

    def test_payloads_are_real_webhook_shape_for_the_adapter(self):
        """The tracker speaks GitHub's webhook shape, so polled and pushed
        PRs go through the same adapter path."""
        from events.adapters.github import pull_request_events
        from events.github_watch import PullRequestTracker

        tracker = PullRequestTracker()
        tracker.observe([])
        [hook] = tracker.observe([_open_pr(7, "c" * 40, author="dana")])
        assert "head" not in hook  # nested under pull_request, as GitHub sends it
        [event] = pull_request_events(hook)
        assert event.metadata["branch"] == "feature/7"
        assert event.metadata["sha"] == "c" * 40
        assert event.actor == "dana"


def test_list_open_pull_requests_raw_asks_for_open_prs():
    import httpx

    from integrations.github.client import GitHubEvidenceClient

    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=[_open_pr(3, "d" * 40)])

    client = GitHubEvidenceClient(
        "VrajVed/helix-sandbox",
        "t",
        client=httpx.Client(
            base_url="https://api.github.com", transport=httpx.MockTransport(handler)
        ),
    )
    assert client.list_open_pull_requests_raw()[0]["number"] == 3
    assert seen[0].url.path == "/repos/VrajVed/helix-sandbox/pulls"
    assert seen[0].url.params["state"] == "open"
