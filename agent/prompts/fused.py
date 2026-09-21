"""Arm-B fused prompt (subphase 1.11, decision D8).

`fused_prompt` asks the model for a `FusedDiagnosisPlan` in ONE
`complete_structured` call: hypothesis and selected action together. This
is the deliberately weaker contrast arm - diagnosis and planning are not
separate stages, there is no evidence-request loop, no ranker, no
validation or verification downstream. The catalogue menu is still
rendered from `catalogue_view()` so the action surface cannot drift, and
the same param rules text applies.
"""

from typing import Any

from agent.prompts.diagnosis import _evidence_block, _incident_block
from agent.prompts.planning import _PARAM_RULES


def fused_prompt(
    incident: dict[str, Any],
    evidence: list[dict[str, Any]],
    menu: str,
    prior_context: str = "",
) -> str:
    """One call -> diagnosis AND action. The model answers with a single
    `FusedDiagnosisPlan`; an empty `action` is the honest "nothing safe to
    do" answer.

    `prior_context` carries retrieved prior incidents (E6) and is empty
    unless retrieval is enabled, so arm B's prompt is byte-identical to
    what it was before E6 and the ablation against arm C measures
    retrieval rather than a reworded prompt."""
    return f"""You are the Helix agent. Diagnose this failure and choose the \
remediation action in one answer.

{_incident_block(incident)}

Collected evidence (cite by id):
{_evidence_block(evidence)}
{prior_context}
Actions you may select (catalogue only):
{menu}

Param rules:
{_PARAM_RULES}
Rules:
- claim: one or two sentences naming the most likely root cause.
- evidence_refs: ONLY ids from the list above that support the claim.
- confidence: 0.0 to 1.0 - honest, lower when evidence is thin.
- action: a catalogue action name, or an empty string if nothing safe
  exists. params: the params object for that action.
- rationale / action_evidence_refs: why this action, and which evidence
  ids justify it.
"""
