"""Read-only evidence capability interfaces, in Helix vocabulary.

Each protocol is one vendor domain. Agents depend only on these Protocol
classes and on `Evidence` - never on a vendor SDK object (C8). Methods
return normalized `Evidence` produced by `build_evidence`, so redaction is
already applied by the time a caller sees anything.

The `get_*_evidence()` factory functions select the live client or the
fixture-backed implementation from `HELIX_EVIDENCE_SOURCE` (`live` /
`fixtures`). Fixture mode replays the single fixture named by
`HELIX_FIXTURE_PATH` - the benchmark harness sets it per scenario.
"""

from typing import TYPE_CHECKING, Protocol

from helix.config import get_settings
from integrations.evidence import Evidence

if TYPE_CHECKING:
    from integrations.kubernetes.prechecks import PrecheckOutcome


class KubernetesEvidence(Protocol):
    """Cluster state: pods, workload rollout status, events, Secret metadata."""

    def get_pods(
        self,
        namespace: str,
        *,
        service: str | None = None,
        label_selector: str | None = None,
    ) -> Evidence:
        """`pod_list` evidence: normalized status for pods matching the
        selector (`service` maps to `app=<service>`)."""
        ...

    def get_pod_status(self, namespace: str, pod_name: str) -> Evidence:
        """`pod_status` evidence for one named pod."""
        ...

    def get_deployment_status(self, namespace: str, name: str) -> Evidence:
        """`pod_status` evidence aggregating a Deployment's pod state
        (replicas, conditions such as ProgressDeadlineExceeded)."""
        ...

    def get_events(self, namespace: str, *, involved_object: str | None = None) -> Evidence:
        """`event_list` evidence for a namespace, optionally filtered to one
        involved object name."""
        ...

    def get_secret_info(self, namespace: str, name: str) -> Evidence:
        """`secret_info` evidence: Secret name/namespace/key names and
        per-key value presence - never values (D4)."""
        ...


class KubernetesPrechecks(Protocol):
    """Read-side admission and authorization checks for the Validation
    Agent (1.7). Nothing here mutates the cluster: `can_i` is a
    SelfSubjectAccessReview query and `dry_run_apply` is a server-side
    apply with dryRun=All, which persists nothing.

    Contract: a PrecheckOutcome is returned when the cluster answered
    (allowed/denied, accepted/rejected); a check that could not run
    raises an EvidenceError - callers must never map "could not check"
    onto pass.
    """

    def can_i(
        self,
        verb: str,
        resource: str,
        namespace: str,
        group: str = "",
    ) -> "PrecheckOutcome":
        """Whether the configured credentials may perform `verb` on
        `resource` (optionally "resource/subresource") in `namespace`."""
        ...

    def dry_run_apply(self, manifest_text: str) -> "PrecheckOutcome":
        """`kubectl apply --dry-run=server` semantics: schema validation
        plus admission on the parsed manifest, persisting nothing."""
        ...


class JenkinsEvidence(Protocol):
    """CI state: build metadata and stages, console log, test results."""

    def get_build_status(self, job: str, build_number: int) -> Evidence:
        """`build_console` evidence: build result plus pipeline stage
        outcomes."""
        ...

    def get_console_log(self, job: str, build_number: int) -> Evidence:
        """`build_console` evidence: the build's console output as lines."""
        ...

    def get_test_results(self, job: str, build_number: int) -> Evidence:
        """`test_results` evidence: counts plus failing test details."""
        ...

    def get_job(self, job: str) -> Evidence:
        """`job_info` evidence: the job's existence plus its last build
        number - what `rerun_pipeline` validation checks (1.7)."""
        ...


class ElkEvidence(Protocol):
    """Bounded log queries against Elasticsearch - the same query shape as
    scripts/query-logs.sh, but returning explicit truncated/total."""

    def get_logs(self, service: str, window: str, *, level: str | None = None) -> Evidence:
        """`log_lines` evidence: `<timestamp> [<pod>] <message>` lines,
        oldest first. `window` is an ES date-math offset (15m, 1h, 24h);
        `level` is a substring match on the message. Capped at 200 lines;
        when capped, `truncated` is True and `total` reports the real hit
        count."""
        ...


class GitHubEvidence(Protocol):
    """Repository state: commits, diffs, PRs, reviews, checks, branches."""

    def get_commit(self, sha: str) -> Evidence:
        """`commit` evidence: message, author, parentage."""
        ...

    def get_diff(self, sha: str) -> Evidence:
        """`diff` evidence: per-file status/additions/deletions/patch."""
        ...

    def get_pull_request(self, number: int) -> Evidence:
        """`pr_info` evidence: PR state, head/base, mergeable."""
        ...

    def get_pr_reviews(self, number: int) -> Evidence:
        """`pr_review` evidence: review states and bodies."""
        ...

    def get_check_runs(self, ref: str) -> Evidence:
        """`check_run` evidence: check conclusions for a commit/ref."""
        ...

    def get_branch_state(self, branch: str, *, base: str | None = None) -> Evidence:
        """`branch_state` evidence: head sha, protection, ahead/behind vs
        `base` when given."""
        ...


class DockerEvidence(Protocol):
    """Host Docker daemon state only. The host daemon cannot see
    `payment-api` pod containers - those run in containerd inside the kind
    node. This capability covers host images (e.g. payment-api:kind) and
    host containers (kind node, Jenkins, ES)."""

    def get_image_info(self, name: str) -> Evidence:
        """`image_info` evidence for one image: id, digests, tags, size."""
        ...

    def list_images(self) -> Evidence:
        """`image_info` evidence listing every image on the host daemon."""
        ...

    def get_container_state(self, name: str) -> Evidence:
        """`container_state` evidence for one host container."""
        ...

    def list_containers(self, *, all_containers: bool = True) -> Evidence:
        """`container_state` evidence listing host containers."""
        ...


def _selected_fixture() -> dict:
    """Load the fixture document `HELIX_FIXTURE_PATH` points at."""
    from integrations.fixtures import selected_fixture

    return selected_fixture(get_settings())


def get_kubernetes_evidence() -> KubernetesEvidence:
    """Return the live or fixture Kubernetes evidence capability."""
    if get_settings().evidence_source == "fixtures":
        from integrations.fixtures import FixtureKubernetesEvidence

        return FixtureKubernetesEvidence(_selected_fixture())
    from integrations.kubernetes.client import KubernetesEvidenceClient

    return KubernetesEvidenceClient()


def get_jenkins_evidence() -> JenkinsEvidence:
    """Return the live or fixture Jenkins evidence capability."""
    if get_settings().evidence_source == "fixtures":
        from integrations.fixtures import FixtureJenkinsEvidence

        return FixtureJenkinsEvidence(_selected_fixture())
    from integrations.jenkins.client import JenkinsEvidenceClient

    return JenkinsEvidenceClient()


def get_elk_evidence() -> ElkEvidence:
    """Return the live or fixture ELK evidence capability."""
    if get_settings().evidence_source == "fixtures":
        from integrations.fixtures import FixtureElkEvidence

        return FixtureElkEvidence(_selected_fixture())
    from integrations.elk.client import ElkEvidenceClient

    return ElkEvidenceClient()


def get_github_evidence() -> GitHubEvidence:
    """Return the live or fixture GitHub evidence capability."""
    if get_settings().evidence_source == "fixtures":
        from integrations.fixtures import FixtureGitHubEvidence

        return FixtureGitHubEvidence(_selected_fixture())
    from integrations.github.client import GitHubEvidenceClient

    return GitHubEvidenceClient()


def get_docker_evidence() -> DockerEvidence:
    """Return the live or fixture Docker evidence capability."""
    if get_settings().evidence_source == "fixtures":
        from integrations.fixtures import FixtureDockerEvidence

        return FixtureDockerEvidence(_selected_fixture())
    from integrations.docker.client import DockerEvidenceClient

    return DockerEvidenceClient()


def get_kubernetes_prechecks() -> KubernetesPrechecks:
    """Return the live or fixture Kubernetes prechecks capability (1.7).
    The fixture impl returns canned `ok=True` outcomes so fixture-mode
    runs still exercise the validation dispatch."""
    if get_settings().evidence_source == "fixtures":
        from integrations.fixtures import FixtureKubernetesPrechecks

        return FixtureKubernetesPrechecks()
    from integrations.kubernetes.prechecks import KubernetesPrechecksClient

    return KubernetesPrechecksClient()
