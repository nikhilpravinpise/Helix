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
        writer = csv.DictWriter(handle, fieldnames=train.FEATURES + ["incident_id", "fixed"])
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
