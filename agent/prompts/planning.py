"""Recovery Planner prompt template - single-sourced here (subphase 1.6).

`planning_prompt` asks the model for a `CandidateList`: bounded recovery
candidates drawn ONLY from the registered action catalogue. The model
proposes; the deterministic ranker in agents/planner.py scores - no
ordering the model emits is trusted.

Design constraints the prompt must carry:

- Only catalogue actions exist. A proposal naming anything else is
  dropped before ranking (the menu is rendered from the registry by
  `catalogue_view()`, so it cannot drift).
- Evidence citations work exactly like diagnosis: `evidence_refs` may
  only contain ids from the collected-evidence list; dangling refs just
  score lower at rank time rather than crashing the call.
- `open_fix_pr` carries full new file contents - that content is what the
  1.7 validator checks before any PR opens, so the prompt must demand
  complete contents, not diffs.
- An empty `candidates` list is the honest "nothing safe to propose"
  answer - the planner turns it into an escalation, never an error.
"""

from typing import Any

#: Hard bounds restated in prose so the model rarely wastes a proposal on
#: params the models would reject. (The real enforcement is pydantic -
#: this text just keeps proposals inside it.)
_PARAM_RULES = """\
- restart_deployment / rollback_deployment: deployment + namespace.
- scale_deployment: deployment + namespace + replicas (>= 1; proposing 0
  is rejected - scaling to zero is an outage, never a remediation).
- reapply_manifest: path must be one of the committed
  sandbox/payment-api/k8s/ manifests named in the catalogue entry.
- open_fix_pr: branch must be helix/<incident id>; each file is
  {path, content} with the COMPLETE new file contents (never a diff),
  and every path stays under sandbox/payment-api/.
- rerun_pipeline: the Jenkins job name.
"""


def _incident_block(incident: dict[str, Any]) -> str:
    return (
        f"Incident: {incident['incident_id']}\n"
        f"Service: {incident['service']}  Environment: {incident['environment']}  "
        f"Severity: {incident['severity']}\n"
        f"Signal source: {incident['signal'].get('source')}\n"
        f"Signal signature: {incident['signal'].get('signature')}\n"
        f"Signal detail: {incident['signal'].get('raw')}"
    )


def _evidence_block(evidence: list[dict[str, Any]]) -> str:
    """Same summary-only discipline as diagnosis: `id | source | kind |
    summary` lines; payloads stay retrievable by id."""
    if not evidence:
        return "No evidence collected."
    return "\n".join(
        f"- {e['id']} | {e['source']} | {e['kind']} | {e['summary']}" for e in evidence
    )


def planning_prompt(
    incident: dict[str, Any],
    hypothesis: dict[str, Any],
    evidence: list[dict[str, Any]],
    catalogue: str,
    prior_context: str = "",
) -> str:
    """Prompt for candidate-action proposals into a `CandidateList`.

    `catalogue` is `catalogue_view()` output - the registry rendered as the
    action menu. `hypothesis` is the recorded diagnosis the candidates must
    address; every rationale should cite evidence ids the diagnosis itself
    leaned on. `prior_context` carries the Knowledge Agent's organizational
    context (2.8) and is empty when the graph is off, so a context-free run
    renders byte-identically to before."""
    return f"""You are the Helix Recovery Planner. The Diagnosis Agent has
recorded a root-cause hypothesis; propose candidate recovery actions.

{_incident_block(incident)}

Recorded hypothesis:
- claim: {hypothesis.get("claim")}
- confidence: {hypothesis.get("confidence")}
- cited evidence: {", ".join(hypothesis.get("evidence_refs") or []) or "(none)"}
- contradicting signals: {hypothesis.get("contradicting_signals") or "(none)"}

Collected evidence (cite by id):
{_evidence_block(evidence)}

{prior_context}The bounded action catalogue - these are the ONLY actions that exist:
{catalogue}

Rules:
- Propose 1 to 4 candidates, most promising first (your order is a hint;
  a deterministic ranker makes the final call).
- Each candidate: action (a catalogue name exactly), params, rationale
  explaining how it remediates the hypothesized cause, evidence_refs.
- evidence_refs: ONLY evidence ids from the list above that the rationale
  actually leans on. Never cite an id you were not shown.
- Respect the parameter bounds exactly; out-of-bounds proposals are
  dropped unranked:
{_PARAM_RULES}- Prefer the smallest action that remediates the diagnosed cause. Do not
  propose actions against systems the evidence does not implicate.
- If no catalogue action can safely remediate this hypothesis, return an
  empty candidates list - that is the escalation signal, not an error.
"""
