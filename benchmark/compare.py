"""Direct-path vs event-path benchmark comparison (subphase E7).

plan.md E7 requires a "comparison of direct-path and event-path results"
and an exit criterion of "no regression against Phase 1 numbers". Phase 1
numbers already exist as `path: direct` reports (subphase 1.11); this
module diffs one of those against a `path: event` report, produced by the
same harness (`benchmark/harness.py --path event`), metric by metric.

Honest-by-construction, matching `benchmark.scoring` and
`benchmark.tolerance`:

- A metric present on one side and missing on the other is NOT a pass. It
  is reported as incomparable, with its own count, never silently folded
  into either side of the verdict.
- Only metrics both sides actually measured (non-null on both) can move
  the regression verdict.

Regression is CONTEXT.md section 17's metrics read in the direction that
makes them worse: a lower `rca_accuracy`, `action_validity`,
`recovery_success`, or `measured_state_match` on the event path, or a
higher `unsafe_action_rate`. Any one comparable metric moving that way on
any arm is a regression.

Usage: `python -m benchmark.compare direct.json event.json`
"""

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

#: The metrics compared, and which direction of movement is a regression.
#: The other section-17 fields (attempted/skipped/runs counts, MTTR,
#: human_intervention_rate) describe what ran or how long it took rather
#: than whether the outcome was worse, so plan.md's "no regression"
#: language does not apply to them here.
METRICS: tuple[tuple[str, str], ...] = (
    ("rca_accuracy", "higher_is_better"),
    ("action_validity", "higher_is_better"),
    ("recovery_success", "higher_is_better"),
    ("measured_state_match", "higher_is_better"),
    ("unsafe_action_rate", "lower_is_better"),
    # Phase 2 (2.9/2.11): change-lifecycle outcome quality.
    ("owner_accuracy", "higher_is_better"),
    ("reviewer_accuracy", "higher_is_better"),
    ("lifecycle_consistency", "higher_is_better"),
    ("duplicate_work_item_rate", "lower_is_better"),
    ("decision_evidence_coverage", "higher_is_better"),
)


@dataclass(frozen=True)
class MetricComparison:
    """One arm/metric pair, both sides, and what moving between them means."""

    arm: str
    metric: str
    direct: float | None
    event: float | None
    #: event - direct. None whenever either side is missing - a delta
    #: computed against a missing value would be a fabricated number.
    delta: float | None
    #: True only when both sides measured this metric (neither is None).
    comparable: bool
    #: True only when comparable AND the event side moved the wrong way.
    regressed: bool
    detail: str


@dataclass(frozen=True)
class ComparisonResult:
    """The whole direct-vs-event diff, plus the overall verdict."""

    arms: list[str]
    comparisons: list[MetricComparison] = field(default_factory=list)
    #: True when any comparable metric regressed - the E7 exit-criterion
    #: answer.
    regressed: bool = False
    #: How many arm/metric pairs could not be judged either way.
    incomparable_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "arms": self.arms,
            "comparisons": [asdict(c) for c in self.comparisons],
            "regressed": self.regressed,
            "incomparable_count": self.incomparable_count,
        }


def _incomparable_detail(direct: float | None, event: float | None) -> str:
    if direct is None and event is None:
        return "incomparable: not measured on either path"
    if direct is None:
        return "incomparable: not measured on the direct path"
    return "incomparable: not measured on the event path"


def compare_reports(direct: dict[str, Any], event: dict[str, Any]) -> ComparisonResult:
    """Diff two harness report dicts (`benchmark.harness.run_benchmark`
    output) arm by arm, metric by metric.

    Arms are the union of both sides: an arm that ran on only one path is
    still worth reporting, and every one of its metrics is incomparable by
    construction rather than silently dropped.
    """
    direct_summary = direct.get("summary") or {}
    event_summary = event.get("summary") or {}
    arms = sorted(set(direct_summary) | set(event_summary))

    comparisons: list[MetricComparison] = []
    incomparable_count = 0
    regressed = False

    for arm in arms:
        direct_arm = direct_summary.get(arm) or {}
        event_arm = event_summary.get(arm) or {}
        for metric, direction in METRICS:
            direct_value = direct_arm.get(metric)
            event_value = event_arm.get(metric)
            comparable = direct_value is not None and event_value is not None

            if not comparable:
                incomparable_count += 1
                comparisons.append(
                    MetricComparison(
                        arm=arm,
                        metric=metric,
                        direct=direct_value,
                        event=event_value,
                        delta=None,
                        comparable=False,
                        regressed=False,
                        detail=_incomparable_detail(direct_value, event_value),
                    )
                )
                continue

            delta = event_value - direct_value
            metric_regressed = (
                event_value < direct_value
                if direction == "higher_is_better"
                else event_value > direct_value
            )
            if metric_regressed:
                regressed = True
            comparisons.append(
                MetricComparison(
                    arm=arm,
                    metric=metric,
                    direct=direct_value,
                    event=event_value,
                    delta=delta,
                    comparable=True,
                    regressed=metric_regressed,
                    detail=f"direct={direct_value:.4f} event={event_value:.4f} delta={delta:+.4f}",
                )
            )

    return ComparisonResult(
        arms=arms,
        comparisons=comparisons,
        regressed=regressed,
        incomparable_count=incomparable_count,
    )


def render_markdown(direct: dict[str, Any], event: dict[str, Any], result: ComparisonResult) -> str:
    """The human-readable form: one row per arm/metric pair, plus the
    verdict `benchmark.tolerance.render_markdown` states the same way."""
    lines = [
        "# Direct-path vs event-path comparison (subphase E7)",
        "",
        f"- direct report generated: {direct.get('generated_at', '-')}",
        f"- event report generated: {event.get('generated_at', '-')}",
        f"- arms: {', '.join(result.arms) or '-'}",
        f"- verdict: **{'REGRESSED' if result.regressed else 'NO REGRESSION'}**",
        f"- incomparable arm/metric pairs: {result.incomparable_count}",
        "",
        "| arm | metric | direct | event | delta | comparable | regressed |",
        "|---|---|---|---|---|---|---|",
    ]

    def _fmt(value: float | None) -> str:
        return "-" if value is None else f"{value:.4f}"

    for c in result.comparisons:
        lines.append(
            f"| {c.arm} | {c.metric} | {_fmt(c.direct)} | {_fmt(c.event)} | "
            f"{_fmt(c.delta)} | {'yes' if c.comparable else 'NO'} | "
            f"{'YES' if c.regressed else 'no'} |"
        )
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m benchmark.compare",
        description="Compare a direct-path and an event-path benchmark report "
        "(subphase E7) and report whether the event path regressed.",
    )
    parser.add_argument("direct", type=Path, help="direct-path report JSON")
    parser.add_argument("event", type=Path, help="event-path report JSON")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of Markdown")
    args = parser.parse_args(argv)

    direct = json.loads(args.direct.read_text())
    event = json.loads(args.event.read_text())
    result = compare_reports(direct, event)

    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        print(render_markdown(direct, event, result))

    # Non-zero on a real regression so CI and `make` treat it as one.
    return 1 if result.regressed else 0


if __name__ == "__main__":
    sys.exit(main())
