"""Subphase 1.9: the Verification Agent - per-action desired-state checks
sampled across the stability window, the `verifications` row, the
coordinator's verdict routing, and the flapping-detection semantics.

Unit tier throughout: SQLite state, fixture evidence (plus synthetic
fixture documents built in-test), scripted FakeLLM, recording fake
writes, and a counting fake `sleep` so windows never cost wall-clock.
The live functional leg (port-forward + real browser) is integration
tier in tests/e2e/selenium_check.py.
"""

from dataclasses import dataclass

import pytest
from fakes import JENKINS_SIGNAL, K8S_SIGNAL, FakeLLM, make_deps
from sqlalchemy import select

from agent.agents.coordinator import _decide
from agent.agents.diagnosis import EvidencePlan
from agent.agents.planner import CandidateList, CandidateProposal
from agent.runner import run_incident
from agent.state import Hypothesis
from agent.validation import candidate_fingerprint
from agent.verification import (
    CheckResult,
    CheckStatus,
    Sample,
    VerificationContext,
    build_checks,
    functional,
    pull_request,
    runtime,
    sample_check,
)
from integrations.evidence import (
    EvidenceKind,
    EvidenceNotFoundError,
    EvidenceOrigin,
    EvidenceSource,
    build_evidence,
)
from integrations.fixtures import (
    FixtureElkEvidence,
    FixtureGitHubEvidence,
    FixtureKubernetesEvidence,
)
from integrations.kubernetes.client import normalize_pod
from state import models
from state.intake import intake_signal
from state.machine import IncidentStatus

K8S = {"deployment": "payment-api", "namespace": "payment-api"}


def _make_incident(sessions, signal: dict) -> str:
    with sessions() as session:
        return intake_signal(session, signal).incident_id


def _llm(plans: list[CandidateList], claim: str = "payment-api is wedged") -> FakeLLM:
    return FakeLLM(
        [
            EvidencePlan(requests=[]),
            Hypothesis(claim=claim, evidence_refs=[], confidence=0.8),
            *plans,
        ]
    )


def _plan(action: str, params: dict, rationale: str = "fix it") -> CandidateList:
    return CandidateList(
        candidates=[CandidateProposal(action=action, params=params, rationale=rationale)]
    )


def _rows(sessions, model, incident_id: str):
    with sessions() as session:
        return list(
            session.scalars(
                select(model)
                .join(models.Incident)
                .where(models.Incident.incident_id == incident_id)
                .order_by(model.id)
            )
        )


def _transitions(sessions, incident_id: str) -> list[tuple]:
    rows = _rows(sessions, models.Transition, incident_id)
    return [(r.from_status.value if r.from_status else None, r.to_status.value) for r in rows]


# --- Synthetic fixture documents ---------------------------------------


def _pod(
    name="payment-api-aa-1",
    *,
    app="payment-api",
    ready=True,
    waiting=None,
    restarts=0,
    phase="Running",
    terminating=False,
):
    """A kubectl-pod-shaped dict as FixtureKubernetesEvidence consumes."""
    state = (
        {"waiting": {"reason": waiting}}
        if waiting
        else {"running": {"startedAt": "2026-09-18T00:00:00Z"}}
    )
    return {
        "metadata": {
            "name": name,
            "namespace": "payment-api",
            "labels": {"app": app},
            "ownerReferences": [{"controller": True, "kind": "ReplicaSet", "name": f"{app}-rs"}],
            **({"deletionTimestamp": "2026-09-18T00:01:00Z"} if terminating else {}),
        },
        "spec": {"containers": [{"name": "payment-api", "image": "payment-api:kind"}]},
        "status": {
            "phase": phase,
            "containerStatuses": [
                {
                    "name": "payment-api",
                    "image": "payment-api:kind",
                    "ready": ready,
                    "restartCount": restarts,
                    "state": state,
                    "lastState": {},
                }
            ],
            "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
            "startTime": "2026-09-18T00:00:00Z",
        },
    }


def _deployment(
    name="payment-api", *, replicas=1, ready=1, updated=1, available=1, conditions=None
):
    """A kubectl-deployment-shaped dict."""
    return {
        "metadata": {"name": name, "namespace": "payment-api"},
        "spec": {"replicas": replicas},
        "status": {
            "replicas": replicas,
            "readyReplicas": ready,
            "updatedReplicas": updated,
            "availableReplicas": available,
            "observedGeneration": 2,
            "conditions": conditions
            or [
                {"type": "Available", "status": "True"},
                {
                    "type": "Progressing",
                    "status": "True",
                    "reason": "NewReplicaSetAvailable",
                },
            ],
        },
    }


def _healthy_doc():
    """A synthetic "recovered" cluster fixture: pods Ready, rollout done,
    clean logs."""
    return {
        "fault": "synthetic-recovered",
        "evidence": {
            "pods_payment_api": {"items": [_pod()]},
            "pods_downstream_stub": {
                "items": [_pod("downstream-stub-bb-1", app="downstream-stub")]
            },
            "deployments_payment_api": _deployment(),
            "events": {"items": []},
            "logs_payment_api": [
                '2026-09-18T00:00:01Z [payment-api-aa-1] INFO: "GET /ready" 200 OK'
            ],
        },
    }


def _jenkins_doc(result: str):
    return {
        "fault": "synthetic-ci",
        "jenkins_job": "payment-api",
        "jenkins_build_number": 44,
        "jenkins_result": result,
        "jenkins_build_url": "https://jenkins.example/job/payment-api/44/",
        "evidence": {"console_output": "Started\n[Pipeline] stage (Test)\nFinished"},
    }


def _pods_evidence(namespace: str, service: str, items: list):
    pods = [normalize_pod(p) for p in items]
    return build_evidence(
        source=EvidenceSource.KUBERNETES,
        kind=EvidenceKind.POD_LIST,
        service=service,
        summary=f"{len(pods)} pod(s)",
        payload={"namespace": namespace, "selector": service, "items": pods},
        origin=EvidenceOrigin.FIXTURE,
        total=len(pods),
    )


class _ScriptedPods(FixtureKubernetesEvidence):
    """Fixture k8s evidence whose `label_selector` get_pods calls (the
    verification legs' call shape) walk a queue of pod-item lists - the
    last list repeats. `service=` calls (diagnosis pre-selection) still
    read the fixture doc, so a scripted flap is only consumed by the
    verify pass."""

    def __init__(self, doc, pods_lists, deployment=None):
        super().__init__(doc)
        self._lists = list(pods_lists)
        self._deployment = deployment
        self.calls = 0

    def get_pods(self, namespace, *, service=None, label_selector=None):
        if label_selector is None:
            return super().get_pods(namespace, service=service)
        items = self._lists[min(self.calls, len(self._lists) - 1)]
        self.calls += 1
        target = label_selector.removeprefix("app=") if label_selector else "payment-api"
        return _pods_evidence(namespace, target, items)

    def get_deployment_status(self, namespace, name):
        if self._deployment is not None:
            return build_evidence(
                source=EvidenceSource.KUBERNETES,
                kind=EvidenceKind.POD_STATUS,
                service=name,
                summary=f"deployment {name}",
                payload=self._deployment,
                origin=EvidenceOrigin.FIXTURE,
            )
        return super().get_deployment_status(namespace, name)


class _ScriptedRollout(FixtureKubernetesEvidence):
    """Fixture k8s evidence whose get_deployment_status walks a queue of
    deployment payloads - the last one repeats. Lets the settle phase
    observe a rollout mid-convergence; pod/log reads still come from the
    fixture doc."""

    def __init__(self, doc, deployments):
        super().__init__(doc)
        self._deployments = list(deployments)
        self.calls = 0

    def get_deployment_status(self, namespace, name):
        from integrations.kubernetes.client import normalize_deployment

        payload = normalize_deployment(
            self._deployments[min(self.calls, len(self._deployments) - 1)]
        )
        self.calls += 1
        return build_evidence(
            source=EvidenceSource.KUBERNETES,
            kind=EvidenceKind.POD_STATUS,
            service=name,
            summary=f"deployment {name}",
            payload=payload,
            origin=EvidenceOrigin.FIXTURE,
        )


class _NoJenkins:
    def get_job(self, job):
        raise EvidenceNotFoundError("no jenkins in this doc")

    def get_build_status(self, job, build_number):
        raise EvidenceNotFoundError("no jenkins in this doc")


def _ctx(doc, **overrides):
    """A VerificationContext over fixture impls for one synthetic doc;
    keyword overrides swap any field (e.g. origin="live" to exercise the
    non-fixture path of the functional leg)."""
    from dataclasses import replace

    ctx = VerificationContext(
        kubernetes=FixtureKubernetesEvidence(doc),
        elk=FixtureElkEvidence(doc),
        jenkins=_NoJenkins(),
        github=FixtureGitHubEvidence(doc),
        service="payment-api",
        namespace="payment-api",
        origin="fixture",
        functional_enabled=True,
        elk_error_threshold=0,
    )
    return replace(ctx, **overrides)


# --- The sampling loop ---------------------------------------------------


@dataclass
class _Script:
    """A SampledCheck replaying a fixed list of Sample statuses."""

    name: str
    samples: list[Sample]
    calls: int = 0

    def check(self) -> Sample:
        sample = self.samples[min(self.calls, len(self.samples) - 1)]
        self.calls += 1
        return sample


def _ok(detail="ok"):
    return Sample(CheckStatus.PASS, detail)


def _bad(detail="bad"):
    return Sample(CheckStatus.FAIL, detail)


class TestSampleCheck:
    """The stability-window semantics: pass-hold, grace, flapping, error."""

    def test_held_across_window_passes(self):
        check = _Script("c", [_ok(), _ok(), _ok()])
        result = sample_check(check, 4, 2, lambda _s: None)
        assert result.status == CheckStatus.PASS
        assert check.calls == 3  # t=0, t=2, t=4

    def test_zero_window_is_a_single_sample(self):
        check = _Script("c", [_ok()])
        assert sample_check(check, 0, 2, lambda _s: None).status == CheckStatus.PASS
        assert check.calls == 1

    def test_grace_before_first_healthy_then_hold_passes(self):
        # Not ready at t=0 is the action's effect still landing; ready at
        # t=2 and t=4 holds.
        check = _Script("c", [_bad("not yet"), _ok(), _ok()])
        result = sample_check(check, 4, 2, lambda _s: None)
        assert result.status == CheckStatus.PASS

    def test_flapping_after_healthy_fails(self):
        # Ready at t=0, broken at t=2 - a restart-then-crash is NOT
        # recovered even if it never gets sampled again.
        check = _Script("c", [_ok(), _bad("CrashLoopBackOff")])
        result = sample_check(check, 4, 2, lambda _s: None)
        assert result.status == CheckStatus.FAIL
        assert "flapping" in result.detail

    def test_never_healthy_fails(self):
        check = _Script("c", [_bad("pending")])
        result = sample_check(check, 4, 2, lambda _s: None)
        assert result.status == CheckStatus.FAIL
        assert "not observed" in result.detail

    def test_all_error_samples_report_error_not_fail(self):
        check = _Script("c", [Sample(CheckStatus.ERROR, "api down")])
        result = sample_check(check, 4, 2, lambda _s: None)
        assert result.status == CheckStatus.ERROR
        assert "never ran" in result.detail

    def test_error_after_healthy_means_unconfirmed_stability(self):
        check = _Script("c", [_ok(), Sample(CheckStatus.ERROR, "api down")])
        result = sample_check(check, 4, 2, lambda _s: None)
        assert result.status == CheckStatus.ERROR

    def test_skipped_check_short_circuits(self):
        check = _Script("c", [Sample(CheckStatus.SKIPPED, "not in fixture")])
        result = sample_check(check, 4, 2, lambda _s: None)
        assert result.status == CheckStatus.SKIPPED
        assert check.calls == 1

    def test_poll_larger_than_window_still_takes_two_samples(self):
        check = _Script("c", [_ok()])
        sample_check(check, 2, 20, lambda _s: None)
        assert check.calls == 2  # t=0 and the window-end sample


class TestPodHealthCheck:
    def test_ready_pods_pass(self):
        check = runtime.PodHealthCheck(
            FixtureKubernetesEvidence(_healthy_doc()), "payment-api", "payment-api"
        )
        sample = check.check()
        assert sample.ok and sample.evidence_id

    def test_bad_waiting_reason_fails(self):
        doc = _healthy_doc()
        doc["evidence"]["pods_payment_api"] = {
            "items": [_pod(waiting="CrashLoopBackOff", ready=False)]
        }
        check = runtime.PodHealthCheck(FixtureKubernetesEvidence(doc), "payment-api", "payment-api")
        sample = check.check()
        assert not sample.ok and "CrashLoopBackOff" in sample.detail

    def test_restart_count_rise_is_a_failed_sample(self):
        """The pod is Running and ready at both samples, but its restart
        count rose - crash-looping caught even mid-'Running'."""
        kube = _ScriptedPods(
            _healthy_doc(),
            [
                [_pod(restarts=1)],
                [_pod(restarts=2)],
            ],
        )
        check = runtime.PodHealthCheck(kube, "payment-api", "payment-api")
        assert check.check().ok  # first sample seeds the counters
        second = check.check()
        assert not second.ok and "restart_count rose" in second.detail

    def test_ready_pod_replaced_by_ready_pod_is_not_a_flap(self):
        """A rollout swaps pod names: the old Ready pod disappears in the
        same sample a new pod is already Ready. That is intended
        replacement, not flapping - the deployment never stopped
        serving."""
        kube = _ScriptedPods(
            _healthy_doc(),
            [
                [_pod("payment-api-aa-1")],
                [_pod("payment-api-bb-2")],  # the Ready pod was replaced
            ],
        )
        check = runtime.PodHealthCheck(kube, "payment-api", "payment-api")
        assert check.check().ok
        assert check.check().ok

    def test_ready_pod_disappearing_without_replacement_fails(self):
        """Ready pod gone and nothing Ready this sample: real loss of
        serving capacity - a failed observation, so a held-then-lost
        sequence still reads as flapping."""
        kube = _ScriptedPods(
            _healthy_doc(),
            [
                [_pod("payment-api-aa-1")],
                [_pod("payment-api-bb-2", ready=False)],  # replacement not Ready
            ],
        )
        check = runtime.PodHealthCheck(kube, "payment-api", "payment-api")
        assert check.check().ok
        second = check.check()
        assert not second.ok and "now absent" in second.detail

    def test_terminating_wedged_pod_does_not_count(self):
        """Post-rollback, the replaced RS's pod lingers in teardown with a
        bad waiting state while the converged RS's pod is Ready. A pod
        Kubernetes is deleting is not the desired state - it must not fail
        the sample."""
        kube = _ScriptedPods(
            _healthy_doc(),
            [
                [
                    _pod("payment-api-bb-2"),  # new RS pod, Ready
                    _pod(
                        "payment-api-aa-1",
                        ready=False,
                        waiting="CrashLoopBackOff",
                        terminating=True,
                    ),  # old RS pod mid-teardown
                ],
            ],
        )
        check = runtime.PodHealthCheck(kube, "payment-api", "payment-api")
        assert check.check().ok

    def test_terminating_is_only_discount_when_some_pod_serves(self):
        """If the ONLY pod is terminating-and-wedged, nothing is serving -
        still a failed sample (the empty-after-filter list path)."""
        kube = _ScriptedPods(
            _healthy_doc(),
            [[_pod(ready=False, waiting="CrashLoopBackOff", terminating=True)]],
        )
        check = runtime.PodHealthCheck(kube, "payment-api", "payment-api")
        assert not check.check().ok

    def test_empty_pod_list_fails(self):
        kube = _ScriptedPods(_healthy_doc(), [[]])
        check = runtime.PodHealthCheck(kube, "payment-api", "payment-api")
        assert not check.check().ok

    def test_missing_fixture_evidence_skips(self):
        check = runtime.PodHealthCheck(
            FixtureKubernetesEvidence({"fault": "empty", "evidence": {}}),
            "payment-api",
            "payment-api",
            fixture=True,
        )
        assert check.check().status == CheckStatus.SKIPPED


class TestRolloutCheck:
    def test_complete_rollout_passes(self):
        check = runtime.RolloutCheck(
            FixtureKubernetesEvidence(_healthy_doc()), "payment-api", "payment-api"
        )
        assert check.check().ok

    def test_incomplete_rollout_fails(self):
        doc = _healthy_doc()
        doc["evidence"]["deployments_payment_api"] = _deployment(ready=0, available=0)
        check = runtime.RolloutCheck(FixtureKubernetesEvidence(doc), "payment-api", "payment-api")
        sample = check.check()
        assert not sample.ok and "incomplete" in sample.detail

    def test_deadline_exceeded_fails(self):
        doc = _healthy_doc()
        doc["evidence"]["deployments_payment_api"] = _deployment(
            conditions=[
                {
                    "type": "Progressing",
                    "status": "False",
                    "reason": "ProgressDeadlineExceeded",
                    "message": "timed out",
                }
            ]
        )
        check = runtime.RolloutCheck(FixtureKubernetesEvidence(doc), "payment-api", "payment-api")
        assert not check.check().ok

    def test_missing_fixture_evidence_skips(self):
        check = runtime.RolloutCheck(
            FixtureKubernetesEvidence({"fault": "empty", "evidence": {}}),
            "payment-api",
            "payment-api",
            fixture=True,
        )
        assert check.check().status == CheckStatus.SKIPPED


class TestElkErrorCheck:
    def test_clean_logs_pass(self):
        check = runtime.ElkErrorCheck(FixtureElkEvidence(_healthy_doc()), "payment-api", 0)
        assert check.check().ok

    def test_error_lines_over_threshold_fail(self):
        doc = _healthy_doc()
        doc["evidence"]["logs_payment_api"] = ["2026-09-18T00:00:02Z [pod] ERROR:payment_api: boom"]
        check = runtime.ElkErrorCheck(FixtureElkEvidence(doc), "payment-api", 0)
        sample = check.check()
        assert not sample.ok and "threshold 0" in sample.detail

    def test_threshold_allows_bounded_errors(self):
        doc = _healthy_doc()
        doc["evidence"]["logs_payment_api"] = ["2026-09-18T00:00:02Z [pod] ERROR:payment_api: boom"]
        check = runtime.ElkErrorCheck(FixtureElkEvidence(doc), "payment-api", 1)
        assert check.check().ok


class TestBuildGreenCheck:
    def test_latest_build_success_passes(self):
        from agent.verification.pipeline import BuildGreenCheck
        from integrations.fixtures import FixtureJenkinsEvidence

        check = BuildGreenCheck(FixtureJenkinsEvidence(_jenkins_doc("SUCCESS")), "payment-api")
        assert check.check().ok

    def test_latest_build_failure_fails(self):
        from agent.verification.pipeline import BuildGreenCheck
        from integrations.fixtures import FixtureJenkinsEvidence

        check = BuildGreenCheck(FixtureJenkinsEvidence(_jenkins_doc("FAILURE")), "payment-api")
        sample = check.check()
        assert not sample.ok and "FAILURE" in sample.detail

    def test_still_running_is_not_recovered(self):
        from agent.verification.pipeline import BuildGreenCheck
        from integrations.fixtures import FixtureJenkinsEvidence

        check = BuildGreenCheck(FixtureJenkinsEvidence(_jenkins_doc("BUILDING")), "payment-api")
        assert not check.check().ok


class TestFixPrCheck:
    """open_fix_pr verifies the recorded/opened PR - the PR IS the fix."""

    PARAMS = {
        "branch": "helix/INC-1",
        "title": "fix",
        "files": [{"path": "sandbox/payment-api/k8s/03-secret.yaml", "content": "apiVersion: v1"}],
    }

    def test_recorded_fake_write_passes(self):
        ctx = _ctx(_healthy_doc())
        output = {
            "op": "open_fix_pr",
            "recorded": True,
            "branch": "helix/INC-1",
            "files": [{"path": "sandbox/payment-api/k8s/03-secret.yaml", "bytes": 42}],
            "pr_number": None,
            "pr_url": None,
        }
        result = pull_request.run(ctx, self.PARAMS, output)
        assert result.status == CheckStatus.PASS

    def test_missing_intended_file_fails(self):
        ctx = _ctx(_healthy_doc())
        output = {
            "op": "open_fix_pr",
            "recorded": True,
            "branch": "helix/INC-1",
            "files": [{"path": "sandbox/payment-api/other.yaml", "bytes": 1}],
        }
        result = pull_request.run(ctx, self.PARAMS, output)
        assert result.status == CheckStatus.FAIL
        assert "did not cover" in result.detail

    def test_no_pr_coordinates_fails(self):
        ctx = _ctx(_healthy_doc())
        result = pull_request.run(ctx, self.PARAMS, {})
        assert result.status == CheckStatus.FAIL
        assert "no PR coordinates" in result.detail

    def test_live_pr_readback_passes(self):
        ctx = _ctx(_healthy_doc(), github=_StubGitHubPR(state="open", head_ref="helix/INC-1"))
        output = {
            "op": "open_fix_pr",
            "branch": "helix/INC-1",
            "files": ["sandbox/payment-api/k8s/03-secret.yaml"],
            "pr_number": 42,
        }
        result = pull_request.run(ctx, self.PARAMS, output)
        assert result.status == CheckStatus.PASS

    def test_live_pr_wrong_branch_fails(self):
        ctx = _ctx(_healthy_doc(), github=_StubGitHubPR(state="open", head_ref="helix/other"))
        output = {
            "op": "open_fix_pr",
            "branch": "helix/INC-1",
            "files": ["sandbox/payment-api/k8s/03-secret.yaml"],
            "pr_number": 42,
        }
        assert pull_request.run(ctx, self.PARAMS, output).status == CheckStatus.FAIL


class _StubGitHubPR:
    """GitHubEvidence-shaped stub returning one pr_info payload."""

    def __init__(self, *, state, head_ref):
        self._payload = {
            "number": 42,
            "state": state,
            "title": "fix",
            "head_ref": head_ref,
            "base_ref": "main",
            "merged": False,
        }

    def get_pull_request(self, number):
        return build_evidence(
            source=EvidenceSource.GITHUB,
            kind=EvidenceKind.PR_INFO,
            service="x",
            summary=f"PR #{number}",
            payload=self._payload,
            origin=EvidenceOrigin.LIVE,
        )


class TestFunctionalLeg:
    def test_fixture_mode_records_skipped(self):
        ctx = _ctx(_healthy_doc())
        result = functional.run(ctx)
        assert result.status == CheckStatus.SKIPPED
        assert "fixture" in result.detail

    def test_disabled_records_skipped(self):
        ctx = _ctx(_healthy_doc(), origin="live", functional_enabled=False)
        result = functional.run(ctx)
        assert result.status == CheckStatus.SKIPPED
        assert "HELIX_VERIFY_FUNCTIONAL" in result.detail

    def test_live_probe_result_is_reported(self):
        ctx = _ctx(
            _healthy_doc(),
            origin="live",
            functional_probe=lambda _c: CheckResult("functional", CheckStatus.PASS, "probe ok"),
        )
        assert functional.run(ctx).status == CheckStatus.PASS

    def test_live_probe_failure_is_fail_not_skip(self):
        ctx = _ctx(
            _healthy_doc(),
            origin="live",
            functional_probe=lambda _c: CheckResult(
                "functional", CheckStatus.FAIL, "connection refused"
            ),
        )
        result = functional.run(ctx)
        assert result.status == CheckStatus.FAIL

    def test_base_url_mode_probes_ingress_and_never_forwards(self, monkeypatch):
        """HELIX_VERIFY_BASE_URL set (1.13): the leg probes the ingress
        surface directly - no kubectl port-forward is opened."""
        import agent.verification.functional as fn

        seen: list[str] = []

        def fake_docs(base_url):
            seen.append(base_url)
            return True, "swagger-ui rendered"

        def fake_pay(base_url):
            seen.append(base_url)
            return True, "POST /payments -> 201 status='settled'"

        def fail_forward(*a, **kw):
            raise AssertionError("port_forward must not run in base-url mode")

        monkeypatch.setattr(fn, "check_swagger_docs", fake_docs)
        monkeypatch.setattr(fn, "check_payments", fake_pay)
        monkeypatch.setattr(fn, "port_forward", fail_forward)

        ctx = _ctx(_healthy_doc(), origin="live", functional_base_url="http://payment-api.local")
        result = functional.run(ctx)
        assert result.status == CheckStatus.PASS
        assert seen == ["http://payment-api.local", "http://payment-api.local"]

    def test_base_url_mode_probe_failure_is_fail(self, monkeypatch):
        import agent.verification.functional as fn

        monkeypatch.setattr(fn, "check_swagger_docs", lambda _u: (True, "rendered"))
        monkeypatch.setattr(
            fn, "check_payments", lambda _u: (False, "POST /payments -> 500 status=None")
        )

        ctx = _ctx(_healthy_doc(), origin="live", functional_base_url="http://payment-api.local")
        assert functional.run(ctx).status == CheckStatus.FAIL


class TestPortForward:
    """The leak-safe port-forward lifecycle over a fake process - no
    cluster needed; the real one is integration tier."""

    class _FakeProc:
        """Emits the kubectl handshake line, then idles until killed."""

        def __init__(self, *a, **kw):
            import io

            self.stdout = io.StringIO("Forwarding from 127.0.0.1:38111 -> 8000\n")
            self.returncode = None
            self.terminated = False
            self.killed = False

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = 0

        def kill(self):
            self.killed = True
            self.returncode = -9

        def wait(self, timeout=None):
            return self.returncode

    def test_forward_yields_url_and_always_terminates(self, monkeypatch):
        created = []

        def fake_popen(cmd, **kw):
            assert "svc/payment-api" in cmd
            proc = TestPortForward._FakeProc()
            created.append(proc)
            return proc

        import agent.verification.functional as fn

        monkeypatch.setattr(fn, "_wait_for_tcp", lambda *a, **kw: None)
        with pytest.raises(RuntimeError, match="boom"):
            with fn.port_forward("payment-api", "payment-api", 8000, popen=fake_popen) as url:
                assert url == "http://127.0.0.1:38111"
                raise RuntimeError("boom")
        # Exception inside the with-block still terminated the process.
        assert created[0].terminated or created[0].killed

    def test_forward_kills_when_terminate_stalls(self, monkeypatch):
        class StubbornProc(TestPortForward._FakeProc):
            def terminate(self):
                self.terminated = True  # marked but never exits
                self.returncode = None

            def wait(self, timeout=None):
                if self.returncode is None:
                    import subprocess

                    raise subprocess.TimeoutExpired("kubectl", timeout)
                return self.returncode

        created = []

        def fake_popen(cmd, **kw):
            proc = StubbornProc()
            created.append(proc)
            return proc

        import agent.verification.functional as fn

        monkeypatch.setattr(fn, "_wait_for_tcp", lambda *a, **kw: None)
        with fn.port_forward("payment-api", "payment-api", 8000, popen=fake_popen):
            pass
        assert created[0].terminated and created[0].killed


class TestDispatch:
    """Which legs run per action - the catalogue's promise is the table."""

    def test_k8s_actions_get_workload_elk_functional(self):
        sampled, oneshots = build_checks(
            _ctx(_healthy_doc()), "restart_deployment", K8S, {}, poll_s=2
        )
        assert [c.name for c in sampled] == [
            "pods_ready",
            "rollout_complete",
            "elk_errors",
        ]
        assert len(oneshots) == 1  # the functional leg

    def test_rerun_pipeline_gets_build_leg_only(self):
        sampled, oneshots = build_checks(
            _ctx(_jenkins_doc("SUCCESS")), "rerun_pipeline", {"job": "payment-api"}, {}, poll_s=2
        )
        assert [c.name for c in sampled] == ["build_green"]
        assert oneshots == []

    def test_open_fix_pr_gets_pr_leg_only(self):
        sampled, oneshots = build_checks(
            _ctx(_healthy_doc()), "open_fix_pr", TestFixPrCheck.PARAMS, {}, poll_s=2
        )
        assert sampled == []
        assert len(oneshots) == 1

    def test_open_fix_pr_on_a_runtime_incident_also_checks_the_pods(self):
        """Live, 2026-10-03: `helix watch` caught an injected crashloop, arm
        E opened a fix PR, the PR-exists check passed, and the incident
        closed as recovered while the pod was still in CrashLoopBackOff. A
        PR changes nothing until it is merged and deployed, so when the
        incident is about the running system, recovered must mean the
        workload is healthy whatever the remediation."""
        sampled, oneshots = build_checks(
            _ctx(_healthy_doc(), runtime_incident=True),
            "open_fix_pr",
            TestFixPrCheck.PARAMS,
            {},
            poll_s=2,
        )
        assert [c.name for c in sampled] == ["pods_ready", "rollout_complete"]
        assert len(oneshots) == 1  # the PR leg still runs

    def test_rerun_pipeline_on_a_runtime_incident_also_checks_the_pods(self):
        sampled, _ = build_checks(
            _ctx(_jenkins_doc("SUCCESS"), runtime_incident=True),
            "rerun_pipeline",
            {"job": "payment-api"},
            {},
            poll_s=2,
        )
        assert [c.name for c in sampled] == ["build_green", "pods_ready", "rollout_complete"]

    def test_unknown_action_gets_an_unrunnable_check(self):
        sampled, oneshots = build_checks(_ctx(_healthy_doc()), "nuke_the_cluster", {}, {}, poll_s=2)
        assert sampled == []
        result = oneshots[0]()
        assert result.status == CheckStatus.ERROR

    def test_reapply_manifest_secret_target(self):
        sampled, _ = build_checks(
            _ctx(_healthy_doc()),
            "reapply_manifest",
            {"path": "sandbox/payment-api/k8s/03-secret.yaml"},
            {},
            poll_s=2,
        )
        names = [c.name for c in sampled]
        assert "secret_present" in names
        assert "pods_ready" in names


class TestDeploymentSettle:
    """The rollout-settle phase run_verification performs before the
    stability window for deployment-mutating actions (1.9 fix): intended
    pod turnover is absorbed during settle, so the window measures steady
    state instead of reading the transition as flapping."""

    def _run(self, ctx, action="restart_deployment", params=None, settle_s=10):
        from agent.verification import run_verification

        return run_verification(
            ctx,
            action,
            params if params is not None else dict(K8S),
            {"op": action},
            window_s=4,
            poll_s=2,
            sleep=lambda _s: None,
            settle_s=settle_s,
        )

    def test_settle_waits_for_convergence_then_window_passes(self):
        """Rollout incomplete for the first two settle samples, converged
        by the third: settle absorbs the transition, then the stability
        window opens on steady state and the verification passes."""
        deployments = [
            _deployment(ready=0, updated=1, available=0),
            _deployment(ready=0, updated=1, available=0),
            _deployment(),
        ]
        ctx = _ctx(_healthy_doc(), kubernetes=_ScriptedRollout(_healthy_doc(), deployments))
        report = self._run(ctx)
        assert report.outcome == "pass"
        assert report.detail.startswith("rollout settle: converged after ~4s;")

    def test_settle_never_converged_still_runs_the_window(self):
        """Settle is a wait, not a verdict: a rollout that never converges
        inside the budget still gets the full window, where
        rollout_complete fails the verification on its own."""
        deployments = [_deployment(ready=0, updated=1, available=0)]
        ctx = _ctx(_healthy_doc(), kubernetes=_ScriptedRollout(_healthy_doc(), deployments))
        report = self._run(ctx)
        assert report.outcome == "fail"
        assert "rollout settle: never converged" in report.detail
        checks = {c.name: c.status for c in report.checks}
        assert checks["rollout_complete"] == CheckStatus.FAIL

    def test_settle_converged_immediately_reports_zero_wait(self):
        ctx = _ctx(_healthy_doc())
        report = self._run(ctx)
        assert report.outcome == "pass"
        assert report.detail.startswith("rollout settle: converged after ~0s;")

    def test_no_settle_for_secret_reapply(self):
        """reapply_manifest on a Secret does not roll a Deployment -
        deployment_target returns None and no settle phase runs."""
        report = self._run(
            _ctx(_healthy_doc()),
            action="reapply_manifest",
            params={"path": "sandbox/payment-api/k8s/03-secret.yaml"},
        )
        assert "rollout settle" not in report.detail

    def test_no_settle_for_pr_action(self):
        params = {
            "branch": "helix/INC-1",
            "title": "fix",
            "files": [{"path": "sandbox/payment-api/k8s/03-secret.yaml", "content": "x"}],
        }
        report = self._run(_ctx(_healthy_doc()), action="open_fix_pr", params=params)
        assert "rollout settle" not in report.detail

    def test_deployment_target_resolution(self):
        ctx = _ctx(_healthy_doc())
        assert runtime.deployment_target(ctx, "restart_deployment", dict(K8S)) == (
            "payment-api",
            "payment-api",
        )
        assert runtime.deployment_target(
            ctx,
            "reapply_manifest",
            {"path": "sandbox/payment-api/k8s/04-deployment.yaml"},
        ) == ("payment-api", "payment-api")
        # Secret reapply rolls nothing.
        assert (
            runtime.deployment_target(
                ctx, "reapply_manifest", {"path": "sandbox/payment-api/k8s/03-secret.yaml"}
            )
            is None
        )
        assert runtime.deployment_target(ctx, "rerun_pipeline", {"job": "x"}) is None


# --- run_verification + the verify node ---------------------------------


def _verify_deps(sessions, llm, fixture, store, **kwargs):
    return make_deps(sessions, llm, fixture, store, **kwargs)


class TestVerificationE2E:
    """Full runs through intake -> ... -> verify -> recovered/escalated."""

    def test_recovered_path_fixture(self, sqlite_sessions, store):
        """execute -> verify pass -> RECOVERED, with a complete
        verifications row (per-check states + window timestamps)."""
        plan = _plan("restart_deployment", dict(K8S), "bounce the wedged pod")
        deps, caps = _verify_deps(sqlite_sessions, _llm([plan]), _healthy_doc(), store)
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)

        result = run_incident(incident_id, deps=deps)

        assert result.error is None
        assert result.status == IncidentStatus.RECOVERED
        assert caps["kube_write"].methods() == ["rollout_restart"]
        verifications = _rows(sqlite_sessions, models.Verification, incident_id)
        assert len(verifications) == 1
        row = verifications[0]
        assert row.outcome == models.VerificationOutcome.PASS
        assert row.action == "restart_deployment"
        assert row.execution_id is not None
        assert row.needs_compensation is False
        assert row.started_at is not None and row.finished_at is not None
        assert row.window_seconds == 4.0 and row.poll_seconds == 2.0
        checks = {c["name"]: c["status"] for c in row.checks}
        assert checks["pods_ready"] == "pass"
        assert checks["rollout_complete"] == "pass"
        assert checks["elk_errors"] == "pass"
        # Fixture mode: the functional leg records skipped, never a pass.
        assert checks["functional"] == "skipped"
        assert _transitions(sqlite_sessions, incident_id)[-1] == ("verifying", "recovered")

    def test_flapping_pod_fails_verification(self, sqlite_sessions, store):
        """Ready at the first sample, crashlooping later in the window -
        explicit flap detection, the incident is NOT recovered."""
        doc = _healthy_doc()
        plan = _plan("restart_deployment", dict(K8S))
        deps, _ = _verify_deps(sqlite_sessions, _llm([plan]), doc, store)
        from dataclasses import replace

        # A k8s capability that returns the healthy pod, then a
        # crashlooping one, then keeps returning the crashlooping one.
        scripted = _ScriptedPods(
            doc,
            [
                [_pod()],
                [_pod(waiting="CrashLoopBackOff", ready=False)],
            ],
        )
        deps = replace(deps, kubernetes=scripted)
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)

        result = run_incident(incident_id, deps=deps)

        # Verification failed -> replan -> the scripted LLM is exhausted
        # -> empty plan -> escalate (retryable actions replan first).
        assert result.status == IncidentStatus.ESCALATED
        verifications = _rows(sqlite_sessions, models.Verification, incident_id)
        assert len(verifications) == 1
        row = verifications[0]
        assert row.outcome == models.VerificationOutcome.FAIL
        pods_check = next(c for c in row.checks if c["name"] == "pods_ready")
        assert pods_check["status"] == "fail"
        assert "flapping" in pods_check["detail"]
        # A failed verification's fingerprint is excluded from replans.
        transitions = _transitions(sqlite_sessions, incident_id)
        assert ("verifying", "remediation_pending") in transitions
        assert transitions[-1] == ("remediation_pending", "escalated")

    def test_ready_pod_with_continuing_errors_fails(self, sqlite_sessions, store):
        """Pods Ready but ELK still logging errors: the error-rate leg
        fails the verification - pod health alone is not recovery."""
        doc = _healthy_doc()
        doc["evidence"]["logs_payment_api"] = [
            "2026-09-18T00:00:05Z [payment-api-aa-1] ERROR:payment_api: 500 on /payments"
        ]
        plan = _plan("restart_deployment", dict(K8S))
        deps, _ = _verify_deps(sqlite_sessions, _llm([plan]), doc, store)
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)

        result = run_incident(incident_id, deps=deps)

        assert result.status == IncidentStatus.ESCALATED
        row = _rows(sqlite_sessions, models.Verification, incident_id)[0]
        checks = {c["name"]: c["status"] for c in row.checks}
        assert checks["pods_ready"] == "pass"  # pods really were fine
        assert checks["elk_errors"] == "fail"  # ...but errors continued
        assert row.outcome == models.VerificationOutcome.FAIL

    def test_open_fix_pr_verifies_recorded_write(self, sqlite_sessions, store):
        """The recorded fake PR is the remediation - it verifies, and the
        incident recovers."""
        params = {
            "branch": "helix/INC-1",
            "title": "helix: restore payment-api deployment",
            "files": [
                {
                    "path": "sandbox/payment-api/k8s/04-deployment.yaml",
                    "content": (
                        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: payment-api\n"
                    ),
                }
            ],
        }
        plan = _plan("open_fix_pr", params, "persist the fix as a PR")
        deps, caps = _verify_deps(sqlite_sessions, _llm([plan]), _healthy_doc(), store)
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)

        result = run_incident(incident_id, deps=deps)

        assert result.status == IncidentStatus.RECOVERED
        assert caps["github_write"].methods() == ["open_fix_pr"]
        row = _rows(sqlite_sessions, models.Verification, incident_id)[0]
        assert row.outcome == models.VerificationOutcome.PASS
        checks = {c["name"]: c["status"] for c in row.checks}
        # A Kubernetes-signal incident is a runtime incident, so the workload
        # legs run beside the PR check (requirement changed 2026-10-03, with
        # Vraj's confirmation, after a live false "recovered").
        assert checks == {"fix_pr_exists": "pass", "pods_ready": "pass", "rollout_complete": "pass"}

    def test_a_fix_pr_does_not_recover_a_pod_that_is_still_crashing(self, sqlite_sessions, store):
        """The live failure, reproduced: a crashloop incident, a fix PR that
        exists, and the pod still in CrashLoopBackOff. It used to verify as
        recovered on the PR alone; now it must not."""
        params = {
            "branch": "helix/INC-1",
            "title": "helix: restore payment-api deployment",
            "files": [
                {
                    "path": "sandbox/payment-api/k8s/04-deployment.yaml",
                    "content": (
                        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: payment-api\n"
                    ),
                }
            ],
        }
        doc = _healthy_doc()
        doc["evidence"]["pods_payment_api"] = {
            "items": [_pod(waiting="CrashLoopBackOff", ready=False)]
        }
        plan = _plan("open_fix_pr", params, "persist the fix as a PR")
        deps, _ = _verify_deps(sqlite_sessions, _llm([plan]), doc, store)
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)

        result = run_incident(incident_id, deps=deps)

        assert result.status != IncidentStatus.RECOVERED
        row = _rows(sqlite_sessions, models.Verification, incident_id)[0]
        assert row.outcome == models.VerificationOutcome.FAIL
        checks = {c["name"]: c["status"] for c in row.checks}
        assert checks["fix_pr_exists"] == "pass"
        assert checks["pods_ready"] == "fail"

    def test_rerun_pipeline_green_build_recovers(self, sqlite_sessions, store):
        plan = _plan("rerun_pipeline", {"job": "payment-api"})
        deps, caps = _verify_deps(sqlite_sessions, _llm([plan]), _jenkins_doc("SUCCESS"), store)
        incident_id = _make_incident(sqlite_sessions, JENKINS_SIGNAL)

        result = run_incident(incident_id, deps=deps)

        assert result.status == IncidentStatus.RECOVERED
        assert caps["jenkins_write"].methods() == ["trigger_build"]
        row = _rows(sqlite_sessions, models.Verification, incident_id)[0]
        assert row.outcome == models.VerificationOutcome.PASS
        assert {c["name"]: c["status"] for c in row.checks} == {"build_green": "pass"}

    def test_compensable_failure_records_needs_compensation(self, sqlite_sessions, store):
        """scale_deployment (compensable) fails verification -> replan
        with the debt recorded; the compensation hook itself is 1.10."""
        doc = _healthy_doc()
        # The scale lands but the pods never come up in-window.
        doc["evidence"]["pods_payment_api"] = {"items": [_pod(ready=False, phase="Pending")]}
        doc["evidence"]["pods_payment_api"]["items"][0]["status"]["containerStatuses"][0][
            "state"
        ] = {"waiting": {"reason": "ContainerCreating"}}
        params = {**K8S, "replicas": 2}
        plan = _plan("scale_deployment", params)
        # The claim names the capacity surface so the ranker scores the
        # scale candidate above the select bar.
        deps, _ = _verify_deps(
            sqlite_sessions,
            _llm([plan], claim="payment-api scaled but replica count never converged"),
            doc,
            store,
        )
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)

        result = run_incident(incident_id, deps=deps)

        assert result.status == IncidentStatus.ESCALATED  # empty replan escalates
        row = _rows(sqlite_sessions, models.Verification, incident_id)[0]
        assert row.outcome == models.VerificationOutcome.FAIL
        assert row.needs_compensation is True  # the recorded debt

    def test_completed_verification_row_routes_without_rerunning(self, sqlite_sessions, store):
        """A crash after the verify node wrote its row but before the
        coordinator routed it: the rerun loads the recorded verdict and
        closes the incident - no second verifications row, no re-sample."""
        from datetime import UTC, datetime

        from state.machine import transition

        deps, caps = _verify_deps(sqlite_sessions, FakeLLM(), _healthy_doc(), store)
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)
        params = dict(K8S)
        fp = candidate_fingerprint("restart_deployment", params)
        now = datetime.now(UTC)
        with sqlite_sessions() as session:
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == incident_id)
            )
            cand = models.Candidate(
                incident_id=incident.id,
                attempt=0,
                rank=1,
                action="restart_deployment",
                params=params,
                rationale="seeded",
                evidence_refs=[],
                score=2.0,
                risk_tier="low",
                selected=True,
                components={},
            )
            session.add(cand)
            session.flush()
            exe = models.Execution(
                incident_id=incident.id,
                candidate_id=cand.id,
                attempt=0,
                action="restart_deployment",
                params=params,
                fingerprint=fp,
                policy_decision="allow_with_checks",
                verification_required=True,
                decided_by=None,
                approval_id=None,
                origin="fixture",
                result=models.ExecutionResult.SUCCESS,
                detail="restarted",
                output={"op": "rollout_restart"},
                started_at=now,
                finished_at=now,
            )
            session.add(exe)
            session.flush()
            # The completed verify pass for THIS execution - the crash
            # landed between the row write and the coordinator's route.
            session.add(
                models.Verification(
                    incident_id=incident.id,
                    execution_id=exe.id,
                    candidate_id=cand.id,
                    attempt=0,
                    action="restart_deployment",
                    params=params,
                    fingerprint=fp,
                    outcome=models.VerificationOutcome.PASS,
                    detail="held across 4s",
                    checks=[
                        {
                            "name": "pods_ready",
                            "status": "pass",
                            "detail": "held",
                            "evidence_id": None,
                        }
                    ],
                    window_seconds=4.0,
                    poll_seconds=2.0,
                    needs_compensation=False,
                    started_at=now,
                    finished_at=now,
                )
            )
            for status in (
                IncidentStatus.DIAGNOSING,
                IncidentStatus.REMEDIATION_PENDING,
                IncidentStatus.VALIDATING,
                IncidentStatus.EXECUTING,
                IncidentStatus.VERIFYING,
            ):
                transition(incident, status, cause="seed", session=session)

        result = run_incident(incident_id, deps=deps)

        assert result.error is None
        assert result.status == IncidentStatus.RECOVERED
        # Exactly the seeded row - the verify node did not re-run.
        verifications = _rows(sqlite_sessions, models.Verification, incident_id)
        assert len(verifications) == 1
        assert caps["kube_write"].calls == []  # nothing re-executed either
        assert _transitions(sqlite_sessions, incident_id)[-1] == ("verifying", "recovered")

    def test_verify_node_never_executes(self, sqlite_sessions, store):
        """The verify node calls no write transport - verification is
        observation plus the functional probe, never a mutation."""
        plan = _plan("restart_deployment", dict(K8S))
        deps, caps = _verify_deps(sqlite_sessions, _llm([plan]), _healthy_doc(), store)
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)

        run_incident(incident_id, deps=deps)

        # Exactly one write ever happened: the execute node's rollout
        # restart. The verify node added nothing.
        assert caps["kube_write"].methods() == ["rollout_restart"]
        assert caps["github_write"].calls == []
        assert caps["jenkins_write"].calls == []

    def test_no_execution_to_verify_records_a_failed_pass(self, sqlite_sessions, store):
        """A run resumed at `verifying` with no successful executions row
        still produces a verdict row: the un-runnable "(none)" check folds
        to fail, and the coordinator replans (the FakeLLM's empty queue
        escalates the empty replan)."""
        from state.machine import transition

        deps, _ = _verify_deps(sqlite_sessions, FakeLLM(), _healthy_doc(), store)
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)
        with sqlite_sessions() as session:
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == incident_id)
            )
            for status in (
                IncidentStatus.DIAGNOSING,
                IncidentStatus.REMEDIATION_PENDING,
                IncidentStatus.VALIDATING,
                IncidentStatus.EXECUTING,
                IncidentStatus.VERIFYING,
            ):
                transition(incident, status, cause="seed", session=session)

        result = run_incident(incident_id, deps=deps)

        assert result.error is None
        assert result.status == IncidentStatus.ESCALATED
        verifications = _rows(sqlite_sessions, models.Verification, incident_id)
        assert len(verifications) == 1
        assert verifications[0].outcome == models.VerificationOutcome.FAIL
        assert verifications[0].action == "(none)"
        assert verifications[0].execution_id is None
        assert "no successful execution" in result.errors[0]


class TestCoordinatorVerifyingRouting:
    """The coordinator's verdict table for `verifying`, decided purely
    over state. Reversibility comes from the catalogue, not the state
    payload - the tests use real action names."""

    def _state(self, verification):
        return {
            "status": IncidentStatus.VERIFYING,
            "verification": verification,
        }

    def test_no_verification_routes_to_node(self):
        assert _decide(self._state(None), None).to_status is None

    def test_pass_recovers(self):
        decision = _decide(self._state({"action": "restart_deployment", "outcome": "pass"}), None)
        assert decision.to_status == IncidentStatus.RECOVERED

    def test_fail_stateless_replans(self):
        decision = _decide(
            self._state(
                {
                    "action": "restart_deployment",
                    "outcome": "fail",
                    "fingerprint": "fp1",
                }
            ),
            None,
        )
        assert decision.to_status == IncidentStatus.REMEDIATION_PENDING
        assert decision.replan and decision.exclude_fingerprint == "fp1"

    def test_fail_compensable_replans(self):
        decision = _decide(
            self._state(
                {
                    "action": "scale_deployment",
                    "outcome": "fail",
                    "fingerprint": "fp2",
                }
            ),
            None,
        )
        assert decision.to_status == IncidentStatus.REMEDIATION_PENDING
        assert decision.replan

    def test_fail_irreversible_escalates(self, monkeypatch):
        """No catalogue action is irreversible yet - the routing exists for
        when one lands, so the test registers a synthetic irreversible spec."""
        from dataclasses import replace

        from agent.actions.catalogue import Reversibility, get_action

        spec = replace(get_action("restart_deployment"), reversibility=Reversibility.IRREVERSIBLE)
        monkeypatch.setattr(
            "agent.agents.coordinator.get_action",
            lambda name: spec if name == "restart_deployment" else get_action(name),
        )
        decision = _decide(
            self._state(
                {
                    "action": "restart_deployment",
                    "outcome": "fail",
                    "fingerprint": "fp3",
                }
            ),
            None,
        )
        assert decision.to_status == IncidentStatus.ESCALATED
        assert not decision.replan


class TestLiveGitHubWriteWithRecordedEvidence:
    """HELIX_GITHUB_WRITES=live on a recorded-evidence deployment (the
    hosted demo): the fix PR is really opened, the executions row says
    origin live, and the PR is read back from real GitHub - while every
    other leg stays recorded."""

    PARAMS = {
        "branch": "helix/INC-1",
        "title": "helix: restore payment-api deployment",
        "files": [
            {
                "path": "sandbox/payment-api/k8s/04-deployment.yaml",
                "content": (
                    "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: payment-api\n"
                ),
            }
        ],
    }

    def _live_deps(self, sessions, store, *, readback_state="open", readback_status=200):
        import dataclasses

        import httpx

        from integrations.github.client import GitHubEvidenceClient
        from integrations.github.write import GitHubWriteClient

        seen: list[tuple[str, str]] = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append((req.method, req.url.path))
            path = req.url.path
            if path.endswith("/git/ref/heads/main"):
                return httpx.Response(200, json={"object": {"sha": "basesha"}})
            if path.endswith("/git/refs"):
                return httpx.Response(201, json={})
            if "/contents/" in path:
                if req.method == "GET":
                    return httpx.Response(404, json={})
                return httpx.Response(201, json={"commit": {"sha": "c1"}})
            if path.endswith("/pulls") and req.method == "POST":
                return httpx.Response(201, json={"number": 7, "html_url": "https://gh/pr/7"})
            if path.endswith("/pulls/7"):
                if readback_status != 200:
                    return httpx.Response(readback_status, json={"message": "Not Found"})
                return httpx.Response(
                    200,
                    json={
                        "number": 7,
                        "state": readback_state,
                        "title": "fix",
                        "head": {"ref": "helix/INC-1", "sha": "c1"},
                        "base": {"ref": "main"},
                        "merged": False,
                    },
                )
            return httpx.Response(500, json={})

        def http() -> httpx.Client:
            return httpx.Client(
                base_url="https://api.github.com", transport=httpx.MockTransport(handler)
            )

        deps, caps = _verify_deps(
            sessions, _llm([_plan("open_fix_pr", self.PARAMS, "pr")]), _healthy_doc(), store
        )
        deps = dataclasses.replace(
            deps,
            github_write=GitHubWriteClient(repo="acme/sbx", token="t", client=http()),
            github_readback=GitHubEvidenceClient(repo="acme/sbx", token="t", client=http()),
        )
        return deps, caps, seen

    def test_pr_is_opened_via_the_api_origin_is_live_and_readback_passes(
        self, sqlite_sessions, store
    ):
        deps, caps, seen = self._live_deps(sqlite_sessions, store)
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)

        result = run_incident(incident_id, deps=deps)

        assert result.status == IncidentStatus.RECOVERED
        assert ("POST", "/repos/acme/sbx/pulls") in seen
        assert ("GET", "/repos/acme/sbx/pulls/7") in seen  # read back from the real API
        execution = _rows(sqlite_sessions, models.Execution, incident_id)[0]
        assert execution.origin == "live"
        assert execution.output["pr_number"] == 7
        checks = {
            c["name"]: c["status"]
            for c in _rows(sqlite_sessions, models.Verification, incident_id)[0].checks
        }
        assert checks["fix_pr_exists"] == "pass"
        assert "get_pull_request" not in caps["github"].methods()

    def test_closed_pr_on_readback_fails_verification(self, sqlite_sessions, store):
        deps, _, _ = self._live_deps(sqlite_sessions, store, readback_state="closed")
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)

        run_incident(incident_id, deps=deps)

        row = _rows(sqlite_sessions, models.Verification, incident_id)[0]
        assert {c["name"]: c["status"] for c in row.checks}["fix_pr_exists"] == "fail"
        assert row.outcome == models.VerificationOutcome.FAIL

    def test_missing_pr_on_readback_fails_verification(self, sqlite_sessions, store):
        deps, _, _ = self._live_deps(sqlite_sessions, store, readback_status=404)
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)

        run_incident(incident_id, deps=deps)

        row = _rows(sqlite_sessions, models.Verification, incident_id)[0]
        assert {c["name"]: c["status"] for c in row.checks}["fix_pr_exists"] == "fail"

    def test_recorded_write_keeps_fixture_origin(self, sqlite_sessions, store):
        """Unset behaviour is unchanged: the recording fake, origin fixture."""
        deps, _ = _verify_deps(
            sqlite_sessions, _llm([_plan("open_fix_pr", self.PARAMS, "pr")]), _healthy_doc(), store
        )
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)

        run_incident(incident_id, deps=deps)

        assert _rows(sqlite_sessions, models.Execution, incident_id)[0].origin == "fixture"
