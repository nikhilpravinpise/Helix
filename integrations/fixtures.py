"""Fixture-backed evidence capabilities and the store that loads them.

`FixtureStore` loads `fixtures/faults/**/*.json` - the recorded captures
from the 1.2 sandbox. Each `Fixture*` class implements one capability
protocol over a single loaded fixture document, producing the same
normalized Evidence shape as the live client (the k8s/elk normalizers are
shared with the live clients by import). Fixture evidence is stamped
`origin=fixture` so provenance can always distinguish replay from live.

Fixture conventions:
- cluster fixtures: `pods_<service>` (service with - as _), plus
  `pods_*_after` suffix variants, `events`, `logs_<service>`,
  `secret_<name>` (a raw `kubectl get secret -o yaml` object)
- ci fixtures: `console_output` plus top-level `jenkins_*` fields
- github/docker artifacts live under `evidence.github.<artifact>` /
  `evidence.docker.<artifact>` when a fixture carries them; absent keys
  raise EvidenceNotFoundError rather than fabricating evidence
"""

import json
from pathlib import Path
from typing import Any

from helix.config import Settings, get_settings
from integrations.elk.client import MAX_HITS
from integrations.evidence import (
    Evidence,
    EvidenceError,
    EvidenceKind,
    EvidenceNotFoundError,
    EvidenceOrigin,
    EvidenceSource,
    build_evidence,
)
from integrations.jenkins.client import stage_names_from_console
from integrations.kubernetes.client import (
    normalize_deployment,
    normalize_event,
    normalize_pod,
    normalize_secret,
    summarize_pods,
)

FIXTURES_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "faults"


def _service_key(service: str) -> str:
    return service.replace("-", "_")


class FixtureStore:
    """Loads recorded fault fixtures from disk, keyed by fault name."""

    def __init__(self, root: Path | str | None = None) -> None:
        self._root = Path(root) if root is not None else FIXTURES_ROOT

    def names(self) -> list[str]:
        """All available fixtures as layer-qualified names (e.g.
        `cluster/crashloop`), sorted - bare stems can collide across
        layers (`dependency-failure` exists under both ci/ and cluster/)."""
        return sorted(
            p.relative_to(self._root).with_suffix("").as_posix() for p in self._root.rglob("*.json")
        )

    def load(self, name: str) -> dict:
        """Load one fixture document. `name` may be layer-qualified
        (`cluster/crashloop`) or a bare fault name; a bare name matching
        more than one layer is an error, not a coin flip."""
        direct = self._root / f"{name}.json"
        if direct.exists():
            return json.loads(direct.read_text())
        matches = sorted(self._root.rglob(f"{name}.json"))
        if len(matches) > 1:
            raise EvidenceError(
                f"fixture name {name} is ambiguous ({', '.join(str(m) for m in matches)})"
                f" - qualify it as <layer>/{name}"
            )
        if not matches:
            raise EvidenceNotFoundError(f"no fixture named {name} under {self._root}")
        return json.loads(matches[0].read_text())

    def resolve(self, ref: str) -> dict:
        """Resolve `ref` as a filesystem path when it is one, else as a
        fault name under the fixtures root."""
        path = Path(ref)
        if path.suffix == ".json" or path.exists():
            if not path.exists():
                raise EvidenceNotFoundError(f"fixture file {ref} does not exist")
            return json.loads(path.read_text())
        return self.load(ref)


def selected_fixture(settings: Settings) -> dict:
    """The one fixture document a fixture-mode run replays
    (HELIX_FIXTURE_PATH: a JSON path or a fault name)."""
    if not settings.fixture_path:
        raise EvidenceError(
            "HELIX_FIXTURE_PATH must name a fixture file or fault when "
            "HELIX_EVIDENCE_SOURCE=fixtures"
        )
    return FixtureStore().resolve(settings.fixture_path)


class FixtureKubernetesEvidence:
    """Kubernetes capability over one fixture document's `evidence` map."""

    def __init__(self, fixture: dict) -> None:
        self._fixture = fixture
        self._evidence: dict = fixture.get("evidence") or {}

    def _pod_doc(self, service: str | None) -> dict | None:
        """The pods_* kubectl List doc for `service` - exact key first,
        then a prefix match (captures `pods_payment_api_after` variants)."""
        if service:
            key = f"pods_{_service_key(service)}"
            if isinstance(self._evidence.get(key), dict):
                return self._evidence[key]
            for k in sorted(self._evidence):
                if k.startswith(key) and isinstance(self._evidence[k], dict):
                    return self._evidence[k]
            return None
        merged: dict = {"items": []}
        for k in sorted(self._evidence):
            doc = self._evidence[k]
            if k.startswith("pods_") and isinstance(doc, dict):
                merged["items"].extend(doc.get("items") or [])
        return merged if merged["items"] else None

    def get_pods(
        self,
        namespace: str,
        *,
        service: str | None = None,
        label_selector: str | None = None,
    ) -> Evidence:
        if service is None and label_selector and label_selector.startswith("app="):
            service = label_selector[4:]
        doc = self._pod_doc(service)
        if doc is None:
            raise EvidenceNotFoundError(
                f"fixture {self._fixture.get('fault')} has no pods_* evidence for {service}"
            )
        pods = [normalize_pod(p) for p in doc.get("items") or []]
        name = service or namespace
        return build_evidence(
            source=EvidenceSource.KUBERNETES,
            kind=EvidenceKind.POD_LIST,
            service=name,
            summary=summarize_pods(pods, namespace),
            payload={"namespace": namespace, "selector": service, "items": pods},
            origin=EvidenceOrigin.FIXTURE,
            total=len(pods),
        )

    def get_pod_status(self, namespace: str, pod_name: str) -> Evidence:
        for k in sorted(self._evidence):
            doc = self._evidence[k]
            if not (k.startswith("pods_") and isinstance(doc, dict)):
                continue
            for item in doc.get("items") or []:
                if (item.get("metadata") or {}).get("name") == pod_name:
                    pod = normalize_pod(item)
                    return build_evidence(
                        source=EvidenceSource.KUBERNETES,
                        kind=EvidenceKind.POD_STATUS,
                        service=pod["name"] or pod_name,
                        summary=f"pod {pod_name}: {pod['phase']}",
                        payload=pod,
                        origin=EvidenceOrigin.FIXTURE,
                    )
        raise EvidenceNotFoundError(f"fixture has no pod {pod_name}")

    def get_deployment_status(self, namespace: str, name: str) -> Evidence:
        doc = self._evidence.get(f"deployments_{_service_key(name)}") or self._evidence.get(
            "deployments"
        )
        if not isinstance(doc, dict):
            raise EvidenceNotFoundError(
                f"fixture {self._fixture.get('fault')} has no deployment evidence for {name}"
            )
        dep = normalize_deployment(doc)
        return build_evidence(
            source=EvidenceSource.KUBERNETES,
            kind=EvidenceKind.POD_STATUS,
            service=name,
            summary=f"deployment {name}: {dep['ready_replicas'] or 0}/{dep['replicas']} ready",
            payload=dep,
            origin=EvidenceOrigin.FIXTURE,
        )

    def get_events(self, namespace: str, *, involved_object: str | None = None) -> Evidence:
        doc = self._evidence.get("events")
        if not isinstance(doc, dict):
            raise EvidenceNotFoundError(
                f"fixture {self._fixture.get('fault')} has no events evidence"
            )
        events = [normalize_event(e) for e in doc.get("items") or []]
        if involved_object is not None:
            events = [e for e in events if e["involved_name"] == involved_object]
        events.sort(key=lambda e: e["last_timestamp"] or "")
        return build_evidence(
            source=EvidenceSource.KUBERNETES,
            kind=EvidenceKind.EVENT_LIST,
            service=involved_object or namespace,
            summary=f"{len(events)} event(s) in namespace {namespace}",
            payload={
                "namespace": namespace,
                "involved_object": involved_object,
                "items": events,
            },
            origin=EvidenceOrigin.FIXTURE,
            total=len(events),
        )

    def get_secret_info(self, namespace: str, name: str) -> Evidence:
        for k in sorted(self._evidence):
            doc = self._evidence[k]
            if not (k.startswith("secret_") and isinstance(doc, dict)):
                continue
            meta = doc.get("metadata") or {}
            if meta.get("name") == name:
                info = normalize_secret(doc)
                return build_evidence(
                    source=EvidenceSource.KUBERNETES,
                    kind=EvidenceKind.SECRET_INFO,
                    service=name,
                    summary=(f"secret {name} in {info['namespace']}: {info['key_count']} key(s)"),
                    payload=info,
                    origin=EvidenceOrigin.FIXTURE,
                )
        raise EvidenceNotFoundError(f"fixture has no secret {name}")


class FixtureElkEvidence:
    """ELK capability over a fixture's `logs_<service>` line lists."""

    def __init__(self, fixture: dict) -> None:
        self._fixture = fixture
        self._evidence: dict = fixture.get("evidence") or {}

    def get_logs(self, service: str, window: str, *, level: str | None = None) -> Evidence:
        key = f"logs_{_service_key(service)}"
        lines = self._evidence.get(key)
        if lines is None:
            raise EvidenceNotFoundError(
                f"fixture {self._fixture.get('fault')} has no {key} evidence"
            )
        if level:
            lines = [line for line in lines if level.lower() in line.lower()]
        total = len(lines)
        shown = lines[:MAX_HITS]
        truncated = total > len(shown)
        return build_evidence(
            source=EvidenceSource.ELK,
            kind=EvidenceKind.LOG_LINES,
            service=service,
            summary=f"{len(shown)} log line(s) for {service} from fixture",
            payload={
                "service": service,
                "window": window,
                "level": level,
                "lines": shown,
            },
            truncated=truncated,
            total=total,
            origin=EvidenceOrigin.FIXTURE,
        )


class FixtureJenkinsEvidence:
    """Jenkins capability over a CI fixture: `console_output` plus the
    top-level `jenkins_*` fields captured beside it."""

    def __init__(self, fixture: dict) -> None:
        self._fixture = fixture
        self._evidence: dict = fixture.get("evidence") or {}

    def get_build_status(self, job: str, build_number: int) -> Evidence:
        if "jenkins_result" not in self._fixture:
            raise EvidenceNotFoundError(
                f"fixture {self._fixture.get('fault')} has no jenkins build fields"
            )
        console = self._evidence.get("console_output") or ""
        stages = [{"name": n, "status": None} for n in stage_names_from_console(console)]
        result = self._fixture["jenkins_result"]
        return build_evidence(
            source=EvidenceSource.JENKINS,
            kind=EvidenceKind.BUILD_CONSOLE,
            service=job,
            summary=f"build {job}#{build_number}: {result} ({len(stages)} stage(s))",
            payload={
                "job": self._fixture.get("jenkins_job", job),
                "number": self._fixture.get("jenkins_build_number", build_number),
                "result": result,
                "url": self._fixture.get("jenkins_build_url"),
                "stages": stages,
            },
            origin=EvidenceOrigin.FIXTURE,
        )

    def get_console_log(self, job: str, build_number: int) -> Evidence:
        console = self._evidence.get("console_output")
        if console is None:
            raise EvidenceNotFoundError(
                f"fixture {self._fixture.get('fault')} has no console_output"
            )
        lines = console.splitlines()
        return build_evidence(
            source=EvidenceSource.JENKINS,
            kind=EvidenceKind.BUILD_CONSOLE,
            service=job,
            summary=f"console log for {job}#{build_number}: {len(lines)} line(s)",
            payload={"job": job, "number": build_number, "lines": lines},
            origin=EvidenceOrigin.FIXTURE,
            total=len(lines),
        )

    def get_test_results(self, job: str, build_number: int) -> Evidence:
        report = self._evidence.get("test_report")
        if not isinstance(report, dict):
            raise EvidenceNotFoundError(
                f"fixture {self._fixture.get('fault')} has no test_report evidence"
            )
        cases = [c for s in report.get("suites") or [] for c in s.get("cases") or []]
        failures = [
            {"name": c.get("name"), "class_name": c.get("className"), "status": c.get("status")}
            for c in cases
            if c.get("status") in ("FAILED", "REGRESSION")
        ]
        total = report.get("totalCount", len(cases))
        return build_evidence(
            source=EvidenceSource.JENKINS,
            kind=EvidenceKind.TEST_RESULTS,
            service=job,
            summary=f"tests {job}#{build_number}: {total} total, {len(failures)} failed",
            payload={
                "job": job,
                "number": build_number,
                "total": total,
                "failed": report.get("failCount", len(failures)),
                "skipped": report.get("skipCount", 0),
                "failures": failures,
            },
            origin=EvidenceOrigin.FIXTURE,
            total=total,
        )


class FixtureGitHubEvidence:
    """GitHub capability over `evidence.github.<artifact>` keys. A fixture
    that does not carry the requested artifact raises
    EvidenceNotFoundError - replaying is honest, never fabricated."""

    def __init__(self, fixture: dict) -> None:
        self._fixture = fixture
        self._github: dict = (fixture.get("evidence") or {}).get("github") or {}
        self._repo = get_settings().github_repo

    def _artifact(self, key: str) -> Any:
        if key not in self._github:
            raise EvidenceNotFoundError(
                f"fixture {self._fixture.get('fault')} has no github.{key} evidence"
            )
        return self._github[key]

    def _wrap(self, kind: EvidenceKind, summary: str, payload: dict) -> Evidence:
        return build_evidence(
            source=EvidenceSource.GITHUB,
            kind=kind,
            service=self._repo,
            summary=summary,
            payload=payload,
            origin=EvidenceOrigin.FIXTURE,
        )

    def get_commit(self, sha: str) -> Evidence:
        return self._wrap(
            EvidenceKind.COMMIT, f"commit {sha[:8]} (fixture)", self._artifact("commit")
        )

    def get_diff(self, sha: str) -> Evidence:
        return self._wrap(
            EvidenceKind.DIFF, f"diff for {sha[:8]} (fixture)", self._artifact("diff")
        )

    def get_pull_request(self, number: int) -> Evidence:
        return self._wrap(
            EvidenceKind.PR_INFO, f"PR #{number} (fixture)", self._artifact("pr_info")
        )

    def get_pr_reviews(self, number: int) -> Evidence:
        return self._wrap(
            EvidenceKind.PR_REVIEW,
            f"reviews on PR #{number} (fixture)",
            self._artifact("pr_reviews"),
        )

    def get_check_runs(self, ref: str) -> Evidence:
        return self._wrap(
            EvidenceKind.CHECK_RUN,
            f"check runs on {ref} (fixture)",
            self._artifact("check_runs"),
        )

    def get_branch_state(self, branch: str, *, base: str | None = None) -> Evidence:
        return self._wrap(
            EvidenceKind.BRANCH_STATE,
            f"branch {branch} (fixture)",
            self._artifact("branch_state"),
        )


class FixtureDockerEvidence:
    """Docker capability over `evidence.docker.<artifact>` keys; absent
    artifacts raise EvidenceNotFoundError like the GitHub impl."""

    def __init__(self, fixture: dict) -> None:
        self._fixture = fixture
        self._docker: dict = (fixture.get("evidence") or {}).get("docker") or {}

    def _artifact(self, key: str) -> Any:
        if key not in self._docker:
            raise EvidenceNotFoundError(
                f"fixture {self._fixture.get('fault')} has no docker.{key} evidence"
            )
        return self._docker[key]

    def _wrap(self, kind: EvidenceKind, service: str, summary: str, payload: Any) -> Evidence:
        if isinstance(payload, list):
            payload = {"items": payload}
        return build_evidence(
            source=EvidenceSource.DOCKER,
            kind=kind,
            service=service,
            summary=summary,
            payload=payload,
            origin=EvidenceOrigin.FIXTURE,
        )

    def get_image_info(self, name: str) -> Evidence:
        return self._wrap(
            EvidenceKind.IMAGE_INFO, name, f"image {name} (fixture)", self._artifact("image_info")
        )

    def list_images(self) -> Evidence:
        return self._wrap(
            EvidenceKind.IMAGE_INFO, "docker-daemon", "images (fixture)", self._artifact("images")
        )

    def get_container_state(self, name: str) -> Evidence:
        return self._wrap(
            EvidenceKind.CONTAINER_STATE,
            name,
            f"container {name} (fixture)",
            self._artifact("container_state"),
        )

    def list_containers(self, *, all_containers: bool = True) -> Evidence:
        return self._wrap(
            EvidenceKind.CONTAINER_STATE,
            "docker-daemon",
            "containers (fixture)",
            self._artifact("containers"),
        )
