"""Docker evidence capability - HOST DAEMON ONLY.

The host Docker daemon cannot see `payment-api` pod containers: those run
in containerd inside the kind node, and `docker ps` on the host will never
list them. This capability covers host-daemon state only - the
`payment-api:kind` image presence/digest, the kind node container, the
Jenkins and Elasticsearch containers. Pod container state belongs to the
Kubernetes capability.

Implemented via the `docker` CLI over subprocess - no docker SDK
dependency. `runner` is an injection seam for tests (defaults to
subprocess.run). Error mapping: missing CLI or daemon-down ->
EvidenceError, "No such ..." -> not found, TimeoutExpired -> timeout,
malformed/empty inspect output -> partial.
"""

import json
import subprocess
from typing import Any

from integrations.evidence import (
    Evidence,
    EvidenceError,
    EvidenceKind,
    EvidenceNotFoundError,
    EvidenceOrigin,
    EvidencePartialError,
    EvidenceSource,
    EvidenceTimeoutError,
    build_evidence,
)


def _is_not_found(stderr: str) -> bool:
    lowered = stderr.lower()
    return "no such" in lowered or "not found" in lowered


class DockerEvidenceClient:
    """Host-daemon Docker evidence capability via the docker CLI."""

    def __init__(self, docker_bin: str = "docker", *, runner: Any = None) -> None:
        self._docker = docker_bin
        self._run = runner or subprocess.run

    def _exec(self, *args: str) -> str:
        try:
            proc = self._run(
                [self._docker, *args],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except FileNotFoundError as exc:
            raise EvidenceError(f"docker CLI not found: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise EvidenceTimeoutError(f"docker {args[0]} timed out") from exc
        if proc.returncode != 0:
            stderr = proc.stderr or ""
            if _is_not_found(stderr):
                raise EvidenceNotFoundError(f"docker: {stderr.strip()}")
            raise EvidenceError(f"docker {args[0]} failed: {stderr.strip()}")
        return proc.stdout

    def _inspect(self, kind: str, name: str) -> dict:
        """`docker <kind> inspect <name>` -> the first object, or a mapped
        error. An empty/malformed result is partial, not an empty object."""
        out = self._exec(kind, "inspect", name)
        try:
            items = json.loads(out)
        except ValueError as exc:
            raise EvidencePartialError(f"docker {kind} inspect returned non-JSON") from exc
        if not items:
            raise EvidenceNotFoundError(f"docker {kind} {name} not found")
        if not isinstance(items[0], dict):
            raise EvidencePartialError(f"docker {kind} inspect returned {type(items[0])}")
        return items[0]

    def get_image_info(self, name: str) -> Evidence:
        data = self._inspect("image", name)
        if "Id" not in data:
            raise EvidencePartialError(f"docker image {name} inspect lacked Id")
        return build_evidence(
            source=EvidenceSource.DOCKER,
            kind=EvidenceKind.IMAGE_INFO,
            service=name,
            summary=f"image {name}: id {str(data['Id'])[:19]}",
            payload={
                "name": name,
                "id": data.get("Id"),
                "repo_tags": data.get("RepoTags") or [],
                "repo_digests": data.get("RepoDigests") or [],
                "size": data.get("Size"),
                "created": data.get("Created"),
            },
            origin=EvidenceOrigin.LIVE,
        )

    def list_images(self) -> Evidence:
        out = self._exec("image", "ls", "--format", "{{json .}}")
        images = [json.loads(line) for line in out.splitlines() if line.strip()]
        items = [
            {
                "repository": i.get("Repository"),
                "tag": i.get("Tag"),
                "id": i.get("ID"),
                "size": i.get("Size"),
                "created": i.get("CreatedAt") or i.get("CreatedSince"),
            }
            for i in images
        ]
        return build_evidence(
            source=EvidenceSource.DOCKER,
            kind=EvidenceKind.IMAGE_INFO,
            service="docker-daemon",
            summary=f"{len(items)} image(s) on host docker daemon",
            payload={"items": items},
            origin=EvidenceOrigin.LIVE,
            total=len(items),
        )

    def get_container_state(self, name: str) -> Evidence:
        data = self._inspect("container", name)
        state = data.get("State")
        if not isinstance(state, dict):
            raise EvidencePartialError(f"docker container {name} inspect lacked State")
        return build_evidence(
            source=EvidenceSource.DOCKER,
            kind=EvidenceKind.CONTAINER_STATE,
            service=name,
            summary=f"container {name}: {state.get('Status')}",
            payload={
                "name": (data.get("Name") or "").lstrip("/"),
                "id": data.get("Id"),
                "status": state.get("Status"),
                "running": state.get("Running"),
                "exit_code": state.get("ExitCode"),
                "started_at": state.get("StartedAt"),
                "finished_at": state.get("FinishedAt"),
                "image": (data.get("Config") or {}).get("Image"),
            },
            origin=EvidenceOrigin.LIVE,
        )

    def list_containers(self, *, all_containers: bool = True) -> Evidence:
        args = ["ps", "--format", "{{json .}}"]
        if all_containers:
            args.insert(1, "-a")
        out = self._exec(*args)
        containers = [json.loads(line) for line in out.splitlines() if line.strip()]
        items = [
            {
                "name": c.get("Names"),
                "id": c.get("ID"),
                "image": c.get("Image"),
                "status": c.get("Status"),
                "state": c.get("State"),
            }
            for c in containers
        ]
        return build_evidence(
            source=EvidenceSource.DOCKER,
            kind=EvidenceKind.CONTAINER_STATE,
            service="docker-daemon",
            summary=f"{len(items)} container(s) on host docker daemon",
            payload={"items": items, "all": all_containers},
            origin=EvidenceOrigin.LIVE,
            total=len(items),
        )
