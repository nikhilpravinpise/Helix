"""Diagnosis Agent prompt templates - single-sourced here (subphase 1.5).

Two prompts, each answered via `complete_structured` so the model never
produces free text an agent must parse:

- `evidence_request_prompt`: the bounded request-more-evidence round. The
  model sees only evidence SUMMARIES (id + kind + summary) and may ask for
  more evidence from a fixed requestable menu. Full payloads stay
  retrievable by id, so prompts stay bounded.
- `diagnosis_prompt`: the final interpretation into `Hypothesis`. The
  model must cite collected evidence ids in `evidence_refs` - citing an
  id it was never shown is a structured-validation failure at the call
  site (one re-prompt, then a low-confidence record).
"""

from typing import Any

#: The (source, kind) pairs an evidence request may ask for, rendered into
#: the prompt so the model can only choose evidence the dispatcher in
#: agents/diagnosis.py can actually fetch.
_REQUESTABLE = """\
- kubernetes pod_list: pods in the incident namespace (target: service name, optional)
- kubernetes pod_status: one pod's detail (target: pod name)
- kubernetes event_list: namespace events (target: involved object name, optional)
- kubernetes secret_info: Secret metadata, names/keys only (target: secret name)
- elk log_lines: service logs (target: service; detail: window like 15m|1h|24h, optional)
- jenkins build_console: console log (target/detail: job and build number, optional)
- jenkins test_results: test report (target/detail: job and build number, optional)
- github diff: commit diff (target: commit sha)
- github commit: commit metadata (target: commit sha)
- docker image_info: host image state (target: image name)
- docker container_state: host container state (target: container name)
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
    """One line per collected item: `id | source | kind | summary`.

    Only summaries enter the prompt - payloads stay retrievable by id -
    so prompt size stays bounded regardless of evidence volume."""
    if not evidence:
        return "No evidence collected yet."
    return "\n".join(
        f"- {e['id']} | {e['source']} | {e['kind']} | {e['summary']}" for e in evidence
    )


def evidence_request_prompt(incident: dict[str, Any], evidence: list[dict[str, Any]]) -> str:
    """Prompt for one bounded request-more-evidence round.

    The model answers with an `EvidencePlan`: `requests` is a list of
    {source, kind, target, detail, rationale}; an empty list means it has
    enough evidence to interpret."""
    return f"""You are the Helix Diagnosis Agent investigating a failure.

{_incident_block(incident)}

Evidence collected so far:
{_evidence_block(evidence)}

You may request additional evidence from this fixed menu only:
{_REQUESTABLE}
Rules:
- Request only evidence the menu can produce; cite a concrete target when
  the menu entry needs one (a pod, secret, service, job or sha).
- Do not re-request evidence already listed above.
- If the evidence you have is enough to form a root-cause hypothesis,
  return an empty requests list.
"""


def diagnosis_prompt(
    incident: dict[str, Any],
    evidence: list[dict[str, Any]],
    *,
    invalid_refs: list[str] | None = None,
) -> str:
    """Prompt for the final interpretation into a `Hypothesis`.

    `invalid_refs` is set on the single re-prompt after the model cited
    evidence ids it was never shown - the prompt then names the rejected
    ids so the retry can correct itself."""
    retry = ""
    if invalid_refs:
        retry = (
            "\nYour previous answer cited evidence ids that were never collected: "
            f"{', '.join(invalid_refs)}. Cite only ids listed above; drop any claim "
            "you cannot support with them.\n"
        )
    return f"""You are the Helix Diagnosis Agent. Form a root-cause hypothesis for this failure.

{_incident_block(incident)}

Collected evidence (cite by id):
{_evidence_block(evidence)}
{retry}
Rules:
- claim: one or two sentences naming the most likely root cause.
- evidence_refs: ONLY ids from the list above that directly support the
  claim. Never cite an id you were not shown.
- confidence: 0.0 to 1.0 - honest, lower when evidence is thin or mixed.
- contradicting_signals: evidence ids or short descriptions of evidence
  that argues against the claim.
"""
