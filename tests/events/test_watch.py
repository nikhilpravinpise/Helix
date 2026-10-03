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
