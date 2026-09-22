"""The Ops Agent boundary (subphase 2.1).

CONTEXT.md section 9 names five Phase 2 agents; Ops is "what failed and
what is the safest recovery action" - exactly the Phase 1 recovery loop
(Diagnosis, Planner, Validation, Execution, Verification, Compensation)
that already exists under `agent/agents/`. 2.1's deliverable is
consolidating those five node-builders under one accessible boundary
WITHOUT changing their behavior - "a pure structural move" whose exit
criterion is that the Phase 1 benchmark passes unchanged.

This module is that boundary: a facade, not a relocation. Every
`agent.agents.*` module Phase 1 built stays exactly where it is, with its
own tests unmodified - `ops_agent_nodes()` just names them as one group so
Phase 2 code (the multi-agent Coordinator's handoff bookkeeping, the
Triage/Review/Knowledge agents that sit alongside Ops) can depend on "the
Ops Agent" as a concept without importing five separate modules to find
it, and so a reader has one place that states which nodes belong to which
CONTEXT.md section 9 agent.
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent.graph import GraphDeps

#: The Ops Agent's node-builder registry: catalogue key -> the exact
#: builder function `agent/graph.py` already wires for arm E/A. Kept as a
#: lazy dict-builder (not a module-level import) so this module carries no
#: import-order dependency on `agent.graph` - the same reason
#: `agent/graph.py` itself defers its arm-specific imports inline.
_OPS_NODE_BUILDERS = (
    "diagnosis",
    "plan",
    "validate",
    "execute",
    "verify",
    "compensate",
)


def ops_agent_nodes(deps: "GraphDeps") -> dict[str, Any]:
    """Build every Ops Agent node (arm E's full recovery loop) keyed by
    its graph node name. Internals unchanged: each builder is the exact
    1.5-1.10 function `agent/graph.py` already calls; this only groups
    the result under one call so a caller can ask for "the Ops Agent"
    once instead of importing six modules."""
    from agent.agents.compensation import compensation_node
    from agent.agents.diagnosis import diagnosis_node
    from agent.agents.execution import execution_node
    from agent.agents.planner import planner_node
    from agent.agents.validation import validation_node
    from agent.agents.verification import verification_node

    return {
        "diagnosis": diagnosis_node(deps),
        "plan": planner_node(deps),
        "validate": validation_node(deps),
        "execute": execution_node(deps),
        "verify": verification_node(deps),
        "compensate": compensation_node(deps),
    }


def is_ops_node(name: str) -> bool:
    """Whether `name` is one of the Ops Agent's own graph nodes - the
    duplicate-action / handoff-ordering check the multi-agent Coordinator
    (2.1) uses to know when control is inside Ops vs. Triage/Review/
    Knowledge."""
    return name in _OPS_NODE_BUILDERS or name in (
        "diagnose_and_plan",
        "rules_diagnosis",
        "rules_plan",
    )
