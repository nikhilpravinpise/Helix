"""`open_fix_pr` verification leg (subphase 1.9).

The PR IS the remediation for a persistent fix (CONTEXT.md section 3:
"execute bounded actions OR create a GitHub PR") - no runtime leg applies.
Desired state: the PR exists and carries the intended files.

Two honest paths:

- live: the executions row's `output` carries `pr_number` - the check
  reads the PR back through the github evidence capability and asserts it
  is open on the intended helix/INC-* branch, plus that the recorded
  write covered every intended path.
- fixture: the recording fake's write IS the recorded PR - the check
  asserts `output` carries the intended branch and file paths (the
  recorded intended write is what the benchmark scores, D10).

A mismatch - wrong branch, missing intended files, a PR that reads back
closed/missing - is a real fail. An executions row with no PR
coordinates at all is a fail too: there is nothing to verify as done.
"""

from typing import Any

from agent.verification import CheckResult, CheckStatus, VerificationContext
from integrations.evidence import EvidenceNotFoundError


def _recorded_paths(output: dict[str, Any]) -> set[str]:
    """File paths the write transport recorded - the live client stores
    plain path strings, the recording fake stores {path, bytes} dicts."""
    paths = set()
    for item in output.get("files") or []:
        paths.add(item["path"] if isinstance(item, dict) else str(item))
    return paths


def run(
    ctx: VerificationContext,
    params: dict[str, Any],
    execution_output: dict[str, Any] | None,
) -> CheckResult:
    """`fix_pr_exists`: the recorded/opened PR exists with intended files."""
    name = "fix_pr_exists"
    output = execution_output or {}
    intended_branch = str(params.get("branch") or "")
    intended_files = sorted(f.get("path") for f in params.get("files") or [] if f.get("path"))

    pr_number = output.get("pr_number")
    recorded = output.get("recorded") and output.get("op") == "open_fix_pr"
    if pr_number is None and not recorded:
        return CheckResult(
            name,
            CheckStatus.FAIL,
            "execution output carries no PR coordinates - nothing to verify as done",
        )

    written = _recorded_paths(output)
    missing = [p for p in intended_files if p not in written]
    if missing:
        return CheckResult(
            name,
            CheckStatus.FAIL,
            f"executed write did not cover intended file(s): {', '.join(missing)}",
        )
    if output.get("branch") != intended_branch:
        return CheckResult(
            name,
            CheckStatus.FAIL,
            f"executed write branch {output.get('branch')!r} != intended {intended_branch!r}",
        )

    if pr_number is not None:
        # Live mode: read the PR back through the evidence capability.
        try:
            evidence = ctx.github.get_pull_request(int(pr_number))
        except EvidenceNotFoundError:
            return CheckResult(name, CheckStatus.FAIL, f"recorded PR #{pr_number} does not exist")
        p = evidence.payload
        problems = []
        if p.get("state") != "open":
            problems.append(f"state is {p.get('state')!r}, expected 'open'")
        if p.get("head_ref") != intended_branch:
            problems.append(f"head is {p.get('head_ref')!r}, expected {intended_branch!r}")
        if p.get("merged"):
            problems.append("already merged")
        if problems:
            return CheckResult(name, CheckStatus.FAIL, "; ".join(problems), evidence.id)
        return CheckResult(
            name, CheckStatus.PASS, f"PR #{pr_number} open on {intended_branch}", evidence.id
        )

    # Fixture mode: the recorded intended write is the PR record.
    return CheckResult(
        name,
        CheckStatus.PASS,
        f"recorded fix PR on {intended_branch} covering {len(written)} file(s) (fixture write)",
    )
