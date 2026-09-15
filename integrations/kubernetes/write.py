"""Controlled Kubernetes writes (subphase 1.8): rollout restart, rollout
undo, scale, and apply of a committed manifest.

Implemented via the `kubectl` CLI over subprocess - the same approach the
sandbox scripts take (`kctl` in scripts/common.sh) - with `runner` as the
injection seam for tests (defaults to subprocess.run).

The hard boundary, enforced HERE and independent of the policy verdict:
every call carries `--context <context>` and `_check_context` refuses any
context outside `HELIX_ALLOWED_KUBE_CONTEXTS` before the subprocess ever
spawns. A wrong context is a refusal even when policy would allow the
action - that is the second enforcement layer.

Error mapping: missing CLI -> WriteError, TimeoutExpired ->
WriteTimeoutError, non-zero exit -> WriteError carrying stderr.
"""

import subprocess
from typing import Any

from helix.config import get_settings
from integrations.writes import WriteError, WriteRefusedError, WriteTimeoutError

KUBECTL_TIMEOUT = 60


class KubernetesWriteClient:
    """kubectl-backed write capability bound to one kube context.

    `runner` is the test seam: a `callable(argv) -> CompletedProcess`.
    The context allowlist check happens on every call, not just at
    construction, so a settings change cannot be laundered through a
    long-lived client."""

    def __init__(
        self,
        context: str | None = None,
        *,
        runner: Any = None,
        kubectl_bin: str = "kubectl",
    ) -> None:
        self._context = context or get_settings().kube_context
        self._run = runner or subprocess.run
        self._kubectl = kubectl_bin

    def _check_context(self) -> None:
        """Refuse any context not in HELIX_ALLOWED_KUBE_CONTEXTS."""
        allowed = get_settings().allowed_kube_context_set()
        if self._context not in allowed:
            raise WriteRefusedError(
                f"kube context {self._context!r} is not in HELIX_ALLOWED_KUBE_CONTEXTS "
                f"({sorted(allowed)})"
            )

    def _exec(self, *args: str) -> str:
        self._check_context()
        argv = [self._kubectl, "--context", self._context, *args]
        try:
            proc = self._run(argv, capture_output=True, text=True, timeout=KUBECTL_TIMEOUT)
        except FileNotFoundError as exc:
            raise WriteError(f"kubectl CLI not found: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise WriteTimeoutError(f"kubectl {args[0]} timed out") from exc
        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()
            raise WriteError(f"kubectl {args[0]} failed: {stderr}")
        return proc.stdout

    def _result(self, op: str, argv_tail: list[str], stdout: str) -> dict[str, Any]:
        return {
            "op": op,
            "command": [self._kubectl, "--context", self._context, *argv_tail],
            "context": self._context,
            "stdout": stdout.strip(),
            "origin": "live",
        }

    def rollout_restart(self, deployment: str, namespace: str) -> dict[str, Any]:
        """Patch the pod template's restartAt annotation via kubectl's
        rollout restart - recreates pods without changing the spec."""
        tail = ["rollout", "restart", f"deployment/{deployment}", "-n", namespace]
        return self._result("rollout_restart", tail, self._exec(*tail))

    def rollout_undo(self, deployment: str, namespace: str) -> dict[str, Any]:
        """Roll back to the previous revision (undoes out-of-band set
        image/env/resources changes)."""
        tail = ["rollout", "undo", f"deployment/{deployment}", "-n", namespace]
        return self._result("rollout_undo", tail, self._exec(*tail))

    def scale(self, deployment: str, namespace: str, replicas: int) -> dict[str, Any]:
        tail = [
            "scale",
            f"deployment/{deployment}",
            f"--replicas={replicas}",
            "-n",
            namespace,
        ]
        return self._result("scale", tail, self._exec(*tail))

    def apply_manifest(self, path: str) -> dict[str, Any]:
        """Apply one committed manifest file. The allowlist of which files
        may be applied lives in the catalogue params model and is
        re-asserted by the caller - this client applies what it is given."""
        tail = ["apply", "-f", str(path)]
        return self._result("apply_manifest", tail, self._exec(*tail))
