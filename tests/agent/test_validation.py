"""Validation Agent tests (subphase 1.7).

Unit tier (D9): strategies run against stub evidence/precheck objects -
no live infrastructure. Graph tests drive the full
intake -> diagnosis -> plan -> validate loop on in-memory SQLite with a
scripted FakeLLM and fixture/fake prechecks.

Covered:

- One deliberately wrong candidate per family -> FAIL (missing manifest
  keys in open_fix_pr, a rejected dry-run for reapply_manifest, a
  nonexistent deployment for the kubernetes actions), producing
  REMEDIATION_PENDING with the failed fingerprint excluded from the
  replan.
- rerun_pipeline with no builds -> NOT_VALIDATABLE with a reason.
- Registry completeness: every catalogue action has a validator wired.
- Second consecutive FAIL -> ESCALATED (the single retry is exhausted).
- Fixture prechecks return canned PASS; live can_i/dry_run are covered
  integration-marked against a real cluster.
"""

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest
from fakes import JENKINS_SIGNAL, K8S_SIGNAL, FakeLLM, make_deps
from sqlalchemy import select

from agent.actions.catalogue import (
    ActionName,
    FileChange,
    OpenFixPrParams,
    all_actions,
    get_action,
)
from agent.agents.diagnosis import EvidencePlan
from agent.agents.planner import CandidateList, CandidateProposal
from agent.agents.validation import validation_node, validator_for
from agent.runner import run_incident
from agent.state import Hypothesis, WorkflowState
from agent.validation import ValidationContext, candidate_fingerprint
from agent.validation.deployments import validate_kubernetes_target
from agent.validation.fix_pr import validate_fix_content
from agent.validation.manifests import validate_manifest_dry_run
from agent.validation.pipeline import validate_pipeline_exists
from integrations.evidence import (
    Evidence,
    EvidenceError,
    EvidenceKind,
    EvidenceNotFoundError,
    EvidenceOrigin,
    EvidenceSource,
    EvidenceTimeoutError,
)
from integrations.fixtures import FixtureJenkinsEvidence, FixtureKubernetesPrechecks
from integrations.kubernetes.prechecks import KubernetesPrechecksClient, PrecheckOutcome
from state import models
from state.intake import intake_signal
from state.machine import IncidentStatus
from state.models import ValidationOutcome

REPO_ROOT = Path(__file__).resolve().parents[2]

K8S = {"deployment": "payment-api", "namespace": "payment-api"}
DEPLOYMENT_PATH = "sandbox/payment-api/k8s/04-deployment.yaml"
SECRET_PATH = "sandbox/payment-api/k8s/03-secret.yaml"

BAD_MANIFEST = "apiVersion: apps/v1\nspec:\n  replicas: 1\n"  # no kind, no metadata


def _params(action: str, params: dict):
    """Validated params for `action` - the shape a planner candidate
    carries into the validator."""
    spec = get_action(action)
    assert spec is not None
    return spec, spec.params_model.model_validate(params)


def _pr_params(files: list[dict[str, str]]):
    """Validated open_fix_pr params carrying `files` on a helix/INC-*
    branch."""
    return _params(
        "open_fix_pr",
        {
            "branch": "helix/INC-ab12cd34",
            "files": files,
            "title": "helix: remediation",
            "body": "",
        },
    )


def _ctx(kubernetes=None, jenkins=None, prechecks=None, repo_root=REPO_ROOT):
    return ValidationContext(
        kubernetes=kubernetes or _StubKubernetes(),
        jenkins=jenkins or _StubJenkins(),
        prechecks=prechecks or _StubPrechecks(),
        repo_root=repo_root,
    )


def _job_evidence(last_build: int | None) -> Evidence:
    return Evidence(
        source=EvidenceSource.JENKINS,
        kind=EvidenceKind.JOB_INFO,
        service="payment-api",
        summary="job payment-api (stub)",
        payload={"job": "payment-api", "last_build": last_build},
        origin=EvidenceOrigin.FIXTURE,
    )


def _deployment_evidence() -> Evidence:
    return Evidence(
        source=EvidenceSource.KUBERNETES,
        kind=EvidenceKind.POD_STATUS,
        service="payment-api",
        summary="deployment payment-api: 1/1 ready",
        payload={"name": "payment-api", "namespace": "payment-api"},
        origin=EvidenceOrigin.FIXTURE,
    )


class _StubKubernetes:
    """KubernetesEvidence stub: `exists` toggles whether the deployment
    lookup finds its target; `error` makes the lookup itself fail."""

    def __init__(self, exists: bool = True, error: Exception | None = None) -> None:
        self._exists = exists
        self._error = error

    def get_deployment_status(self, namespace: str, name: str) -> Evidence:
        if self._error is not None:
            raise self._error
        if not self._exists:
            raise EvidenceNotFoundError(f"no deployment {name} in {namespace}")
        return _deployment_evidence()


class _StubJenkins:
    """JenkinsEvidence stub: `exists`/`last_build` shape the job lookup;
    `error` makes the lookup itself fail."""

    def __init__(
        self,
        exists: bool = True,
        last_build: int | None = 42,
        error: Exception | None = None,
    ) -> None:
        self._exists = exists
        self._last_build = last_build
        self._error = error

    def get_job(self, job: str) -> Evidence:
        if self._error is not None:
            raise self._error
        if not self._exists:
            raise EvidenceNotFoundError(f"no job {job}")
        return _job_evidence(self._last_build)


class _StubPrechecks:
    """KubernetesPrechecks stub recording calls and returning scripted
    verdicts; `can_i_error`/`dry_run_error` make the check itself fail."""

    def __init__(
        self,
        can_i_ok: bool = True,
        can_i_detail: str = "allowed by RBAC",
        dry_run_ok: bool = True,
        dry_run_detail: str = "accepted by dry-run",
        can_i_error: Exception | None = None,
        dry_run_error: Exception | None = None,
    ) -> None:
        self._can_i_ok = can_i_ok
        self._can_i_detail = can_i_detail
        self._dry_run_ok = dry_run_ok
        self._dry_run_detail = dry_run_detail
        self._can_i_error = can_i_error
        self._dry_run_error = dry_run_error
        self.can_i_calls: list[tuple] = []
        self.dry_run_calls: list[str] = []

    def can_i(self, verb: str, resource: str, namespace: str, group: str = ""):
        self.can_i_calls.append((verb, resource, namespace, group))
        if self._can_i_error is not None:
            raise self._can_i_error
        return PrecheckOutcome(ok=self._can_i_ok, detail=self._can_i_detail)

    def dry_run_apply(self, manifest_text: str) -> PrecheckOutcome:
        self.dry_run_calls.append(manifest_text)
        if self._dry_run_error is not None:
            raise self._dry_run_error
        return PrecheckOutcome(ok=self._dry_run_ok, detail=self._dry_run_detail)


# --- Strategy unit tests ---------------------------------------------------


class TestKubernetesTarget:
    """restart/rollback/scale: target must exist + the needed verb must
    be authorized."""

    @pytest.mark.parametrize(
        "action",
        [
            ActionName.RESTART_DEPLOYMENT.value,
            ActionName.ROLLBACK_DEPLOYMENT.value,
            ActionName.SCALE_DEPLOYMENT.value,
        ],
    )
    def test_existing_target_and_permitted_verb_passes(self, action):
        params_dict = dict(K8S)
        if action == ActionName.SCALE_DEPLOYMENT.value:
            params_dict["replicas"] = 1
        spec, params = _params(action, params_dict)
        prechecks = _StubPrechecks()

        result = validate_kubernetes_target(spec, params, _ctx(prechecks=prechecks))

        assert result.outcome == ValidationOutcome.PASS
        assert len(prechecks.can_i_calls) == 1

    def test_scale_checks_the_scale_subresource(self):
        spec, params = _params("scale_deployment", {**K8S, "replicas": 1})
        prechecks = _StubPrechecks()

        validate_kubernetes_target(spec, params, _ctx(prechecks=prechecks))

        assert prechecks.can_i_calls == [("update", "deployments/scale", "payment-api", "apps")]

    def test_nonexistent_deployment_fails(self):
        spec, params = _params("restart_deployment", K8S)
        result = validate_kubernetes_target(
            spec, params, _ctx(kubernetes=_StubKubernetes(exists=False))
        )
        assert result.outcome == ValidationOutcome.FAIL
        assert "does not exist" in result.reason

    def test_denied_verb_fails(self):
        spec, params = _params("rollback_deployment", K8S)
        prechecks = _StubPrechecks(can_i_ok=False, can_i_detail="RBAC: access denied")
        result = validate_kubernetes_target(spec, params, _ctx(prechecks=prechecks))
        assert result.outcome == ValidationOutcome.FAIL
        assert "not authorized" in result.reason
        assert "RBAC: access denied" in result.reason

    def test_lookup_error_is_not_validatable(self):
        spec, params = _params("restart_deployment", K8S)
        result = validate_kubernetes_target(
            spec,
            params,
            _ctx(kubernetes=_StubKubernetes(error=EvidenceTimeoutError("cluster gone"))),
        )
        assert result.outcome == ValidationOutcome.NOT_VALIDATABLE
        assert "could not verify" in result.reason

    def test_can_i_error_is_not_validatable(self):
        spec, params = _params("restart_deployment", K8S)
        prechecks = _StubPrechecks(can_i_error=EvidenceError("no kubeconfig"))
        result = validate_kubernetes_target(spec, params, _ctx(prechecks=prechecks))
        assert result.outcome == ValidationOutcome.NOT_VALIDATABLE
        assert "authorization check" in result.reason


class TestManifestDryRun:
    """reapply_manifest: allowlist + parse + required keys + server
    dry-run."""

    def test_committed_manifest_passes(self):
        spec, params = _params("reapply_manifest", {"path": DEPLOYMENT_PATH})
        result = validate_manifest_dry_run(spec, params, _ctx())
        assert result.outcome == ValidationOutcome.PASS

    def test_dry_run_rejection_fails(self):
        spec, params = _params("reapply_manifest", {"path": DEPLOYMENT_PATH})
        prechecks = _StubPrechecks(dry_run_ok=False, dry_run_detail="field is immutable")
        result = validate_manifest_dry_run(spec, params, _ctx(prechecks=prechecks))
        assert result.outcome == ValidationOutcome.FAIL
        assert "field is immutable" in result.reason

    def test_unparseable_manifest_fails(self, tmp_path):
        bad = tmp_path / DEPLOYMENT_PATH
        bad.parent.mkdir(parents=True)
        bad.write_text("apiVersion: [unclosed\n")
        spec, params = _params("reapply_manifest", {"path": DEPLOYMENT_PATH})
        result = validate_manifest_dry_run(spec, params, _ctx(repo_root=tmp_path))
        assert result.outcome == ValidationOutcome.FAIL
        assert "does not parse" in result.reason

    def test_missing_required_keys_fails(self, tmp_path):
        bad = tmp_path / DEPLOYMENT_PATH
        bad.parent.mkdir(parents=True)
        bad.write_text(BAD_MANIFEST)
        spec, params = _params("reapply_manifest", {"path": DEPLOYMENT_PATH})
        result = validate_manifest_dry_run(spec, params, _ctx(repo_root=tmp_path))
        assert result.outcome == ValidationOutcome.FAIL
        assert "missing required keys" in result.reason

    def test_missing_file_fails(self, tmp_path):
        spec, params = _params("reapply_manifest", {"path": DEPLOYMENT_PATH})
        result = validate_manifest_dry_run(spec, params, _ctx(repo_root=tmp_path))
        assert result.outcome == ValidationOutcome.FAIL
        assert "does not exist" in result.reason

    def test_dry_run_error_is_not_validatable(self):
        spec, params = _params("reapply_manifest", {"path": DEPLOYMENT_PATH})
        prechecks = _StubPrechecks(dry_run_error=EvidenceError("no cluster"))
        result = validate_manifest_dry_run(spec, params, _ctx(prechecks=prechecks))
        assert result.outcome == ValidationOutcome.NOT_VALIDATABLE
        assert "could not run" in result.reason

    def test_allowlist_reasserted(self):
        """The validator alone enforces the allowlist even if a caller
        bypassed params validation."""
        spec = get_action("reapply_manifest")
        assert spec is not None
        params = spec.params_model.model_construct(path="sandbox/payment-api/app/main.py")
        result = validate_manifest_dry_run(spec, params, _ctx())
        assert result.outcome == ValidationOutcome.FAIL
        assert "allowlist" in result.reason


class TestFixContent:
    """open_fix_pr: per-file content checks before any PR opens."""

    def test_good_files_pass(self):
        manifest = (REPO_ROOT / DEPLOYMENT_PATH).read_text()
        spec, params = _pr_params(
            [
                {"path": DEPLOYMENT_PATH, "content": manifest},
                {"path": "sandbox/payment-api/payment_api/main.py", "content": "x = 1\n"},
                {"path": "sandbox/payment-api/Dockerfile", "content": "FROM python:3.12\n"},
            ]
        )
        prechecks = _StubPrechecks()
        result = validate_fix_content(spec, params, _ctx(prechecks=prechecks))
        assert result.outcome == ValidationOutcome.PASS
        # The manifest file went through the dry-run precheck; the python
        # and Dockerfile files were content-checked only.
        assert len(prechecks.dry_run_calls) == 1
        assert len(result.details["checked"]) == 3

    def test_manifest_missing_required_keys_fails(self):
        spec, params = _pr_params([{"path": DEPLOYMENT_PATH, "content": BAD_MANIFEST}])
        result = validate_fix_content(spec, params, _ctx())
        assert result.outcome == ValidationOutcome.FAIL
        assert "missing required keys" in result.reason

    def test_manifest_dry_run_rejection_fails(self):
        spec, params = _pr_params(
            [
                {
                    "path": DEPLOYMENT_PATH,
                    "content": "apiVersion: apps/v1\nkind: Deployment\n"
                    "metadata:\n  name: payment-api\n",
                }
            ]
        )
        prechecks = _StubPrechecks(dry_run_ok=False, dry_run_detail="denied by admission")
        result = validate_fix_content(spec, params, _ctx(prechecks=prechecks))
        assert result.outcome == ValidationOutcome.FAIL
        assert "denied by admission" in result.reason

    def test_python_syntax_error_fails(self):
        spec, params = _pr_params(
            [{"path": "sandbox/payment-api/payment_api/main.py", "content": "def broken(:\n"}]
        )
        result = validate_fix_content(spec, params, _ctx())
        assert result.outcome == ValidationOutcome.FAIL
        assert "does not parse" in result.reason

    def test_unparseable_yaml_fails(self):
        # Under the service root (a real directory) but outside k8s/, so
        # the generic yaml check runs rather than the manifest dry-run.
        spec, params = _pr_params(
            [{"path": "sandbox/payment-api/settings.yaml", "content": "a: [unclosed\n"}]
        )
        result = validate_fix_content(spec, params, _ctx())
        assert result.outcome == ValidationOutcome.FAIL
        assert "does not parse" in result.reason

    def test_phantom_directory_tree_fails(self):
        """The 1.5 ablation's finding: a model that never looked at the
        repo invents trees (src/main/java in a Python project). New files
        are fine; new directory trees under the write root are the
        hallucination signature - the parent must exist."""
        spec, params = _pr_params(
            [
                {
                    "path": "sandbox/payment-api/src/main/java/App.java",
                    "content": "class App {}\n",
                }
            ]
        )
        result = validate_fix_content(spec, params, _ctx())
        assert result.outcome == ValidationOutcome.FAIL
        assert "does not exist" in result.reason

    def test_empty_dockerfile_fails(self):
        spec, params = _pr_params([{"path": "sandbox/payment-api/Dockerfile", "content": "   \n"}])
        result = validate_fix_content(spec, params, _ctx())
        assert result.outcome == ValidationOutcome.FAIL
        assert "Dockerfile" in result.reason

    def test_path_outside_write_root_fails(self):
        """The write-root bound is re-asserted even when a caller bypassed
        params validation (model_construct)."""
        spec = get_action("open_fix_pr")
        assert spec is not None
        params = OpenFixPrParams.model_construct(
            branch="helix/INC-1",
            files=[FileChange.model_construct(path="agent/graph.py", content="x = 1\n")],
            title="t",
            body="",
        )
        result = validate_fix_content(spec, params, _ctx())
        assert result.outcome == ValidationOutcome.FAIL
        assert "write root" in result.reason

    def test_dry_run_error_is_not_validatable(self):
        spec, params = _pr_params(
            [
                {
                    "path": DEPLOYMENT_PATH,
                    "content": "apiVersion: apps/v1\nkind: Deployment\n"
                    "metadata:\n  name: payment-api\n",
                }
            ]
        )
        prechecks = _StubPrechecks(dry_run_error=EvidenceError("no cluster"))
        result = validate_fix_content(spec, params, _ctx(prechecks=prechecks))
        assert result.outcome == ValidationOutcome.NOT_VALIDATABLE
        assert "could not run" in result.reason


class TestPipelineExists:
    """rerun_pipeline: job exists + a previous build exists; honestly
    NOT_VALIDATABLE where nothing meaningful can be checked."""

    def test_job_with_builds_passes(self):
        spec, params = _params("rerun_pipeline", {"job": "payment-api"})
        result = validate_pipeline_exists(spec, params, _ctx())
        assert result.outcome == ValidationOutcome.PASS
        assert "42" in result.reason

    def test_missing_job_fails(self):
        spec, params = _params("rerun_pipeline", {"job": "no-such-job"})
        result = validate_pipeline_exists(spec, params, _ctx(jenkins=_StubJenkins(exists=False)))
        assert result.outcome == ValidationOutcome.FAIL
        assert "does not exist" in result.reason

    def test_job_with_no_builds_is_not_validatable(self):
        spec, params = _params("rerun_pipeline", {"job": "payment-api"})
        result = validate_pipeline_exists(spec, params, _ctx(jenkins=_StubJenkins(last_build=None)))
        assert result.outcome == ValidationOutcome.NOT_VALIDATABLE
        assert "no previous build" in result.reason

    def test_lookup_error_is_not_validatable(self):
        spec, params = _params("rerun_pipeline", {"job": "payment-api"})
        result = validate_pipeline_exists(
            spec,
            params,
            _ctx(jenkins=_StubJenkins(error=EvidenceTimeoutError("jenkins down"))),
        )
        assert result.outcome == ValidationOutcome.NOT_VALIDATABLE


class TestRegistryCompleteness:
    """Every catalogue action's `validator` key must resolve - a future
    action without a wired validator fails this test, not the incident."""

    def test_every_catalogue_action_has_a_validator(self):
        for spec in all_actions():
            assert validator_for(spec) is not None, f"{spec.name}: no validator wired"

    def test_unknown_validator_key_raises(self):
        spec = get_action("restart_deployment")
        assert spec is not None
        broken = dataclasses.replace(spec, validator="no_such_validator")
        with pytest.raises(ValueError):
            validator_for(broken)


# --- Precheck plumbing unit tests ------------------------------------------


class _FakeAuthz:
    """AuthorizationV1Api stub returning a canned SSAR status dict."""

    def __init__(self, allowed: bool, reason: str = "") -> None:
        self._status = {"allowed": allowed, "reason": reason}
        self.bodies: list[Any] = []

    def create_self_subject_access_review(self, body):
        self.bodies.append(body)
        return {"status": self._status}


class TestPrechecksClient:
    """The live prechecks client over injected fakes - no cluster."""

    def test_can_i_allowed(self):
        authz = _FakeAuthz(allowed=True)
        client = KubernetesPrechecksClient(context="unused", authz_v1=authz)
        outcome = client.can_i("patch", "deployments", "payment-api", group="apps")
        assert outcome.ok is True

    def test_can_i_denied_carries_reason(self):
        authz = _FakeAuthz(allowed=False, reason="RBAC: access denied")
        client = KubernetesPrechecksClient(context="unused", authz_v1=authz)
        outcome = client.can_i("update", "deployments/scale", "payment-api", group="apps")
        assert outcome.ok is False
        assert outcome.detail == "RBAC: access denied"
        # The subresource split landed on the review spec.
        attrs = authz.bodies[0].spec.resource_attributes
        assert attrs.resource == "deployments"
        assert attrs.subresource == "scale"

    def test_can_i_api_error_raises_mapped(self):
        from kubernetes.client.exceptions import ApiException

        class DeniedAuthz:
            def create_self_subject_access_review(self, body):
                raise ApiException(status=403, reason="Forbidden")

        client = KubernetesPrechecksClient(context="unused", authz_v1=DeniedAuthz())
        with pytest.raises(EvidenceError):
            client.can_i("get", "pods", "payment-api")

    def test_dry_run_apply_ok(self):
        applied = []

        def apply_fn(docs):
            applied.extend(docs)

        client = KubernetesPrechecksClient(context="unused", apply_fn=apply_fn)
        text = (REPO_ROOT / DEPLOYMENT_PATH).read_text()
        outcome = client.dry_run_apply(text)
        assert outcome.ok is True
        assert len(applied) == 1

    def test_dry_run_apply_rejected(self):
        from kubernetes.client.exceptions import ApiException
        from kubernetes.utils.create_from_yaml import FailToCreateError

        body = json.dumps({"message": "spec.template: field is immutable"})

        def apply_fn(docs):
            api_exc = ApiException(status=422, reason="Unprocessable Entity")
            api_exc.body = body
            raise FailToCreateError([api_exc])

        client = KubernetesPrechecksClient(context="unused", apply_fn=apply_fn)
        outcome = client.dry_run_apply("apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\n")
        assert outcome.ok is False
        assert "field is immutable" in outcome.detail

    def test_dry_run_apply_malformed(self):
        client = KubernetesPrechecksClient(context="unused", apply_fn=lambda docs: None)
        assert client.dry_run_apply("not: [valid\n").ok is False
        assert client.dry_run_apply("# just a comment\n").ok is False
        assert client.dry_run_apply("a string, not an object\n").ok is False

    def test_dry_run_apply_machinery_error_raises(self):
        import urllib3.exceptions

        def transport_fail(docs):
            raise urllib3.exceptions.HTTPError("conn refused")

        client = KubernetesPrechecksClient(context="unused", apply_fn=transport_fail)
        with pytest.raises(EvidenceError):
            client.dry_run_apply("apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\n")


class TestFixturePrechecks:
    """Fixture-mode prechecks return canned PASS so fixture runs still
    exercise the dispatch."""

    def test_canned_pass(self):
        prechecks = FixtureKubernetesPrechecks()
        outcome = prechecks.can_i("patch", "deployments", "payment-api", group="apps")
        assert outcome.ok is True
        assert "fixture" in outcome.detail
        assert prechecks.dry_run_apply("anything").ok is True


class TestFixtureJobLookup:
    """get_job over fixtures: CI fixtures carry the job, cluster
    fixtures honestly do not."""

    def test_ci_fixture_job_exists(self):
        ev = FixtureJenkinsEvidence(
            {"fault": "x", "jenkins_job": "payment-api", "jenkins_build_number": 2}
        ).get_job("payment-api")
        assert ev.kind == EvidenceKind.JOB_INFO
        assert ev.payload["last_build"] == 2

    def test_fixture_without_jenkins_fields_not_found(self):
        with pytest.raises(EvidenceNotFoundError):
            FixtureJenkinsEvidence({"fault": "x"}).get_job("payment-api")

    def test_fixture_job_without_builds(self):
        ev = FixtureJenkinsEvidence({"fault": "x", "jenkins_job": "payment-api"}).get_job(
            "payment-api"
        )
        assert ev.payload["last_build"] is None


# --- Node + graph ----------------------------------------------------------


def _make_incident(sessions, signal: dict) -> str:
    with sessions() as session:
        return intake_signal(session, signal).incident_id


def _validating_state(sessions, incident_id: str, selected: dict, **extra) -> WorkflowState:
    """A state shaped like the coordinator's view at `validating`."""
    with sessions() as session:
        incident = session.scalar(
            select(models.Incident).where(models.Incident.incident_id == incident_id)
        )
        state: WorkflowState = {
            "incident_id": incident_id,
            "incident_pk": incident.id,
            "service": incident.service,
            "environment": incident.environment,
            "status": "validating",
            "signal": {"source": "kubernetes", "signature": {}, "raw": {}},
            "selected_action": selected,
            "attempts": 0,
            "errors": [],
        }
    state.update(extra)
    return state


def _validations(sessions, incident_id: str) -> list[models.Validation]:
    with sessions() as session:
        return list(
            session.scalars(
                select(models.Validation)
                .join(models.Incident)
                .where(models.Incident.incident_id == incident_id)
                .order_by(models.Validation.id)
            )
        )


def _verifications(sessions, incident_id: str) -> list[models.Verification]:
    with sessions() as session:
        return list(
            session.scalars(
                select(models.Verification)
                .join(models.Incident)
                .where(models.Incident.incident_id == incident_id)
                .order_by(models.Verification.id)
            )
        )


class TestValidateNode:
    """The node: dispatch, persistence, exclusion bookkeeping."""

    def test_pass_persists_row_and_state(self, sqlite_sessions, store):
        deps, _ = make_deps(sqlite_sessions, FakeLLM(), "cluster/crashloop", store)
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)
        selected = {"action": "reapply_manifest", "params": {"path": DEPLOYMENT_PATH}}

        update = validation_node(deps)(_validating_state(sqlite_sessions, incident_id, selected))

        assert update["validation"]["outcome"] == "pass"
        assert update["validation_attempts"] == 1
        rows = _validations(sqlite_sessions, incident_id)
        assert len(rows) == 1
        assert rows[0].outcome == ValidationOutcome.PASS
        assert rows[0].action == "reapply_manifest"
        assert rows[0].fingerprint == candidate_fingerprint(
            "reapply_manifest", {"path": DEPLOYMENT_PATH}
        )

    def test_fail_excludes_fingerprint(self, sqlite_sessions, store):
        deps, _ = make_deps(sqlite_sessions, FakeLLM(), "cluster/crashloop", store)
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)
        # crashloop fixture carries no deployment evidence -> the
        # deployment existence check fails honestly.
        selected = {"action": "restart_deployment", "params": dict(K8S)}

        update = validation_node(deps)(_validating_state(sqlite_sessions, incident_id, selected))

        assert update["validation"]["outcome"] == "fail"
        fp = candidate_fingerprint("restart_deployment", K8S)
        assert fp in update["excluded_candidates"]
        rows = _validations(sqlite_sessions, incident_id)
        assert rows[0].outcome == ValidationOutcome.FAIL
        assert rows[0].fingerprint == fp

    def test_no_selected_action_fails_not_crashes(self, sqlite_sessions, store):
        deps, _ = make_deps(sqlite_sessions, FakeLLM(), "cluster/crashloop", store)
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)

        update = validation_node(deps)(_validating_state(sqlite_sessions, incident_id, None))

        assert update["validation"]["outcome"] == "fail"
        assert "no selected candidate" in update["validation"]["reason"]
        # Nothing to fingerprint -> nothing excluded.
        assert update["excluded_candidates"] == []


class TestValidationE2E:
    """Full runs: intake -> diagnosis -> plan -> validate."""

    def _llm(self, plans: list[CandidateList]) -> FakeLLM:
        return FakeLLM(
            [
                EvidencePlan(requests=[]),
                Hypothesis(
                    claim="payment-api deployment is wedged on a bad image tag",
                    evidence_refs=[],
                    confidence=0.8,
                ),
                *plans,
            ]
        )

    def test_pass_run_executes_and_ends_at_verifying(self, sqlite_sessions, store):
        plan = CandidateList(
            candidates=[
                CandidateProposal(
                    action="reapply_manifest",
                    params={"path": DEPLOYMENT_PATH},
                    rationale="re-assert the deployment manifest",
                )
            ]
        )
        deps, caps = make_deps(sqlite_sessions, self._llm([plan]), "cluster/crashloop", store)
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)

        result = run_incident(incident_id, deps=deps)

        assert result.error is None
        # 1.9: a PASSing medium-tier sandbox action clears the policy
        # gate, executes through the recording fake, and the real verify
        # node then runs - the crashloop fixture still shows the fault,
        # so verification fails, the compensable action replans, and the
        # exhausted FakeLLM's empty plan escalates.
        assert result.status == IncidentStatus.ESCALATED
        assert result.run_status == models.RunStatus.TERMINAL
        rows = _validations(sqlite_sessions, incident_id)
        assert [r.outcome for r in rows] == [ValidationOutcome.PASS]
        # The fixture precheck's dry-run was exercised by the dispatch.
        assert caps["prechecks"].methods() == ["dry_run_apply"]
        # The write went through the recording fake, not a cluster - and
        # 1.10 ran the compensating reapply after verification failed.
        assert caps["kube_write"].methods() == ["apply_manifest", "apply_manifest"]
        with sqlite_sessions() as session:
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == incident_id)
            )
            pairs = [
                (r.from_status.value if r.from_status else None, r.to_status.value)
                for r in incident.transitions
            ]
        assert pairs[-4:] == [
            ("validating", "executing"),
            ("executing", "verifying"),
            ("verifying", "remediation_pending"),
            ("remediation_pending", "escalated"),
        ]

    def test_rerun_at_verifying_runs_verification(self, sqlite_sessions, store):
        """A fresh `helix run` on an incident parked at `verifying` is not
        a no-op anymore (1.9): the coordinator routes to the real verify
        node, which reloads the persisted execution (action + params +
        fingerprint from the executions row), verifies against the
        fixture, and routes onward - the fault still shows, so it fails
        and replans."""
        from datetime import UTC, datetime

        from state.machine import transition

        deps, _ = make_deps(sqlite_sessions, self._llm([]), "cluster/crashloop", store)
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)
        # Park the incident at verifying with a persisted successful
        # execution - the state a crashed mid-verify run leaves behind.
        params = {"path": DEPLOYMENT_PATH}
        with sqlite_sessions() as session:
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == incident_id)
            )
            cand = models.Candidate(
                incident_id=incident.id,
                attempt=0,
                rank=1,
                action="reapply_manifest",
                params=params,
                rationale="seeded",
                evidence_refs=[],
                score=1.5,
                risk_tier="medium",
                selected=True,
                components={},
            )
            session.add(cand)
            session.flush()
            session.add(
                models.Execution(
                    incident_id=incident.id,
                    candidate_id=cand.id,
                    attempt=0,
                    action="reapply_manifest",
                    params=params,
                    fingerprint=candidate_fingerprint("reapply_manifest", params),
                    policy_decision="allow_with_checks",
                    verification_required=True,
                    decided_by=None,
                    approval_id=None,
                    origin="fixture",
                    result=models.ExecutionResult.SUCCESS,
                    detail="applied",
                    output={"op": "apply_manifest", "path": DEPLOYMENT_PATH},
                    started_at=datetime.now(UTC),
                    finished_at=datetime.now(UTC),
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
        # The verify pass ran and failed against the crashloop fixture,
        # then replanned (exhausted FakeLLM -> empty plan -> escalate).
        assert result.status == IncidentStatus.ESCALATED
        verifications = _verifications(sqlite_sessions, incident_id)
        assert len(verifications) == 1
        assert verifications[0].outcome == models.VerificationOutcome.FAIL
        assert verifications[0].action == "reapply_manifest"
        # No new validation rows - the verify node did not re-run earlier
        # stages.
        assert _validations(sqlite_sessions, incident_id) == []

    def test_fail_replans_with_exclusion_then_passes(self, sqlite_sessions, store):
        """Fail -> REMEDIATION_PENDING -> replan excluding the failed
        fingerprint -> the new candidate validates -> VALIDATING."""
        bad = CandidateList(
            candidates=[
                CandidateProposal(
                    action="restart_deployment",
                    params=dict(K8S),
                    rationale="bounce it",
                )
            ]
        )
        # The replanned proposal set deliberately re-proposes the failed
        # candidate: the ranker must drop it via the exclusion list.
        retry = CandidateList(
            candidates=[
                CandidateProposal(
                    action="restart_deployment",
                    params=dict(K8S),
                    rationale="bounce it again",
                ),
                CandidateProposal(
                    action="reapply_manifest",
                    params={"path": DEPLOYMENT_PATH},
                    rationale="re-assert the manifest instead",
                ),
            ]
        )
        deps, _ = make_deps(sqlite_sessions, self._llm([bad, retry]), "cluster/crashloop", store)
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)

        result = run_incident(incident_id, deps=deps)

        assert result.error is None
        rows = _validations(sqlite_sessions, incident_id)
        assert [r.outcome for r in rows] == [ValidationOutcome.FAIL, ValidationOutcome.PASS]
        assert rows[0].action == "restart_deployment"
        assert rows[1].action == "reapply_manifest"
        # The exclusion drop reason reached the error log, and the
        # replanned selection is NOT the excluded fingerprint.
        assert any("already failed validation" in e for e in result.errors)
        assert result.selected_action["action"] == "reapply_manifest"
        # 1.9: the PASSing replanned candidate executes -> verify fails
        # (fixture still broken) -> replan -> empty -> escalate.
        assert result.status == IncidentStatus.ESCALATED
        with sqlite_sessions() as session:
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == incident_id)
            )
            pairs = [
                (r.from_status.value if r.from_status else None, r.to_status.value)
                for r in incident.transitions
            ]
        assert pairs == [
            (None, "received"),
            ("received", "diagnosing"),
            ("diagnosing", "remediation_pending"),
            ("remediation_pending", "validating"),
            ("validating", "remediation_pending"),  # failed -> replan
            ("remediation_pending", "validating"),  # new selection
            ("validating", "executing"),
            ("executing", "verifying"),
            ("verifying", "remediation_pending"),  # verify failed -> replan
            ("remediation_pending", "escalated"),  # exhausted LLM -> empty plan
        ]

    def test_second_consecutive_fail_escalates(self, sqlite_sessions, store):
        bad1 = CandidateList(
            candidates=[
                CandidateProposal(action="restart_deployment", params=dict(K8S), rationale="bounce")
            ]
        )
        bad2 = CandidateList(
            candidates=[
                CandidateProposal(
                    action="rollback_deployment", params=dict(K8S), rationale="roll back"
                )
            ]
        )
        deps, _ = make_deps(sqlite_sessions, self._llm([bad1, bad2]), "cluster/crashloop", store)
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)

        result = run_incident(incident_id, deps=deps)

        assert result.status == IncidentStatus.ESCALATED
        rows = _validations(sqlite_sessions, incident_id)
        assert [r.outcome for r in rows] == [ValidationOutcome.FAIL, ValidationOutcome.FAIL]
        with sqlite_sessions() as session:
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == incident_id)
            )
            last = incident.transitions[-1]
        assert (last.from_status, last.to_status) == (
            IncidentStatus.VALIDATING,
            IncidentStatus.ESCALATED,
        )
        assert "retry" in last.cause

    def test_exclusion_survives_across_runs(self, sqlite_sessions, store):
        """The exclusion list is persisted in `validations` rows - intake
        rebuilds `excluded_candidates` from the DB on every fresh run, so
        a once-failed candidate can never be re-selected later."""
        bad = CandidateList(
            candidates=[
                CandidateProposal(action="restart_deployment", params=dict(K8S), rationale="bounce")
            ]
        )
        deps, _ = make_deps(sqlite_sessions, self._llm([bad]), "cluster/crashloop", store)
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)

        run_incident(incident_id, deps=deps)

        rows = _validations(sqlite_sessions, incident_id)
        assert [r.outcome for r in rows] == [ValidationOutcome.FAIL]

        # A fresh intake on the same incident loads the persisted
        # exclusion - the fingerprint of the failed (action, params).
        from agent.graph import _intake_node

        loaded = _intake_node(deps)({"incident_id": incident_id})
        assert candidate_fingerprint("restart_deployment", K8S) in loaded["excluded_candidates"]

    def test_rerun_pipeline_on_ci_fixture_passes(self, sqlite_sessions, store):
        """rerun_pipeline over a CI fixture: the job exists with builds ->
        PASS through the real fixture capability, executes the low-tier
        write through the recording jenkins fake, then the 1.9 verify
        node checks the build - the fixture's latest build is FAILURE,
        so verification fails, the retryable action replans, and the
        empty replan escalates."""
        plan = CandidateList(
            candidates=[
                CandidateProposal(
                    action="rerun_pipeline",
                    params={"job": "payment-api"},
                    rationale="re-run the failed job",
                )
            ]
        )
        llm = FakeLLM(
            [
                EvidencePlan(requests=[]),
                Hypothesis(
                    claim="test stage failed on a transient runner timeout",
                    evidence_refs=[],
                    confidence=0.8,
                ),
                plan,
            ]
        )
        deps, caps = make_deps(sqlite_sessions, llm, "ci/test-regression", store)
        incident_id = _make_incident(sqlite_sessions, JENKINS_SIGNAL)

        result = run_incident(incident_id, deps=deps)

        assert result.error is None
        assert result.status == IncidentStatus.ESCALATED
        rows = _validations(sqlite_sessions, incident_id)
        assert [r.outcome for r in rows] == [ValidationOutcome.PASS]
        assert caps["jenkins_write"].methods() == ["trigger_build"]
        verifications = _verifications(sqlite_sessions, incident_id)
        assert [v.outcome for v in verifications] == [models.VerificationOutcome.FAIL]
        assert verifications[0].action == "rerun_pipeline"


# --- Live prechecks (integration tier) --------------------------------------


@pytest.mark.integration
class TestLivePrechecks:
    """Real can_i / dry_run_apply against the kind sandbox - the only
    honest check that the python-client calls do what kubectl would."""

    def test_can_i_read_is_allowed(self):
        client = KubernetesPrechecksClient()
        outcome = client.can_i("get", "pods", "payment-api")
        assert outcome.ok is True

    def test_dry_run_apply_committed_manifest(self):
        client = KubernetesPrechecksClient()
        outcome = client.dry_run_apply((REPO_ROOT / DEPLOYMENT_PATH).read_text())
        assert outcome.ok is True

    def test_dry_run_apply_garbage_manifest(self):
        client = KubernetesPrechecksClient()
        outcome = client.dry_run_apply(BAD_MANIFEST)
        assert outcome.ok is False
