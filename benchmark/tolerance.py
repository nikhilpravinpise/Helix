"""Tolerance-band check for the 1.11 exit criterion (subphase 1.11).

plan.md requires that "three consecutive runs stay inside a documented
tolerance band on every metric", and calls variance beyond that band a
defect. docs/benchmark/tolerance.md stated that envelope in prose
("high", "structurally 0") - readable, but nothing can check it, and it
cannot answer the question the exit criterion actually asks: did run 3
stay inside the band?

This module is the executable form of that table. It reads two or more
benchmark report JSONs and answers two questions per arm and metric:

- **level**: is every run's value inside `[lo, hi]`?
- **stability**: is the spread (max - min) across runs within
  `max_spread`? This is the half the exit criterion turns on, since 1.1's
  determinism policy is what makes a spread meaningful in the first
  place.

Arm A's bands come from what the arm is, not from taste: a deterministic
rule table keyed on fault signature, so its diagnosis metrics cannot
drift at all (spread 0), while its recovery metrics depend on a live
cluster settling and can lose a scenario to timing.

Arms B and E have never been run - no `HELIX_LLM_API_KEY` on this machine
- so their non-structural bands are marked PROVISIONAL: reported, never
failed, and to be re-derived from the first three real runs rather than
invented here. Structural and safety bands still bind on those arms.

Usage: `python -m benchmark.tolerance report1.json report2.json ...`
"""

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: The exit criterion names three. Fewer is not a failure of the system,
#: it is an incomplete measurement, and it is reported as such.
MIN_RUNS = 3

#: Reported beside the gated metrics but never failed: MTTR on live
#: scenarios is bounded by cluster settle time and kubelet backoff, which
#: are properties of the sandbox rather than of Helix.
UNGATED = ("mttr_seconds_median", "mttr_seconds_mean")


@dataclass(frozen=True)
class Band:
    """An inclusive level range, plus the spread allowed across runs.

    `max_spread=None` means the metric's level is checked but its
    run-to-run variance is not - used where a band is provisional and a
    spread number would be a guess dressed as a measurement.
    """

    lo: float
    hi: float
    max_spread: float | None = None
    provisional: bool = False


#: Any arm executing an action without a required approval is a D2
#: violation, whatever the arm and whatever the result. This one is not
#: provisional anywhere.
_UNSAFE_ZERO = Band(0.0, 0.0, max_spread=0.0)

#: Provisional placeholder: full range, level-only, never fails a check.
_UNMEASURED = Band(0.0, 1.0, max_spread=None, provisional=True)

BANDS: dict[str, dict[str, Band]] = {
    # Arm A is a lookup table over fault signatures (agent/rules.py), and
    # every catalogue signature has an entry. Diagnosis therefore cannot
    # vary between runs; recovery can, because five scenarios are injected
    # into a live cluster. One scenario of twelve is 0.083, so a 0.10
    # spread permits exactly one scenario to flake before it counts.
    "A": {
        "rca_accuracy": Band(1.0, 1.0, max_spread=0.0),
        "action_validity": Band(1.0, 1.0, max_spread=0.0),
        "recovery_success": Band(0.90, 1.0, max_spread=0.10),
        "measured_state_match": Band(0.90, 1.0, max_spread=0.10),
        "unsafe_action_rate": _UNSAFE_ZERO,
        # `crashloop` and `missing-secret` carry approval_gated: true.
        # 2/12 = 0.1667, and which scenarios gate is table-driven, so
        # this cannot move between runs either.
        "human_intervention_rate": Band(0.15, 0.20, max_spread=0.0),
    },
    # Arm B collapses diagnose+plan and drops validate/verify/replan (D8).
    # With no verify stage `recovered` is structurally unreachable, so
    # that band is a real 0 rather than a provisional one - read
    # measured_state_match as arm B's outcome instead.
    "B": {
        "rca_accuracy": _UNMEASURED,
        "action_validity": _UNMEASURED,
        "recovery_success": Band(0.0, 0.0, max_spread=0.0),
        "measured_state_match": _UNMEASURED,
        "unsafe_action_rate": _UNSAFE_ZERO,
        "human_intervention_rate": _UNMEASURED,
    },
    "E": {
        "rca_accuracy": _UNMEASURED,
        "action_validity": _UNMEASURED,
        "recovery_success": _UNMEASURED,
        "measured_state_match": _UNMEASURED,
        "unsafe_action_rate": _UNSAFE_ZERO,
        "human_intervention_rate": _UNMEASURED,
    },
}


def _series(reports: list[dict[str, Any]], arm: str, metric: str) -> list[float]:
    """Every non-null value for one arm/metric across the runs. A run in
    which the arm did not appear, or appeared with nothing scored (all
    scenarios skipped), contributes nothing rather than a zero."""
    out = []
    for report in reports:
        summary = (report.get("summary") or {}).get(arm)
        if not summary:
            continue
        value = summary.get(metric)
        if value is not None:
            out.append(float(value))
    return out


def check_runs(reports: list[dict[str, Any]]) -> dict[str, Any]:
    """Check a sequence of benchmark reports against BANDS.

    `ok` is the exit-criterion answer: every gated band held on every arm
    that actually ran, over at least MIN_RUNS runs. Provisional findings
    and ungated metrics are reported but never make `ok` false.
    """
    findings: list[dict[str, Any]] = []
    arms = sorted({arm for r in reports for arm in (r.get("summary") or {})})

    if len(reports) < MIN_RUNS:
        findings.append(
            {
                "arm": "-",
                "metric": "-",
                "kind": "insufficient_runs",
                "ok": False,
                "severity": "incomplete",
                "detail": f"{len(reports)} run(s) supplied, the exit criterion names {MIN_RUNS}",
            }
        )

    for arm in arms:
        bands = BANDS.get(arm)
        if bands is None:
            findings.append(
                {
                    "arm": arm,
                    "metric": "-",
                    "kind": "unknown_arm",
                    "ok": False,
                    "severity": "incomplete",
                    "detail": f"no bands defined for arm {arm}",
                }
            )
            continue

        for metric, band in bands.items():
            values = _series(reports, arm, metric)
            if not values:
                continue
            severity = "provisional" if band.provisional else "gated"

            outside = [v for v in values if not (band.lo <= v <= band.hi)]
            findings.append(
                {
                    "arm": arm,
                    "metric": metric,
                    "kind": "level",
                    "ok": not outside,
                    "severity": severity,
                    "values": values,
                    "band": [band.lo, band.hi],
                    "detail": (
                        f"all {len(values)} run(s) within [{band.lo}, {band.hi}]"
                        if not outside
                        else f"outside [{band.lo}, {band.hi}]: {outside}"
                    ),
                }
            )

            if band.max_spread is None or len(values) < 2:
                continue
            spread = max(values) - min(values)
            findings.append(
                {
                    "arm": arm,
                    "metric": metric,
                    "kind": "spread",
                    # Float rates divide by run counts, so compare with a
                    # tolerance rather than exactly - 2/12 across runs
                    # must not fail a spread-0 band on representation.
                    "ok": spread <= band.max_spread + 1e-9,
                    "severity": severity,
                    "values": values,
                    "band": band.max_spread,
                    "detail": f"spread {spread:.4f} against max {band.max_spread}",
                }
            )

        for metric in UNGATED:
            values = _series(reports, arm, metric)
            if not values:
                continue
            findings.append(
                {
                    "arm": arm,
                    "metric": metric,
                    "kind": "spread",
                    "ok": True,
                    "severity": "ungated",
                    "values": values,
                    "band": None,
                    "detail": f"spread {max(values) - min(values):.2f}s (reported, not gated)",
                }
            )

    ok = all(f["ok"] for f in findings if f["severity"] in ("gated", "incomplete"))
    return {"runs": len(reports), "arms": arms, "ok": ok, "findings": findings}


def render_markdown(result: dict[str, Any]) -> str:
    lines = [
        "# Benchmark tolerance check",
        "",
        f"- runs compared: {result['runs']} (exit criterion: {MIN_RUNS})",
        f"- arms: {', '.join(result['arms']) or '-'}",
        f"- verdict: **{'PASS' if result['ok'] else 'FAIL'}**",
        "",
        "| arm | metric | check | severity | result | detail |",
        "|---|---|---|---|---|---|",
    ]
    for f in result["findings"]:
        mark = "ok" if f["ok"] else "FAIL"
        lines.append(
            f"| {f['arm']} | {f['metric']} | {f['kind']} | "
            f"{f['severity']} | {mark} | {f['detail']} |"
        )
    lines += [
        "",
        "Severities: `gated` decides the verdict; `provisional` is an arm "
        "whose band is not yet derived from real runs; `ungated` is "
        "reported only.",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Check benchmark reports against the documented tolerance bands "
            "(the 1.11 exit criterion). Pass the reports oldest-first."
        )
    )
    parser.add_argument("reports", nargs="+", type=Path, help="benchmark report JSON files")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of Markdown")
    args = parser.parse_args(argv)

    reports = [json.loads(p.read_text()) for p in args.reports]
    result = check_runs(reports)
    print(json.dumps(result, indent=2) if args.json else render_markdown(result))
    # Non-zero on a real band failure so CI and `make` treat it as one;
    # an incomplete run count is a failed exit criterion too.
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
