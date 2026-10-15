"""Controlled-write capability interfaces, in Helix vocabulary (1.8).

The write side mirrors the read side's shape: each vendor domain exposes a
small Protocol whose methods return plain JSON-able dicts - never vendor
SDK objects (C8) - and a `get_*_write()` factory picks the live client or
the recording fake from `HELIX_EVIDENCE_SOURCE` (D10: write transports
follow the evidence mode, so fixture-mode benchmark runs record intended
writes instead of mutating real systems).

The error hierarchy is parallel to the evidence one but distinct:
`WriteRefusedError` is special - it is raised by code-level bounds (kube
context allowlist, protected branches, the write root) BEFORE any vendor
call, meaning "this write is outside the enforced boundary", not "a call
failed".
"""

from typing import Any, Protocol

from helix.config import get_settings


class WriteError(Exception):
    """Base class for every failure a write capability can raise."""


class WriteAuthError(WriteError):
    """Authentication or authorization failed on the vendor write call."""


class WriteTimeoutError(WriteError):
    """The vendor write call timed out."""


class WriteRateLimitError(WriteError):
    """The vendor rate-limited the write request."""


class WriteNotFoundError(WriteError):
    """The object the write refers to does not exist."""


class WriteRefusedError(WriteError):
    """The write is outside a code-enforced boundary (kube context
    allowlist, protected branch, sandbox/payment-api/ write root) and was
    refused before any vendor call. Independent of the policy verdict -
    this is the second enforcement layer."""


class KubernetesWrite(Protocol):
    """Controlled Kubernetes mutations, all via the configured context."""

    def rollout_restart(self, deployment: str, namespace: str) -> dict[str, Any]:
        """`kubectl rollout restart deployment/<name>` - pod recreation."""
        ...

    def rollout_undo(self, deployment: str, namespace: str) -> dict[str, Any]:
        """`kubectl rollout undo deployment/<name>` - previous revision."""
        ...

    def scale(self, deployment: str, namespace: str, replicas: int) -> dict[str, Any]:
        """`kubectl scale deployment/<name> --replicas=N`."""
        ...

    def apply_manifest(self, path: str) -> dict[str, Any]:
        """`kubectl apply -f <path>` on a committed manifest file."""
        ...


class GitHubWrite(Protocol):
    """The controlled persistent-fix path: branch + commit + PR. Only
    `helix/INC-*` branches, only `sandbox/payment-api/` paths, never a
    direct push to a protected branch and never a merge."""

    def open_fix_pr(
        self,
        *,
        branch: str,
        files: list[dict[str, str]],
        title: str,
        body: str = "",
    ) -> dict[str, Any]:
        """Create the incident branch, commit the file changes, open a
        PR. Returns the PR coordinates (number, url, branch, files)."""
        ...

    def close_pr(self, *, branch: str, pr_number: int | None = None) -> dict[str, Any]:
        """Close the open PR on the incident branch - the executor-side
        compensation for open_fix_pr (1.10). Idempotent: an already-closed
        or never-opened PR is a successful no-op."""
        ...


class JenkinsWrite(Protocol):
    """Controlled CI mutation: trigger a job build."""

    def trigger_build(self, job: str) -> dict[str, Any]:
        """POST /job/<job>/build (with crumb) - queues a build."""
        ...


def get_kubernetes_write() -> KubernetesWrite:
    """Live kubectl-backed client, or the recording fake in fixture mode."""
    if get_settings().evidence_source == "fixtures":
        from integrations.fakes import FakeKubernetesWrite

        return FakeKubernetesWrite()
    from integrations.kubernetes.write import KubernetesWriteClient

    return KubernetesWriteClient()


def get_github_write() -> GitHubWrite:
    """Live REST client, or the recording fake in fixture mode.
    `HELIX_GITHUB_WRITES` overrides the evidence-source default for this
    one transport (live PRs over recorded evidence)."""
    settings = get_settings()
    recorded = (
        settings.evidence_source == "fixtures"
        if settings.github_writes is None
        else settings.github_writes == "recorded"
    )
    if recorded:
        from integrations.fakes import FakeGitHubWrite

        return FakeGitHubWrite()
    from integrations.github.write import GitHubWriteClient

    return GitHubWriteClient()


def get_jenkins_write() -> JenkinsWrite:
    """Live REST client, or the recording fake in fixture mode."""
    if get_settings().evidence_source == "fixtures":
        from integrations.fakes import FakeJenkinsWrite

        return FakeJenkinsWrite()
    from integrations.jenkins.write import JenkinsWriteClient

    return JenkinsWriteClient()
