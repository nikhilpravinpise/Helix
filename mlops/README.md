# mlops - OPT-1 advisory model

A PyCaret model over Helix's incident history. For a fault and an action it
estimates the chance the action fixes the incident. `helix provenance` shows
that estimate beside a decision; nothing in Helix uses it to choose, rank or
allow an action.

This is its own uv project on Python 3.11: PyCaret 3.3.2 refuses Python 3.12
at import, and Helix is 3.12. Helix never imports it.

## Run it

```bash
uv run helix export-history --out data/history.csv     # from the repo root
cd mlops && uv run python train.py --data ../data/history.csv --out models
uv run pytest                                           # mlops tests, 3.11
```

Through Jenkins: the `helix-mlops` job (`infra/jenkins/Jenkinsfile.mlops`)
does the export, the training and archives the models. Start Jenkins with
`bash scripts/jenkins-up.sh`; the first time a rebuilt image adds the job,
it appears after a configuration reload.

## What a run produces

`models/<version>/` holds `model.pkl`, `metrics.json` (held-out accuracy,
baseline, verdict, train and held-out sizes, data sha256, git sha) and
`advisory.json` (per fault and action: the mean predicted chance of a fix and
how many past actions it rests on). `models/latest.json` and
`models/advisory.json` point at the newest version. `models/` is not
committed.

## Reading the numbers honestly

- The held-out quarter is split by incident, so no incident sits on both
  sides.
- Accuracy is always reported beside a baseline that guesses the most common
  outcome. "Beats baseline" needs a margin of two standard errors of the
  baseline's accuracy on the held-out rows; otherwise the verdict is "no
  better than baseline".
- Rows from ablation arms B, C and D are left out: they have no verify step,
  so they never reach recovered whatever action they pick. `arm` is not a
  feature: production runs one arm.

## Current result (2026-10-14, Jenkins build #4)

305 actions exported, 140 from arms B, C and D left out; 125 to train, 40
held out. Held-out accuracy 0.900 against a 0.850 baseline: **no better than
baseline**. Most remaining actions in this benchmark history succeeded, so
guessing "fixed" is already right 85% of the time, and the model has too
little varied data to do reliably better.

The first version reported 0.949 against 0.603. That number came from the
`arm` column (which benchmark arm ran), not from the action: arm A fixes
almost always and arms B, C and D never can. Removing them is why the score
fell. The pipeline meets OPT-1's exit criterion; the model will only become
useful with history from real incidents.
