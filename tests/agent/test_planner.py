"""Recovery Planner unit tests (subphase 1.6): the action catalogue, the
deterministic ranker, the plan node, and the end-to-end intake ->
diagnosis -> plan run.

All unit tier: in-memory SQLite, fixture-backed capabilities, scripted
FakeLLM - no live infrastructure, no real LLM calls. The per-scenario
table is the planner-side half of the benchmark contract: each scenario's
optimal/valid/unsafe sets live in benchmark/scenarios/*.yaml and the test
asserts the ranker's top pick lands in the valid set with no unsafe
action first.
"""

from pathlib import Path

import pytest
import yaml
from fakes import JENKINS_SIGNAL, K8S_SIGNAL, FakeLLM, make_deps
from sqlalchemy import select

from agent.actions.catalogue import (
    SECRET_MANIFEST_PATHS,
    ActionSpec,
    Reversibility,
    RiskTier,
    all_actions,
    get_action,
    register_action,
)
from agent.agents.diagnosis import EvidencePlan
from agent.agents.planner import (
    MIN_SELECT_SCORE,
    CandidateList,
    CandidateProposal,
    planner_node,
    rank_candidates,
)
from agent.runner import run_incident
from agent.state import Hypothesis, WorkflowState
from integrations.evidence import (
    Evidence,
    EvidenceKind,
    EvidenceOrigin,
    EvidenceSource,
)
from state import models
from state.intake import intake_signal
from state.machine import IncidentStatus

REPO_ROOT = Path(__file__).resolve().parents[2]
SCENARIOS_DIR = REPO_ROOT / "benchmark" / "scenarios"

GITHUB_SIGNAL = {
    "source": "github",
    "service": "payment-api",
    "environment": "sandbox",
    "severity": "medium",
    "signature": {"pr": 42},
    "raw": {"pr": 42, "sha": "abc123def456"},
}

K8S = {"deployment": "payment-api", "namespace": "payment-api"}
SECRET_PATH = "sandbox/payment-api/k8s/03-secret.yaml"
CONFIGMAP_PATH = "sandbox/payment-api/k8s/02-configmap.yaml"
DEPLOYMENT_PATH = "sandbox/payment-api/k8s/04-deployment.yaml"


def _pr(path: str) -> dict:
    """A minimal valid open_fix_pr params dict touching one file."""
    return {
        "branch": "helix/INC-ab12cd34",
        "files": [{"path": path, "content": "fixed content\n"}],
        "title": "helix: remediation",
        "body": "",
    }


def _scenario(name: str) -> dict:
    return yaml.safe_load((SCENARIOS_DIR / f"{name}.yaml").read_text())


def _evidence(*ids: str, summaries: dict[str, str] | None = None) -> list[Evidence]:
    """Synthetic collected evidence with explicit ids - the ranker only
    needs resolvable ids and summary text, not fixture payloads."""
    out = []
    for ev_id in ids:
        out.append(
            Evidence(
                id=ev_id,
                source=EvidenceSource.KUBERNETES,
                kind=EvidenceKind.POD_LIST,
                service="payment-api",
                summary=(summaries or {}).get(ev_id, f"evidence {ev_id}"),
                payload={"note": ev_id},
                origin=EvidenceOrigin.FIXTURE,
            )
        )
    return out


#: Per-scenario planner inputs: a hypothesis claim matching the fault, the
#: signal the intake would carry, and the proposals a reasonable model
#: emits (the optimal/valid actions plus at least one unsafe pretender).
#: The assertions consume the scenario YAML's own valid/unsafe sets.
SCENARIO_CASES = {
    "ci-dependency-failure": {
        "signal": JENKINS_SIGNAL,
        "claim": "uv sync fails to resolve this-package-definitely-does-not-exist-"
        "helix-fault>=1.0 declared in pyproject.toml - the dependency is unresolvable",
        "proposals": [
            ("open_fix_pr", _pr("sandbox/payment-api/pyproject.toml")),
            ("rerun_pipeline", {"job": "payment-api"}),
            ("restart_deployment", K8S),
            ("rollback_deployment", K8S),
        ],
    },
    "ci-test-regression": {
        "signal": JENKINS_SIGNAL,
        "claim": "test_health_returns_200 asserts status_code == 999 in "
        "tests/test_main.py - the Test stage fails deterministically",
        "proposals": [
            ("open_fix_pr", _pr("sandbox/payment-api/tests/test_main.py")),
            ("rerun_pipeline", {"job": "payment-api"}),
            ("restart_deployment", K8S),
        ],
    },
    "ci-docker-build-failure": {
        "signal": JENKINS_SIGNAL,
        "claim": "the docker build fails at Dockerfile COPY "
        "this-file-does-not-exist-helix-fault.txt - the file is absent",
        "proposals": [
            ("open_fix_pr", _pr("sandbox/payment-api/Dockerfile")),
            ("rerun_pipeline", {"job": "payment-api"}),
            ("scale_deployment", {**K8S, "replicas": 2}),
        ],
    },
    "config-mismatch": {
        "signal": K8S_SIGNAL,
        "claim": "an env override sets PAYMENT_API_DOWNSTREAM_URL to a nonexistent "
        "host; the new ReplicaSet never becomes ready and the rollout is wedged",
        "proposals": [
            ("rollback_deployment", K8S),
            ("reapply_manifest", {"path": CONFIGMAP_PATH}),
            ("restart_deployment", K8S),
            ("scale_deployment", {**K8S, "replicas": 1}),
        ],
    },
    "crashloop": {
        "signal": K8S_SIGNAL,
        "claim": "the payment-api container exits 1 because PAYMENT_API_API_KEY "
        "from payment-api-secret is blank whitespace",
        "proposals": [
            ("reapply_manifest", {"path": SECRET_PATH}),
            ("open_fix_pr", _pr(SECRET_PATH)),
            ("restart_deployment", K8S),
            ("scale_deployment", {**K8S, "replicas": 1}),
        ],
    },
    "oom-killed": {
        "signal": K8S_SIGNAL,
        "claim": "env var PAYMENT_API_BALLAST_MB=200 pushes the payment-api "
        "container over its 128Mi memory limit; OOMKilled exit 137",
        "proposals": [
            ("rollback_deployment", K8S),
            ("open_fix_pr", _pr(DEPLOYMENT_PATH)),
            ("restart_deployment", K8S),
            ("scale_deployment", {**K8S, "replicas": 1}),
        ],
    },
    "failed-rollout": {
        "signal": K8S_SIGNAL,
        "claim": "deployment payment-api rollout wedged: the new pod cannot pull "
        "image payment-api:doesnotexist (ImagePullBackOff)",
        "proposals": [
            ("rollback_deployment", K8S),
            ("reapply_manifest", {"path": DEPLOYMENT_PATH}),
            ("restart_deployment", K8S),
            ("scale_deployment", {**K8S, "replicas": 1}),
        ],
    },
    "missing-secret": {
        "signal": K8S_SIGNAL,
        "claim": "payment-api-secret is missing so envFrom cannot resolve; the "
        "container never starts (CreateContainerConfigError)",
        "proposals": [
            ("reapply_manifest", {"path": SECRET_PATH}),
            ("open_fix_pr", _pr(SECRET_PATH)),
            ("restart_deployment", K8S),
            ("scale_deployment", {**K8S, "replicas": 1}),
        ],
    },
    "bad-deployment-config": {
        "signal": K8S_SIGNAL,
        "claim": "a patch replacing the deployment memory limit with "
        "'not-a-quantity' was rejected by API schema validation; the live "
        "deployment is unchanged",
        "proposals": [
            ("open_fix_pr", _pr(DEPLOYMENT_PATH)),
            ("reapply_manifest", {"path": DEPLOYMENT_PATH}),
            ("scale_deployment", {**K8S, "replicas": 1}),
            ("restart_deployment", K8S),
        ],
    },
    "rejected-pr": {
        "signal": GITHUB_SIGNAL,
        "claim": "pull request #42 received a changes-requested review on the fix branch",
        "proposals": [
            ("open_fix_pr", _pr("sandbox/payment-api/payment_api/main.py")),
            ("rerun_pipeline", {"job": "payment-api"}),
            ("restart_deployment", K8S),
            ("scale_deployment", {**K8S, "replicas": 1}),
        ],
    },
    "failed-check": {
        "signal": GITHUB_SIGNAL,
        "claim": "the 'tests' check run on the head commit failed on a "
        "transient runner timeout, not a code failure",
        "proposals": [
            ("rerun_pipeline", {"job": "payment-api"}),
            ("open_fix_pr", _pr("sandbox/payment-api/tests/test_main.py")),
            ("restart_deployment", K8S),
        ],
    },
    "stale-branch": {
        "signal": GITHUB_SIGNAL,
        "claim": "the feature branch is 14 commits behind main and can no longer merge cleanly",
        "proposals": [
            ("open_fix_pr", _pr("sandbox/payment-api/payment_api/main.py")),
            ("rerun_pipeline", {"job": "payment-api"}),
            ("restart_deployment", K8S),
        ],
    },
}


def _make_incident(sessions, signal: dict) -> str:
    with sessions() as session:
        return intake_signal(session, signal).incident_id


def _run_planner(
    deps,
    sessions,
    incident_id: str,
    *,
    hypothesis: Hypothesis,
    evidence: list[Evidence],
    signal: dict,
) -> dict:
    """Invoke the plan node on a state shaped like the coordinator's view
    at remediation_pending."""
    with sessions() as session:
        incident = session.scalar(
            select(models.Incident).where(models.Incident.incident_id == incident_id)
        )
        state: WorkflowState = {
            "incident_id": incident_id,
            "incident_pk": incident.id,
            "service": incident.service,
            "environment": incident.environment,
            "severity": incident.severity,
            "status": "remediation_pending",
            "signal": {
                "source": signal["source"],
                "signature": signal.get("signature", {}),
                "raw": signal.get("raw", {}),
            },
            "evidence": [e.model_dump(mode="json") for e in evidence],
            "hypothesis": hypothesis.model_dump(mode="json"),
            "attempts": 0,
            "errors": [],
        }
    return planner_node(deps)(state)


def _proposal_list(cases: list[tuple[str, dict]], ref: str) -> CandidateList:
    return CandidateList(
        candidates=[
            CandidateProposal(
                action=action,
                params=params,
                rationale=f"{action} addresses the diagnosed cause",
                evidence_refs=[ref],
            )
            for action, params in cases
        ]
    )


class TestScenarioRanking:
    """The 1.6 exit criterion, per scenario: the ranker emits a non-empty
    ordered list, the top candidate sits in the scenario's valid set, and
    no unsafe action is ever ranked first."""

    @pytest.mark.parametrize("name", sorted(SCENARIO_CASES))
    def test_top_candidate_valid_unsafe_never_first(self, name, sqlite_sessions):
        case = SCENARIO_CASES[name]
        scenario = _scenario(name)
        evidence = _evidence(f"ev_{name}_1")
        hypothesis = Hypothesis(claim=case["claim"], evidence_refs=[f"ev_{name}_1"], confidence=0.8)
        proposal = _proposal_list(case["proposals"], f"ev_{name}_1")

        ranked, drops = rank_candidates(
            proposal,
            hypothesis=hypothesis,
            evidence=evidence,
            signal=case["signal"],
        )

        assert drops == []
        assert len(ranked) == len(case["proposals"])
        assert [c.rank for c in ranked] == list(range(1, len(ranked) + 1))
        assert ranked[0].action in scenario["valid_actions"], (
            f"{name}: top candidate {ranked[0].action} not in {scenario['valid_actions']}"
        )
        assert ranked[0].action not in scenario["unsafe_actions"]
        assert ranked[0].score >= MIN_SELECT_SCORE

    @pytest.mark.parametrize("name", sorted(SCENARIO_CASES))
    def test_every_scenario_optimal_is_proposed_and_valid(self, name):
        scenario = _scenario(name)
        proposed = {action for action, _ in SCENARIO_CASES[name]["proposals"]}
        # The scenario's own YAML is the contract: optimal must be a real
        # catalogue action inside the valid set (the consistency test in
        # tests/benchmark pins the same thing file-side).
        assert scenario["optimal_action"] in scenario["valid_actions"]
        assert get_action(scenario["optimal_action"]) is not None
        assert scenario["optimal_action"] in proposed


class TestCatalogueIntegrity:
    """The registry is the bounded catalogue: bounds at param validation,
    tier as a function of params, compensating-action integrity."""

    def test_six_actions_registered(self):
        assert {s.name for s in all_actions()} == {
            "restart_deployment",
            "rollback_deployment",
            "scale_deployment",
            "reapply_manifest",
            "open_fix_pr",
            "rerun_pipeline",
        }

    def test_compensable_without_compensation_fails_registration(self):
        with pytest.raises(ValueError):
            register_action(
                ActionSpec(
                    name="bogus_compensable",
                    params_model=type("P", (object,), {}),  # type: ignore[arg-type]
                    risk_tier=lambda p: RiskTier.LOW,
                    preconditions=(),
                    expected_effect="test",
                    reversibility=Reversibility.COMPENSABLE,
                    compensating_action=None,
                    validator="v",
                    executor="e",
                    blast_radius=0.1,
                    params_hint="x",
                )
            )

    def test_stateless_with_compensation_fails_registration(self):
        with pytest.raises(ValueError):
            register_action(
                ActionSpec(
                    name="bogus_stateless",
                    params_model=type("P", (object,), {}),  # type: ignore[arg-type]
                    risk_tier=lambda p: RiskTier.LOW,
                    preconditions=(),
                    expected_effect="test",
                    reversibility=Reversibility.STATELESS,
                    compensating_action="something",
                    validator="v",
                    executor="e",
                    blast_radius=0.1,
                    params_hint="x",
                )
            )


class TestParamBounds:
    """Unsafe shapes are rejected at params_model validation - the planner
    cannot express them, so they can never be ranked first."""

    def test_scale_to_zero_rejected(self):
        spec = get_action("scale_deployment")
        assert spec is not None
        with pytest.raises(Exception):
            spec.params_model.model_validate({**K8S, "replicas": 0})

    def test_reapply_outside_allowlist_rejected(self):
        spec = get_action("reapply_manifest")
        assert spec is not None
        for bad in (
            "sandbox/payment-api/payment_api/main.py",
            "sandbox/payment-api/k8s/99-not-a-manifest.yaml",
            "../secrets.yaml",
            "k8s/03-secret.yaml",
        ):
            with pytest.raises(Exception):
                spec.params_model.model_validate({"path": bad})

    def test_open_fix_pr_bounds(self):
        spec = get_action("open_fix_pr")
        assert spec is not None
        with pytest.raises(Exception):  # branch off helix/INC-*
            spec.params_model.model_validate({**_pr("sandbox/payment-api/x.py"), "branch": "main"})
        with pytest.raises(Exception):  # file outside the write root
            spec.params_model.model_validate(_pr("agent/graph.py"))

    def test_reapply_secret_is_high_tier(self):
        """The param-dependent tier: same action, different risk."""
        spec = get_action("reapply_manifest")
        assert spec is not None
        secret = spec.params_model.model_validate({"path": SECRET_PATH})
        configmap = spec.params_model.model_validate({"path": CONFIGMAP_PATH})
        assert spec.risk_tier(secret) == RiskTier.HIGH
        assert spec.risk_tier(configmap) == RiskTier.MEDIUM
        # The set the tier consults must be exactly the real secret file.
        assert SECRET_MANIFEST_PATHS == {SECRET_PATH}

    def test_open_fix_pr_secret_file_is_high_tier(self):
        spec = get_action("open_fix_pr")
        assert spec is not None
        on_secret = spec.params_model.model_validate(_pr(SECRET_PATH))
        on_code = spec.params_model.model_validate(_pr("sandbox/payment-api/payment_api/main.py"))
        assert spec.risk_tier(on_secret) == RiskTier.HIGH
        assert spec.risk_tier(on_code) == RiskTier.MEDIUM

    def test_scale_tier_depends_on_replicas(self):
        spec = get_action("scale_deployment")
        assert spec is not None
        one = spec.params_model.model_validate({**K8S, "replicas": 1})
        three = spec.params_model.model_validate({**K8S, "replicas": 3})
        assert spec.risk_tier(one) == RiskTier.LOW
        assert spec.risk_tier(three) == RiskTier.MEDIUM


class TestRanker:
    """Determinism and the constraint drops - pure Python, zero LLM."""

    def test_deterministic_same_inputs_same_order(self):
        evidence = _evidence("ev_a")
        hypothesis = Hypothesis(claim="secret value blank", evidence_refs=["ev_a"], confidence=0.8)
        proposal = _proposal_list(
            [
                ("reapply_manifest", {"path": SECRET_PATH}),
                ("restart_deployment", K8S),
            ],
            "ev_a",
        )
        first, drops1 = rank_candidates(
            proposal, hypothesis=hypothesis, evidence=evidence, signal=K8S_SIGNAL
        )
        second, drops2 = rank_candidates(
            proposal, hypothesis=hypothesis, evidence=evidence, signal=K8S_SIGNAL
        )
        assert [c.action for c in first] == [c.action for c in second]
        assert [c.score for c in first] == [c.score for c in second]
        assert drops1 == drops2

    def test_unknown_action_dropped(self):
        hypothesis = Hypothesis(claim="x", evidence_refs=[], confidence=0.8)
        proposal = CandidateList(
            candidates=[
                CandidateProposal(action="kubectl_delete_all", params={}, rationale="yolo"),
                CandidateProposal(action="restart_deployment", params=K8S, rationale="bounce"),
            ]
        )
        ranked, drops = rank_candidates(
            proposal, hypothesis=hypothesis, evidence=[], signal=K8S_SIGNAL
        )
        assert [c.action for c in ranked] == ["restart_deployment"]
        assert any("not in the action catalogue" in d for d in drops)

    def test_invalid_params_dropped(self):
        hypothesis = Hypothesis(claim="x", evidence_refs=[], confidence=0.8)
        proposal = CandidateList(
            candidates=[
                CandidateProposal(
                    action="scale_deployment",
                    params={**K8S, "replicas": 0},  # unexpressable-as-valid
                    rationale="scale it",
                )
            ]
        )
        ranked, drops = rank_candidates(
            proposal, hypothesis=hypothesis, evidence=[], signal=K8S_SIGNAL
        )
        assert ranked == []
        assert any("params failed validation" in d for d in drops)

    def test_dangling_refs_score_lower_than_cited(self):
        evidence = _evidence("ev_real")
        hypothesis = Hypothesis(claim="secret blank", evidence_refs=["ev_real"], confidence=0.8)
        cited = CandidateProposal(
            action="reapply_manifest",
            params={"path": SECRET_PATH},
            rationale="cited",
            evidence_refs=["ev_real"],
        )
        dangling = CandidateProposal(
            action="reapply_manifest",
            params={"path": SECRET_PATH},
            rationale="uncited",
            evidence_refs=["ev_invented"],
        )
        ranked, _ = rank_candidates(
            CandidateList(candidates=[dangling, cited]),
            hypothesis=hypothesis,
            evidence=evidence,
            signal=K8S_SIGNAL,
        )
        assert ranked[0].rationale == "cited"
        assert ranked[0].components["evidence_support"] == 1.0
        assert ranked[1].components["evidence_support"] == 0.0


class TestPlanNode:
    """The node itself: outcomes into state, full ranked list persisted."""

    def test_selected_action_and_full_candidate_rows(self, sqlite_sessions, store):
        proposal = _proposal_list(
            [
                ("reapply_manifest", {"path": SECRET_PATH}),
                ("restart_deployment", K8S),
                ("scale_deployment", {**K8S, "replicas": 1}),
            ],
            "ev_1",
        )
        deps, _ = make_deps(sqlite_sessions, FakeLLM([proposal]), "cluster/crashloop", store)
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)
        evidence = _evidence("ev_1")
        hypothesis = Hypothesis(
            claim="container exits 1 on blank PAYMENT_API_API_KEY from payment-api-secret",
            evidence_refs=["ev_1"],
            confidence=0.8,
        )

        result = _run_planner(
            deps,
            sqlite_sessions,
            incident_id,
            hypothesis=hypothesis,
            evidence=evidence,
            signal=K8S_SIGNAL,
        )

        assert result["plan_complete"] is True
        assert result["escalation_cause"] is None
        assert result["selected_action"]["action"] == "reapply_manifest"
        # Exactly one structured proposal call - the ranker is pure Python
        # and never touches the LLM.
        assert len(deps.llm.calls) == 1
        assert result["selected_action"]["rank"] == 1
        # All three proposed candidates persisted, fully ranked.
        assert len(result["candidates"]) == 3
        with sqlite_sessions() as session:
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == incident_id)
            )
            rows = session.scalars(
                select(models.Candidate)
                .where(models.Candidate.incident_id == incident.id)
                .order_by(models.Candidate.rank)
            ).all()
        assert [r.action for r in rows] == [c["action"] for c in result["candidates"]]
        assert [r.rank for r in rows] == [1, 2, 3]
        assert rows[0].selected is True and rows[0].risk_tier == "high"
        assert all(r.rationale and r.evidence_refs == ["ev_1"] for r in rows)
        assert rows[0].components["expected_effect"] > 0

    def test_empty_proposal_escalates(self, sqlite_sessions, store):
        deps, _ = make_deps(
            sqlite_sessions,
            FakeLLM([CandidateList(candidates=[])]),
            "cluster/crashloop",
            store,
        )
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)

        result = _run_planner(
            deps,
            sqlite_sessions,
            incident_id,
            hypothesis=Hypothesis(claim="x", evidence_refs=[], confidence=0.8),
            evidence=[],
            signal=K8S_SIGNAL,
        )

        assert result["plan_complete"] is True
        assert result["selected_action"] is None
        assert "no candidate" in result["escalation_cause"]

    def test_unsafe_only_proposal_escalates(self, sqlite_sessions, store):
        """Every proposal fails catalogue constraints -> escalate."""
        deps, _ = make_deps(
            sqlite_sessions,
            FakeLLM(
                [
                    CandidateList(
                        candidates=[
                            CandidateProposal(
                                action="scale_deployment",
                                params={**K8S, "replicas": 0},
                                rationale="shut it down",
                            ),
                            CandidateProposal(
                                action="delete_namespace", params={}, rationale="nuke it"
                            ),
                        ]
                    )
                ]
            ),
            "cluster/crashloop",
            store,
        )
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)

        result = _run_planner(
            deps,
            sqlite_sessions,
            incident_id,
            hypothesis=Hypothesis(claim="x", evidence_refs=[], confidence=0.8),
            evidence=[],
            signal=K8S_SIGNAL,
        )

        assert result["plan_complete"] is True
        assert result["selected_action"] is None
        assert "survived catalogue constraints" in result["escalation_cause"]
        assert len(result["errors"]) >= 2  # one drop reason per rejected proposal

    def test_low_confidence_hypothesis_escalates_without_llm(self, sqlite_sessions, store):
        deps, _ = make_deps(sqlite_sessions, FakeLLM(), "cluster/crashloop", store)
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)

        result = _run_planner(
            deps,
            sqlite_sessions,
            incident_id,
            hypothesis=Hypothesis(claim="uncertain", evidence_refs=[], confidence=0.1),
            evidence=[],
            signal=K8S_SIGNAL,
        )

        assert "confidence" in result["escalation_cause"]
        assert result["selected_action"] is None
        # The gate fired before any LLM call.
        assert deps.llm.calls == []

    def test_llm_error_marks_plan_failed(self, sqlite_sessions, store):
        from helix.llm import LLMError

        class DownLLM(FakeLLM):
            def complete_structured(self, prompt, schema, *, max_tokens=2048):
                raise LLMError("provider down")

        deps, _ = make_deps(sqlite_sessions, DownLLM(), "cluster/crashloop", store)
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)

        result = _run_planner(
            deps,
            sqlite_sessions,
            incident_id,
            hypothesis=Hypothesis(claim="x", evidence_refs=[], confidence=0.8),
            evidence=[],
            signal=K8S_SIGNAL,
        )

        assert result["plan_complete"] is False
        assert result["plan_failed"]
        assert result["escalation_cause"] is None
        assert any("proposal failed" in e for e in result["errors"])

    def test_score_floor_escalates_weak_proposals(self, sqlite_sessions, store):
        """A candidate citing nothing and matching nothing stays below the
        bar - 'no safe action' covers it, it is not an error."""
        deps, _ = make_deps(
            sqlite_sessions,
            FakeLLM(
                [
                    CandidateList(
                        candidates=[
                            CandidateProposal(
                                action="rerun_pipeline",
                                params={"job": "definitely-not-a-real-job-xyz"},
                                rationale="uncited and off-target",
                                evidence_refs=["ev_invented"],
                            )
                        ]
                    )
                ]
            ),
            "cluster/crashloop",
            store,
        )
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)

        result = _run_planner(
            deps,
            sqlite_sessions,
            incident_id,
            hypothesis=Hypothesis(
                claim="container exits 1 on blank secret", evidence_refs=[], confidence=0.8
            ),
            evidence=[],
            signal=K8S_SIGNAL,
        )

        assert result["selected_action"] is None
        assert "safe bar" in result["escalation_cause"]


class TestEndToEnd:
    """intake -> diagnosis -> plan on fixture evidence: the run now
    reaches VALIDATING with a selected action, or ESCALATED."""

    def test_run_reaches_validating_with_selected_action(self, sqlite_sessions, store):
        # Callable FakeLLM items read the prompt's evidence list so both
        # the hypothesis and the proposals cite ids that really exist.
        def cited_ids(prompt: str) -> list[str]:
            return [
                line.split("|")[0].strip("- ").strip()
                for line in prompt.splitlines()
                if line.strip().startswith("- ev_")
            ]

        def interpret(prompt: str, schema):
            return Hypothesis(
                claim="payment-api container exits 1 on blank PAYMENT_API_API_KEY "
                "from payment-api-secret",
                evidence_refs=cited_ids(prompt)[:1],
                confidence=0.85,
            )

        def propose(prompt: str, schema):
            ids = cited_ids(prompt)
            return CandidateList(
                candidates=[
                    CandidateProposal(
                        action="reapply_manifest",
                        params={"path": SECRET_PATH},
                        rationale="restore the secret value",
                        evidence_refs=ids[:1],
                    ),
                    CandidateProposal(
                        action="restart_deployment",
                        params=K8S,
                        rationale="bounce the pod",
                        evidence_refs=ids[:1],
                    ),
                ]
            )

        llm = FakeLLM([EvidencePlan(requests=[]), interpret, propose])
        deps, _ = make_deps(sqlite_sessions, llm, "cluster/crashloop", store)
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)

        result = run_incident(incident_id, deps=deps)

        assert result.error is None
        # 1.8: the selected action re-applies 03-secret.yaml - a high-tier
        # secret change -> REQUIRE_APPROVAL, so the run suspends at
        # awaiting_approval with a pending approvals row (the 1.8 gate
        # test suite covers approve/reject from here).
        assert result.status == IncidentStatus.AWAITING_APPROVAL
        assert result.run_status == models.RunStatus.SUSPENDED
        assert result.selected_action is not None
        assert result.selected_action["action"] == "reapply_manifest"
        with sqlite_sessions() as session:
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == incident_id)
            )
            rows = session.scalars(
                select(models.Candidate)
                .where(models.Candidate.incident_id == incident.id)
                .order_by(models.Candidate.rank)
            ).all()
            assert len(rows) == 2
            assert rows[0].action == "reapply_manifest" and rows[0].selected
            approvals = session.scalars(
                select(models.Approval).where(models.Approval.incident_id == incident.id)
            ).all()
            assert len(approvals) == 1
            assert approvals[0].status == models.ApprovalStatus.PENDING
            assert approvals[0].action == "reapply_manifest"
            pairs = [
                (r.from_status.value if r.from_status else None, r.to_status.value)
                for r in incident.transitions
            ]
        assert pairs == [
            (None, "received"),
            ("received", "diagnosing"),
            ("diagnosing", "remediation_pending"),
            ("remediation_pending", "validating"),
            ("validating", "awaiting_approval"),
        ]

    def test_low_confidence_hypothesis_ends_escalated(self, sqlite_sessions, store):
        llm = FakeLLM(
            [
                EvidencePlan(requests=[]),
                Hypothesis(claim="guessing", evidence_refs=[], confidence=0.1),
            ]
        )
        deps, _ = make_deps(sqlite_sessions, llm, "cluster/crashloop", store)
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)

        result = run_incident(incident_id, deps=deps)

        assert result.error is None
        assert result.status == IncidentStatus.ESCALATED
        assert result.run_status == models.RunStatus.TERMINAL
        assert result.selected_action is None
        with sqlite_sessions() as session:
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == incident_id)
            )
            last = incident.transitions[-1]
        assert (last.from_status, last.to_status) == (
            IncidentStatus.REMEDIATION_PENDING,
            IncidentStatus.ESCALATED,
        )
        assert "confidence" in last.cause

    def test_empty_plan_ends_escalated(self, sqlite_sessions, store):
        llm = FakeLLM(
            [
                EvidencePlan(requests=[]),
                Hypothesis(claim="mystery", evidence_refs=[], confidence=0.8),
                CandidateList(candidates=[]),
            ]
        )
        deps, _ = make_deps(sqlite_sessions, llm, "cluster/crashloop", store)
        incident_id = _make_incident(sqlite_sessions, K8S_SIGNAL)

        result = run_incident(incident_id, deps=deps)

        assert result.status == IncidentStatus.ESCALATED
        with sqlite_sessions() as session:
            incident = session.scalar(
                select(models.Incident).where(models.Incident.incident_id == incident_id)
            )
            assert "no candidate" in incident.transitions[-1].cause
