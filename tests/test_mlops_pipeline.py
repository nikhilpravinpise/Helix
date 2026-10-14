"""OPT-1: training runs through Jenkins as its own job.

Static guards only; the proof is a live run of the helix-mlops job. These
keep the job declared, the stages in place, and secrets out of its
workspace.
"""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_jenkins_declares_the_mlops_job():
    config = yaml.safe_load((ROOT / "infra/jenkins/jenkins.yaml").read_text())
    scripts = " ".join(job["script"] for job in config["jobs"])
    assert "pipelineJob('helix-mlops')" in scripts
    assert "infra/jenkins/Jenkinsfile.mlops" in scripts


def test_the_pipeline_exports_trains_and_archives():
    text = (ROOT / "infra/jenkins/Jenkinsfile.mlops").read_text()
    for stage in ("Connect", "Prepare", "Export", "Train", "Archive"):
        assert f"stage('{stage}')" in text
    assert "helix export-history" in text and "train.py" in text


def test_no_secret_reaches_the_jenkins_workspace():
    text = (ROOT / "infra/jenkins/Jenkinsfile.mlops").read_text()
    assert "--exclude=./.env" in text
    assert "HELIX_ENV_FILE     = ''" in text
