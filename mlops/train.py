"""OPT-1: train, evaluate and version a model over Helix incident history.

Input is `helix export-history` CSV: one row per remediation action, with
whether it fixed the incident. The model estimates, for a fault and an
action, the chance the action fixes it. Output is advisory only: Helix
shows it beside a decision and never uses it to choose, rank or allow an
action (plan.md OPT-1).

Honest evaluation:
- The held-out set is split by incident, not by row, so no incident's
  actions sit on both sides of the split.
- Accuracy is reported beside a baseline that always guesses the most
  common outcome, and the verdict says plainly when the model is no
  better than that.
- Everything is seeded, so the same data gives the same split and score.

Each run writes models/<version>/ (model.pkl, metrics.json, advisory.json)
and points models/latest.json and models/advisory.json at it.
"""

import argparse
import hashlib
import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
from sklearn.model_selection import GroupShuffleSplit

FEATURES = ["fault", "action", "arm", "risk_tier", "environment", "kind", "attempt"]
TARGET = "fixed"
SEED = 42
HOLDOUT = 0.25
#: The model must beat the baseline by more than this to count as better;
#: a hair's difference on a small held-out set is noise.
MARGIN = 0.02


def _split(frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    splitter = GroupShuffleSplit(n_splits=1, test_size=HOLDOUT, random_state=SEED)
    train_idx, hold_idx = next(splitter.split(frame, groups=frame["incident_id"]))
    return frame.iloc[train_idx], frame.iloc[hold_idx]


def _advisory_table(model, frame: pd.DataFrame) -> dict[str, dict[str, float]]:
    """Mean predicted chance of a fix per (fault, action), with how many
    past actions it rests on."""
    from pycaret.classification import predict_model

    scored = predict_model(model, data=frame[FEATURES], raw_score=True, verbose=False)
    scored["fault"], scored["action"] = frame["fault"].values, frame["action"].values
    table = {}
    for (fault, action), group in scored.groupby(["fault", "action"]):
        table[f"{fault}|{action}"] = {
            "p_fixed": round(float(group["prediction_score_1"].mean()), 3),
            "n": int(len(group)),
        }
    return table


def run(data: Path, out: Path, *, git_sha: str = "unknown") -> dict:
    from pycaret.classification import (
        compare_models,
        predict_model,
        save_model,
        setup,
    )

    raw = data.read_bytes()
    frame = pd.read_csv(data)
    frame["attempt"] = frame["attempt"].astype(int)
    train, holdout = _split(frame)

    setup(
        data=train[FEATURES + [TARGET]],
        test_data=holdout[FEATURES + [TARGET]],
        target=TARGET,
        categorical_features=[f for f in FEATURES if f != "attempt"],
        session_id=SEED,
        fold=3,
        verbose=False,
        html=False,
        system_log=False,  # PyCaret otherwise writes logs.log into the cwd
    )
    model = compare_models(include=["lr", "dt", "rf", "nb"], sort="Accuracy", verbose=False)

    scored = predict_model(model, data=holdout[FEATURES], verbose=False)
    truth = holdout[TARGET].to_numpy()
    accuracy = float((scored["prediction_label"].to_numpy() == truth).mean())
    majority = int(train[TARGET].mode().iloc[0])
    baseline = float((truth == majority).mean())

    data_sha = hashlib.sha256(raw).hexdigest()
    version = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + "-" + data_sha[:8]
    metrics = {
        "version": version,
        "model": type(model).__name__,
        "train_rows": int(len(train)),
        "holdout_rows": int(len(holdout)),
        "holdout_incidents": sorted(holdout["incident_id"].unique().tolist()),
        "accuracy": round(accuracy, 4),
        "baseline_accuracy": round(baseline, 4),
        "verdict": (
            "beats baseline" if accuracy > baseline + MARGIN else "no better than baseline"
        ),
        "data_sha256": data_sha,
        "git_sha": git_sha,
        "trained_at": datetime.now(UTC).isoformat(),
    }

    target_dir = out / version
    target_dir.mkdir(parents=True, exist_ok=True)
    save_model(model, str(target_dir / "model"), verbose=False)
    advisory = {
        "version": version,
        "verdict": metrics["verdict"],
        "accuracy": metrics["accuracy"],
        "baseline_accuracy": metrics["baseline_accuracy"],
        "table": _advisory_table(model, frame),
    }
    (target_dir / "metrics.json").write_text(json.dumps(metrics, indent=2))
    (target_dir / "advisory.json").write_text(json.dumps(advisory, indent=2))
    (out / "latest.json").write_text(json.dumps({"version": version}, indent=2))
    shutil.copyfile(target_dir / "advisory.json", out / "advisory.json")
    return {"version": version, "metrics": metrics}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", required=True, type=Path, help="helix export-history CSV")
    parser.add_argument("--out", default=Path("models"), type=Path, help="models directory")
    parser.add_argument("--git-sha", default="unknown")
    args = parser.parse_args(argv)
    result = run(args.data, args.out, git_sha=args.git_sha)
    m = result["metrics"]
    print(
        f"model {result['version']} ({m['model']}): held-out accuracy {m['accuracy']:.3f} "
        f"vs baseline {m['baseline_accuracy']:.3f} on {m['holdout_rows']} rows - {m['verdict']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
