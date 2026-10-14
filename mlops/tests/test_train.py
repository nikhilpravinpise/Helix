"""OPT-1 exit criterion: the pipeline trains and evaluates reproducibly and
reports honest accuracy against a held-out set, including when it is poor.

Synthetic history with a known rule (restarting a crash fixes it, nothing
else does) checks the pipeline learns it; a coin-flip history checks the
report says plainly when the model is no better than guessing.
"""

import csv
import json
import random

import train


def _history(path, n_incidents, label):
    rng = random.Random(7)
    with path.open("w", newline="") as handle:
        # The export's own columns: arm is in the data even though the model
        # does not use it as a feature.
        writer = csv.DictWriter(
            handle, fieldnames=sorted(set(train.FEATURES) | {"arm", "incident_id", "fixed"})
        )
        writer.writeheader()
        for i in range(n_incidents):
            fault = rng.choice(["kubernetes:CrashLoopBackOff", "jenkins:Test"])
            action = rng.choice(["restart_deployment", "rerun_pipeline", "open_fix_pr"])
            writer.writerow(
                {
                    "incident_id": f"INC-{i}",
                    "fault": fault,
                    "action": action,
                    "arm": rng.choice(["A", "E"]),
                    "risk_tier": "low",
                    "environment": "sandbox",
                    "kind": "recovery",
                    "attempt": 0,
                    "fixed": label(fault, action, rng),
                }
            )


def test_a_learnable_rule_beats_the_baseline_on_held_out_incidents(tmp_path):
    data = tmp_path / "history.csv"
    _history(
        data,
        240,
        lambda f, a, r: int(f == "kubernetes:CrashLoopBackOff" and a == "restart_deployment"),
    )
    result = train.run(data, tmp_path / "models", git_sha="test")
    metrics = result["metrics"]
    assert metrics["holdout_rows"] > 0 and metrics["train_rows"] > metrics["holdout_rows"]
    assert metrics["accuracy"] > metrics["baseline_accuracy"]
    assert metrics["verdict"] == "beats baseline"
    version = tmp_path / "models" / result["version"]
    assert (version / "model.pkl").exists()
    assert json.loads((version / "metrics.json").read_text())["git_sha"] == "test"
    latest = json.loads((tmp_path / "models" / "latest.json").read_text())
    assert latest["version"] == result["version"]
    table = json.loads((tmp_path / "models" / "advisory.json").read_text())["table"]
    assert table["kubernetes:CrashLoopBackOff|restart_deployment"]["p_fixed"] > 0.5
    assert table["jenkins:Test|open_fix_pr"]["p_fixed"] < 0.5


def test_noise_is_reported_as_no_better_than_guessing(tmp_path):
    data = tmp_path / "history.csv"
    _history(data, 240, lambda f, a, r: int(r.random() < 0.5))
    metrics = train.run(data, tmp_path / "models", git_sha="test")["metrics"]
    assert metrics["verdict"] == "no better than baseline"


def test_the_same_data_gives_the_same_split_and_version_data_hash(tmp_path):
    data = tmp_path / "history.csv"
    _history(data, 120, lambda f, a, r: int(a == "restart_deployment"))
    first = train.run(data, tmp_path / "a", git_sha="x")["metrics"]
    second = train.run(data, tmp_path / "b", git_sha="x")["metrics"]
    assert first["data_sha256"] == second["data_sha256"]
    assert first["holdout_incidents"] == second["holdout_incidents"]
    assert first["accuracy"] == second["accuracy"]


def test_arms_that_cannot_record_a_fix_are_left_out_and_arm_is_not_a_feature(tmp_path):
    """Arms B, C and D have no verify step, so they never reach recovered
    whatever action they pick: every one of their rows is labelled "not
    fixed". Trained on them, the model learned which benchmark arm ran (0.949
    held-out) rather than which action fixes a fault (0.667 without arm).
    Production runs one arm, so arm carries no information there either."""
    data = tmp_path / "history.csv"
    rng = random.Random(3)
    with data.open("w", newline="") as handle:
        fields = sorted(set(train.FEATURES) | {"arm", "incident_id", "fixed"})
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for i in range(200):
            arm = rng.choice(["A", "B", "C", "D", "E"])
            action = rng.choice(["restart_deployment", "open_fix_pr"])
            fixed = int(arm in ("A", "E") and action == "restart_deployment")
            writer.writerow(
                {
                    "incident_id": f"INC-{i}",
                    "fault": "kubernetes:CrashLoopBackOff",
                    "action": action,
                    "arm": arm,
                    "risk_tier": "low",
                    "environment": "sandbox",
                    "kind": "recovery",
                    "attempt": 0,
                    "fixed": fixed,
                }
            )
    assert "arm" not in train.FEATURES
    metrics = train.run(data, tmp_path / "models", git_sha="t")["metrics"]
    import pandas as pd

    frame = pd.read_csv(data)
    expected_excluded = int(frame["arm"].isin(["B", "C", "D"]).sum())
    assert metrics["excluded_rows"] == expected_excluded
    assert metrics["train_rows"] + metrics["holdout_rows"] == len(frame) - expected_excluded
