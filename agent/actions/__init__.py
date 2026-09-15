"""Bounded action catalogue (subphase 1.6).

The catalogue is the only set of actions the planner may propose, the
validator may check, and the executor may run - "an attempt to execute
outside the action catalogue is rejected" starts here, at registration.
"""

from agent.actions.catalogue import (
    MANIFEST_ALLOWLIST,
    REPO_WRITE_ROOT,
    SECRET_MANIFEST_PATHS,
    ActionName,
    ActionSpec,
    Reversibility,
    RiskTier,
    all_actions,
    catalogue_view,
    get_action,
    params_model_for,
    register_action,
)

__all__ = [
    "MANIFEST_ALLOWLIST",
    "REPO_WRITE_ROOT",
    "SECRET_MANIFEST_PATHS",
    "ActionName",
    "ActionSpec",
    "Reversibility",
    "RiskTier",
    "all_actions",
    "catalogue_view",
    "get_action",
    "params_model_for",
    "register_action",
]
