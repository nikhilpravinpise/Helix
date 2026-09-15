"""Fixture-mode write transports (subphase 1.8, D10).

When HELIX_EVIDENCE_SOURCE=fixtures the write capabilities are these
recording fakes: each call is appended to `.calls` (the in-memory audit
the tests and benchmark inspect) and answered with a canned success whose
payload describes the intended write. That payload lands in
`executions.output`, so "the write Helix would have made" is queryable
per incident without any real system ever being touched.

The fakes enforce the SAME code-level bounds as the live clients
(kube context allowlist, helix/INC-* branch, sandbox/payment-api/ write
root) so fixture-mode runs exercise the real refusal paths - a bound
that only existed live would make fixture runs dishonest.
"""

from typing import Any

from helix.config import get_settings
from integrations.github.write import _check_branch, _check_path
from integrations.writes import WriteRefusedError


class _RecordingWrite:
    """Shared record + canned-success shape for the fake transports."""

    origin = "fixture"

    def __init__(self) -> None:
        #: Every intended write, in order: {"op": ..., params...}. The
        #: same payload is returned to the caller and lands in
        #: executions.output.
        self.calls: list[dict[str, Any]] = []

    def _record(self, op: str, **fields: Any) -> dict[str, Any]:
        call = {"op": op, "recorded": True, "origin": self.origin, **fields}
        self.calls.append(call)
        return call


class FakeKubernetesWrite(_RecordingWrite):
    """Records rollout restart/undo/scale/apply; no cluster is touched.
    The context allowlist is still enforced - a misconfigured context is
    refused even in fixture mode."""

    def __init__(self, context: str | None = None) -> None:
        super().__init__()
        self._context = context or get_settings().kube_context

    def _check_context(self) -> None:
        allowed = get_settings().allowed_kube_context_set()
        if self._context not in allowed:
            raise WriteRefusedError(
                f"kube context {self._context!r} is not in HELIX_ALLOWED_KUBE_CONTEXTS "
                f"(fixture write refused)"
            )

    def rollout_restart(self, deployment: str, namespace: str) -> dict[str, Any]:
        self._check_context()
        return self._record(
            "rollout_restart",
            deployment=deployment,
            namespace=namespace,
            context=self._context,
        )

    def rollout_undo(self, deployment: str, namespace: str) -> dict[str, Any]:
        self._check_context()
        return self._record(
            "rollout_undo",
            deployment=deployment,
            namespace=namespace,
            context=self._context,
        )

    def scale(self, deployment: str, namespace: str, replicas: int) -> dict[str, Any]:
        self._check_context()
        return self._record(
            "scale",
            deployment=deployment,
            namespace=namespace,
            replicas=replicas,
            context=self._context,
        )

    def apply_manifest(self, path: str) -> dict[str, Any]:
        self._check_context()
        return self._record("apply_manifest", path=str(path), context=self._context)


class FakeGitHubWrite(_RecordingWrite):
    """Records the fix-PR intent; no repo is touched. The branch/path
    bounds are the live client's own checks - the same code runs."""

    def open_fix_pr(
        self,
        *,
        branch: str,
        files: list[dict[str, str]],
        title: str,
        body: str = "",
    ) -> dict[str, Any]:
        _check_branch(branch)
        for change in files:
            _check_path(change["path"])
        return self._record(
            "open_fix_pr",
            branch=branch,
            files=[{"path": c["path"], "bytes": len(c["content"])} for c in files],
            title=title,
            pr_url=None,
            pr_number=None,
        )

    def close_pr(self, *, branch: str, pr_number: int | None = None) -> dict[str, Any]:
        """The open_fix_pr compensation (1.10): records the close intent
        under the same helix/INC-* branch bound - no repo is touched."""
        _check_branch(branch)
        return self._record("close_pr", branch=branch, pr_number=pr_number)


class FakeJenkinsWrite(_RecordingWrite):
    """Records the build trigger; no Jenkins call is made."""

    def trigger_build(self, job: str) -> dict[str, Any]:
        return self._record("trigger_build", job=job, queue_url=None)
