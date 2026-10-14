"""The OPT-1 model's estimate, read for display only.

`mlops/train.py` writes an advisory table: for each (fault, action), the
model's chance that the action fixes the incident, and how many past
actions that rests on. `helix provenance` shows it beside a decision.

Advisory means exactly that. Nothing that diagnoses, plans, applies policy
or executes reads this module (a test enforces it), so the model can never
choose, rank or allow an action (plan.md OPT-1).
"""

import json
from pathlib import Path
from typing import Any

from helix.config import get_settings

REPO_ROOT = Path(__file__).resolve().parents[1]


def _path() -> Path:
    configured = Path(get_settings().advisory_path)
    return configured if configured.is_absolute() else REPO_ROOT / configured


def advice_for(fault: str, action: str) -> dict[str, Any] | None:
    """The model's estimate for `action` against `fault`, or None when no
    model has been trained or it has never seen that pair."""
    path = _path()
    if not path.is_file():
        return None
    try:
        advisory = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    entry = (advisory.get("table") or {}).get(f"{fault}|{action}")
    if entry is None:
        return None
    return {
        "p_fixed": entry["p_fixed"],
        "n": entry["n"],
        "version": advisory.get("version"),
        "verdict": advisory.get("verdict"),
        "accuracy": advisory.get("accuracy"),
        "baseline_accuracy": advisory.get("baseline_accuracy"),
    }
