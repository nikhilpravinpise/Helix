"""Write-transport unit tests (subphase 1.8): the code-level bounds that
exist independent of the policy verdict - kube context allowlist,
helix/INC-* branch + sandbox write root, Jenkins crumb POST - plus the
fixture-mode recording fakes.

No live systems: the kube client runs an injected argv runner, the
github/jenkins clients run on httpx.MockTransport.
"""

import json
import subprocess

import httpx
import pytest

from integrations.fakes import FakeGitHubWrite, FakeJenkinsWrite, FakeKubernetesWrite
from integrations.github.write import GitHubWriteClient
from integrations.jenkins.write import JenkinsWriteClient
from integrations.kubernetes.write import KubernetesWriteClient
from integrations.writes import (
    WriteAuthError,
    WriteError,
    WriteRefusedError,
    WriteTimeoutError,
)


def _proc(returncode: int = 0, stdout: str = "ok", stderr: str = ""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class TestKubernetesWriteContext:
    """HELIX_ALLOWED_KUBE_CONTEXTS is enforced in the client - a wrong
    context is refused even when policy would allow the action."""

    def test_allowed_context_runs_kubectl(self, monkeypatch):
        monkeypatch.setenv("HELIX_ALLOWED_KUBE_CONTEXTS", "kind-helix-sandbox")
        argv_seen = []
        client = KubernetesWriteClient(
            context="kind-helix-sandbox",
            runner=lambda argv, **kw: argv_seen.append(argv) or _proc(),
        )
        out = client.rollout_restart("payment-api", "payment-api")
        assert argv_seen[0][:3] == ["kubectl", "--context", "kind-helix-sandbox"]
        assert "rollout" in argv_seen[0] and "restart" in argv_seen[0]
        assert "deployment/payment-api" in argv_seen[0]
        assert out["op"] == "rollout_restart"

    def test_wrong_context_refused_before_runner(self, monkeypatch):
        monkeypatch.setenv("HELIX_ALLOWED_KUBE_CONTEXTS", "kind-helix-sandbox")
        called = []
        client = KubernetesWriteClient(
            context="prod-eu-1",
            runner=lambda argv, **kw: called.append(argv),
        )
        with pytest.raises(WriteRefusedError, match="not in HELIX_ALLOWED_KUBE_CONTEXTS"):
            client.rollout_restart("payment-api", "payment-api")
        with pytest.raises(WriteRefusedError):
            client.scale("payment-api", "payment-api", 2)
        with pytest.raises(WriteRefusedError):
            client.apply_manifest("/tmp/x.yaml")
        assert called == []  # nothing ever spawned

    def test_multiple_allowed_contexts(self, monkeypatch):
        monkeypatch.setenv("HELIX_ALLOWED_KUBE_CONTEXTS", "kind-helix-sandbox, kind-other")
        client = KubernetesWriteClient(context="kind-other", runner=lambda argv, **kw: _proc())
        assert client.scale("payment-api", "payment-api", 2)["op"] == "scale"

    def test_default_context_comes_from_settings(self):
        client = KubernetesWriteClient(runner=lambda argv, **kw: _proc())
        assert client.rollout_undo("payment-api", "payment-api")["op"] == "rollout_undo"

    def test_kubectl_failure_maps_to_write_error(self, monkeypatch):
        monkeypatch.setenv("HELIX_ALLOWED_KUBE_CONTEXTS", "kind-helix-sandbox")
        client = KubernetesWriteClient(
            context="kind-helix-sandbox",
            runner=lambda argv, **kw: _proc(returncode=1, stderr="connection refused"),
        )
        with pytest.raises(WriteError, match="connection refused"):
            client.rollout_restart("payment-api", "payment-api")

    def test_kubectl_timeout_maps_to_timeout(self, monkeypatch):
        monkeypatch.setenv("HELIX_ALLOWED_KUBE_CONTEXTS", "kind-helix-sandbox")

        def timeout_run(argv, **kw):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=60)

        client = KubernetesWriteClient(context="kind-helix-sandbox", runner=timeout_run)
        with pytest.raises(WriteTimeoutError):
            client.rollout_restart("payment-api", "payment-api")


class TestGitHubWriteBounds:
    """The branch/path bounds are enforced before any HTTP request."""

    def _client(self, handler):
        transport = httpx.MockTransport(handler)
        return GitHubWriteClient(
            repo="acme/payment-sandbox",
            token="t",
            client=httpx.Client(transport=transport),
        )

    def test_protected_branch_refused_without_http(self):
        requests = []
        client = self._client(lambda req: requests.append(req) or httpx.Response(200, json={}))
        with pytest.raises(WriteRefusedError, match="helix/INC-"):
            client.open_fix_pr(
                branch="main",
                files=[{"path": "sandbox/payment-api/x.py", "content": "x"}],
                title="nope",
            )
        assert requests == []  # refusal fired before any call

    def test_non_incident_branch_refused(self):
        requests = []
        client = self._client(lambda req: requests.append(req) or httpx.Response(200))
        with pytest.raises(WriteRefusedError):
            client.open_fix_pr(
                branch="helix/something-else",
                files=[{"path": "sandbox/payment-api/x.py", "content": "x"}],
                title="nope",
            )
        assert requests == []

    def test_path_outside_write_root_refused(self):
        requests = []
        client = self._client(lambda req: requests.append(req) or httpx.Response(200))
        with pytest.raises(WriteRefusedError, match="writable"):
            client.open_fix_pr(
                branch="helix/INC-deadbeef",
                files=[{"path": "pyproject.toml", "content": "x"}],
                title="nope",
            )
        assert requests == []

    def test_traversal_path_refused(self):
        client = self._client(lambda req: httpx.Response(200))
        with pytest.raises(WriteRefusedError):
            client.open_fix_pr(
                branch="helix/INC-deadbeef",
                files=[{"path": "sandbox/payment-api/../secrets/x.py", "content": "x"}],
                title="nope",
            )

    def test_open_fix_pr_happy_path_rest_calls(self):
        """The write order: read base ref -> create branch ref -> put
        contents -> open PR. Asserted on the request sequence."""
        calls = []

        def handler(req: httpx.Request) -> httpx.Response:
            calls.append((req.method, req.url.path))
            if req.url.path.endswith("/git/ref/heads/main"):
                return httpx.Response(200, json={"object": {"sha": "basesha"}})
            if req.url.path.endswith("/git/refs"):
                return httpx.Response(201, json={"ref": "refs/heads/helix/INC-abc"})
            if "/contents/" in req.url.path and req.method == "GET":
                return httpx.Response(404, json={})
            if "/contents/" in req.url.path and req.method == "PUT":
                return httpx.Response(201, json={"commit": {"sha": "c1"}})
            if req.url.path.endswith("/pulls"):
                return httpx.Response(201, json={"number": 7, "html_url": "https://gh/pr/7"})
            return httpx.Response(500, json={})

        client = self._client(handler)
        out = client.open_fix_pr(
            branch="helix/INC-abc",
            files=[{"path": "sandbox/payment-api/app/main.py", "content": "print(1)\n"}],
            title="helix: fix",
            body="incident INC-abc",
        )
        methods = [m for m, _ in calls]
        assert methods == ["GET", "POST", "GET", "PUT", "POST"]
        assert calls[1][1].endswith("/git/refs")
        assert calls[-1][1].endswith("/pulls")
        assert out["pr_number"] == 7
        assert out["pr_url"] == "https://gh/pr/7"
        assert out["branch"] == "helix/INC-abc"
        assert out["origin"] == "live"

    def test_existing_branch_is_reused(self):
        """A retry on an already-created branch ref proceeds - the POST
        refs 422 carrying 'Reference already exists' is not fatal."""

        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.path.endswith("/git/ref/heads/main"):
                return httpx.Response(200, json={"object": {"sha": "basesha"}})
            if req.url.path.endswith("/git/refs"):
                return httpx.Response(422, json={"message": "Reference already exists"})
            if "/contents/" in req.url.path and req.method == "GET":
                return httpx.Response(200, json={"sha": "old"})
            if "/contents/" in req.url.path:
                return httpx.Response(200, json={"commit": {"sha": "c2"}})
            if req.url.path.endswith("/pulls"):
                return httpx.Response(201, json={"number": 8, "html_url": "u"})
            return httpx.Response(500)

        client = self._client(handler)
        out = client.open_fix_pr(
            branch="helix/INC-abc",
            files=[{"path": "sandbox/payment-api/a.py", "content": "x"}],
            title="t",
        )
        assert out["pr_number"] == 8


class TestJenkinsWrite:
    def _client(self, handler):
        transport = httpx.MockTransport(handler)
        return JenkinsWriteClient(
            base_url="http://jenkins.test",
            user="u",
            token="t",
            client=httpx.Client(transport=transport),
        )

    def test_trigger_build_posts_with_crumb(self):
        calls = []

        def handler(req: httpx.Request) -> httpx.Response:
            calls.append((req.method, req.url.path, dict(req.headers)))
            if req.url.path == "/crumbIssuer/api/json":
                return httpx.Response(
                    200, json={"crumbRequestField": "Jenkins-Crumb", "crumb": "abc"}
                )
            if req.url.path == "/job/payment-api/build":
                return httpx.Response(
                    201, headers={"Location": "http://jenkins.test/queue/item/9/"}
                )
            return httpx.Response(500)

        out = self._client(handler).trigger_build("payment-api")

        assert [c[1] for c in calls] == [
            "/crumbIssuer/api/json",
            "/job/payment-api/build",
        ]
        assert calls[1][0] == "POST"
        assert calls[1][2].get("jenkins-crumb") == "abc"
        assert out["queue_url"].endswith("/queue/item/9/")
        assert out["job"] == "payment-api"

    def test_missing_crumb_issuer_is_tolerated(self):
        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.path == "/crumbIssuer/api/json":
                return httpx.Response(404)
            return httpx.Response(201, headers={"Location": "q"})

        out = self._client(handler).trigger_build("payment-api")
        assert out["queue_url"] == "q"

    def test_auth_error_maps(self):
        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.path == "/crumbIssuer/api/json":
                return httpx.Response(404)
            return httpx.Response(403)

        with pytest.raises(WriteAuthError):
            self._client(handler).trigger_build("payment-api")


class TestFakeWrites:
    """The fixture transports record the intended write AND enforce the
    same code-level bounds, so fixture runs exercise real refusals."""

    def test_kube_fake_records_and_enforces_context(self, monkeypatch):
        monkeypatch.setenv("HELIX_ALLOWED_KUBE_CONTEXTS", "kind-helix-sandbox")
        fake = FakeKubernetesWrite()
        out = fake.rollout_restart("payment-api", "payment-api")
        assert out["recorded"] is True and out["origin"] == "fixture"
        assert fake.calls[0]["op"] == "rollout_restart"

        bad = FakeKubernetesWrite(context="prod-eu-1")
        with pytest.raises(WriteRefusedError):
            bad.rollout_restart("payment-api", "payment-api")
        assert bad.calls == []

    def test_github_fake_records_and_enforces_bounds(self):
        fake = FakeGitHubWrite()
        out = fake.open_fix_pr(
            branch="helix/INC-abc",
            files=[{"path": "sandbox/payment-api/a.py", "content": "x=1"}],
            title="fix",
        )
        assert out["recorded"] is True
        assert out["files"][0]["path"] == "sandbox/payment-api/a.py"

        with pytest.raises(WriteRefusedError):
            fake.open_fix_pr(
                branch="main",
                files=[{"path": "sandbox/payment-api/a.py", "content": "x"}],
                title="nope",
            )
        with pytest.raises(WriteRefusedError):
            fake.open_fix_pr(
                branch="helix/INC-abc",
                files=[{"path": "README.md", "content": "x"}],
                title="nope",
            )
        assert len(fake.calls) == 1  # refusals record nothing

    def test_jenkins_fake_records_trigger(self):
        fake = FakeJenkinsWrite()
        out = fake.trigger_build("payment-api")
        assert out == {
            "op": "trigger_build",
            "recorded": True,
            "origin": "fixture",
            "job": "payment-api",
            "queue_url": None,
        }
        assert fake.calls == [out]


class TestWriteFactorySelection:
    """D10: HELIX_EVIDENCE_SOURCE picks fake vs live write transports."""

    def test_fixtures_source_selects_fakes(self, monkeypatch):
        monkeypatch.setenv("HELIX_EVIDENCE_SOURCE", "fixtures")
        from integrations.writes import (
            get_github_write,
            get_jenkins_write,
            get_kubernetes_write,
        )

        assert isinstance(get_kubernetes_write(), FakeKubernetesWrite)
        assert isinstance(get_github_write(), FakeGitHubWrite)
        assert isinstance(get_jenkins_write(), FakeJenkinsWrite)

    def test_live_source_selects_clients(self, monkeypatch):
        monkeypatch.setenv("HELIX_EVIDENCE_SOURCE", "live")
        from integrations.writes import (
            get_github_write,
            get_jenkins_write,
            get_kubernetes_write,
        )

        assert isinstance(get_kubernetes_write(), KubernetesWriteClient)
        assert isinstance(get_github_write(), GitHubWriteClient)
        assert isinstance(get_jenkins_write(), JenkinsWriteClient)


def _deployment(revision: int, uid: str = "dep-uid") -> dict:
    return {
        "metadata": {
            "uid": uid,
            "annotations": {"deployment.kubernetes.io/revision": str(revision)},
        }
    }


def _rs(revision: int, ready: int, owner: str = "dep-uid") -> dict:
    return {
        "metadata": {
            "name": f"payment-api-r{revision}",
            "annotations": {"deployment.kubernetes.io/revision": str(revision)},
            "ownerReferences": [{"kind": "Deployment", "uid": owner}],
        },
        "status": {"readyReplicas": ready} if ready else {},
    }


class TestRollbackTarget:
    """FOLLOWUPS #13, seen live twice: `rollout undo` goes to revision N-1,
    and after earlier faults N-1 was itself a broken version with no pods -
    while the last good version was still up and serving. A broken rollout
    never becomes ready, so Kubernetes keeps the previous good ReplicaSet
    serving; that ReplicaSet is the revision to return to."""

    def test_the_serving_revision_wins_over_the_previous_one(self):
        from integrations.kubernetes.write import serving_rollback_revision

        replicasets = {"items": [_rs(5, 0), _rs(4, 0), _rs(3, 1)]}
        assert serving_rollback_revision(_deployment(5), replicasets) == 3

    def test_the_current_revision_is_never_the_target(self):
        from integrations.kubernetes.write import serving_rollback_revision

        replicasets = {"items": [_rs(5, 2), _rs(4, 0)]}
        assert serving_rollback_revision(_deployment(5), replicasets) is None

    def test_another_deployments_replicasets_are_ignored(self):
        from integrations.kubernetes.write import serving_rollback_revision

        replicasets = {"items": [_rs(5, 0), _rs(9, 3, owner="other-uid")]}
        assert serving_rollback_revision(_deployment(5), replicasets) is None

    def test_the_newest_serving_revision_is_chosen(self):
        from integrations.kubernetes.write import serving_rollback_revision

        replicasets = {"items": [_rs(6, 0), _rs(4, 1), _rs(5, 1)]}
        assert serving_rollback_revision(_deployment(6), replicasets) == 5


class TestRollbackCommand:
    def _client(self, outputs, seen):
        def runner(argv, **kwargs):
            seen.append(argv)
            return _proc(stdout=outputs.pop(0))

        return KubernetesWriteClient(context="kind-helix-sandbox", runner=runner)

    def test_rolls_back_to_the_serving_revision(self, monkeypatch):
        monkeypatch.setenv("HELIX_ALLOWED_KUBE_CONTEXTS", "kind-helix-sandbox")
        seen = []
        client = self._client(
            [
                json.dumps(_deployment(5)),
                json.dumps({"items": [_rs(5, 0), _rs(4, 0), _rs(3, 1)]}),
                "rolled back",
            ],
            seen,
        )
        out = client.rollout_undo("payment-api", "payment-api")
        assert "--to-revision=3" in seen[-1]
        assert out["target_revision"] == 3
        assert out["basis"] == "serving"

    def test_with_nothing_serving_it_falls_back_and_says_so(self, monkeypatch):
        monkeypatch.setenv("HELIX_ALLOWED_KUBE_CONTEXTS", "kind-helix-sandbox")
        seen = []
        client = self._client(
            [json.dumps(_deployment(5)), json.dumps({"items": [_rs(5, 0), _rs(4, 0)]}), "ok"],
            seen,
        )
        out = client.rollout_undo("payment-api", "payment-api")
        assert not any(arg.startswith("--to-revision") for arg in seen[-1])
        assert out["basis"] == "previous"

    def test_unreadable_history_still_rolls_back(self, monkeypatch):
        """Failing to read the history must not cancel the remediation."""
        monkeypatch.setenv("HELIX_ALLOWED_KUBE_CONTEXTS", "kind-helix-sandbox")
        seen = []
        client = self._client(["not json", "not json", "ok"], seen)
        out = client.rollout_undo("payment-api", "payment-api")
        assert out["op"] == "rollout_undo"
        assert out["basis"] == "previous"
