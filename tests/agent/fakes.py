"""Test doubles and wiring helpers for agent tests.

`FakeLLM` is a scripted LLMClient - unit tests never touch a real
provider. `Recording` wraps a capability impl and logs every call so
pre-selection tests can assert exactly which evidence was requested.
`make_deps` builds GraphDeps over one fixture's Fixture*Evidence impls.
"""

import functools
from typing import Any

from agent.agents.diagnosis import EvidencePlan
from agent.graph import GraphDeps
from agent.state import Hypothesis
from helix.llm import LLMResponse
from integrations.fixtures import (
    FixtureDockerEvidence,
    FixtureElkEvidence,
    FixtureGitHubEvidence,
    FixtureJenkinsEvidence,
    FixtureKubernetesEvidence,
    FixtureKubernetesPrechecks,
)

K8S_SIGNAL = {
    "source": "kubernetes",
    "service": "payment-api",
    "environment": "sandbox",
    "severity": "high",
    "signature": {"waiting_reason": "CrashLoopBackOff", "container": "payment-api"},
    "raw": {"pod": "payment-api-98867c74d-h6j8v", "namespace": "payment-api"},
}

JENKINS_SIGNAL = {
    "source": "jenkins",
    "service": "payment-api",
    "signature": {"job": "payment-api", "stage": "Test"},
    "raw": {"job": "payment-api", "build_number": 42, "commit": "abc123def456"},
}


class FakeLLM:
    """Scripted `LLMClient`: pops queued responses for
    complete_structured in call order; a callable item is invoked with
    (prompt, schema) so tests can compute answers. A queued item that
    cannot answer the requested schema raises - a scripting bug in the
    test, never a silent wrong type. Once the queue is empty, `script`
    answers every call (for "always requests more" tests); with neither,
    the fake answers "no more evidence" for EvidencePlan and a bland
    hypothesis for Hypothesis, so tests only script the interesting
    calls."""

    def __init__(self, responses: list | None = None, script=None) -> None:
        self._responses = list(responses or [])
        self._script = script
        self.calls: list[tuple[str, Any]] = []

    def complete(self, prompt: str, *, max_tokens: int = 1024) -> LLMResponse:
        self.calls.append(("complete", prompt))
        return LLMResponse(text="fake", model="fake-model", stop_reason="stop")

    def complete_structured(self, prompt: str, schema: type, *, max_tokens: int = 2048):
        self.calls.append(("structured", schema))
        if self._responses:
            item = self._responses.pop(0)
            if callable(item):
                return item(prompt, schema)
            if isinstance(item, schema):
                return item
            raise AssertionError(
                f"FakeLLM: queued {type(item).__name__} cannot answer {schema.__name__}"
            )
        if self._script is not None:
            return self._script(prompt, schema)
        if schema is EvidencePlan:
            return EvidencePlan(requests=[])
        if schema is Hypothesis:
            return Hypothesis(claim="untested claim", evidence_refs=[], confidence=0.5)
        return schema.model_validate({})


class Recording:
    """Wraps a capability impl, logging (method, args, kwargs) per call."""

    def __init__(self, impl: Any) -> None:
        self.impl = impl
        self.calls: list[tuple[str, tuple, dict]] = []

    def __getattr__(self, name: str):
        attr = getattr(self.impl, name)
        if not callable(attr):
            return attr

        @functools.wraps(attr)
        def wrapper(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return attr(*args, **kwargs)

        return wrapper

    def methods(self) -> list[str]:
        return [name for name, _, _ in self.calls]


def make_deps(
    session_factory,
    llm,
    fixture: str,
    store,
    *,
    database_url: str = "sqlite://",
    max_evidence_rounds: int = 3,
    prechecks=None,
) -> tuple[GraphDeps, dict[str, Recording]]:
    """GraphDeps over one fixture's Fixture*Evidence impls, each wrapped in
    a Recording proxy. Returns (deps, recorders-by-domain).

    `prechecks` defaults to the fixture impl (canned pass); pass a stub to
    script can_i/dry_run_apply verdicts in validation tests."""
    doc = store.load(fixture)
    caps = {
        "kubernetes": Recording(FixtureKubernetesEvidence(doc)),
        "jenkins": Recording(FixtureJenkinsEvidence(doc)),
        "elk": Recording(FixtureElkEvidence(doc)),
        "github": Recording(FixtureGitHubEvidence(doc)),
        "docker": Recording(FixtureDockerEvidence(doc)),
        "prechecks": Recording(prechecks or FixtureKubernetesPrechecks()),
    }
    deps = GraphDeps(
        session_scope=session_factory,
        llm=llm,
        kubernetes=caps["kubernetes"],
        jenkins=caps["jenkins"],
        elk=caps["elk"],
        github=caps["github"],
        docker=caps["docker"],
        kube_prechecks=caps["prechecks"],
        database_url=database_url,
        max_evidence_rounds=max_evidence_rounds,
    )
    return deps, caps
