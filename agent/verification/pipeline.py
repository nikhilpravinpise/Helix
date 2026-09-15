"""`rerun_pipeline` verification leg (subphase 1.9).

Desired state after a pipeline re-run: the job's LATEST build is green.
Sampled across the stability window like every leg - a queued build may
still be `BUILDING` on the first samples (that is the grace period), a
build that goes FAILURE is a real fail, and a fixture without jenkins_*
fields reports skipped rather than a fabricated verdict.
"""

from dataclasses import dataclass

from agent.verification import CheckStatus, Sample
from integrations.capabilities import JenkinsEvidence
from integrations.evidence import EvidenceNotFoundError

#: Jenkins result strings that mean the build is still running.
_BUILDING_RESULTS: frozenset[str] = frozenset({"BUILDING"})


@dataclass
class BuildGreenCheck:
    """`build_green`: the job's newest build finished SUCCESS."""

    jenkins: JenkinsEvidence
    job: str
    fixture: bool = False

    name: str = "build_green"

    def check(self) -> Sample:
        try:
            job_ev = self.jenkins.get_job(self.job)
            last_build = job_ev.payload.get("last_build")
            if last_build is None:
                return Sample(CheckStatus.FAIL, f"job {self.job} has no builds", job_ev.id)
            build = self.jenkins.get_build_status(self.job, int(last_build))
        except EvidenceNotFoundError as exc:
            if self.fixture:
                return Sample(CheckStatus.SKIPPED, f"no jenkins evidence recorded: {exc}")
            return Sample(CheckStatus.FAIL, f"jenkins lookup failed: {exc}")
        result = build.payload.get("result")
        if result == "SUCCESS":
            return Sample(CheckStatus.PASS, f"build {self.job}#{last_build} SUCCESS", build.id)
        if result in _BUILDING_RESULTS or build.payload.get("building"):
            return Sample(
                CheckStatus.FAIL, f"build {self.job}#{last_build} still running", build.id
            )
        return Sample(
            CheckStatus.FAIL,
            f"build {self.job}#{last_build} finished {result or 'unknown'}",
            build.id,
        )
