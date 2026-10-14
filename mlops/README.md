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
  outcome; the verdict says "no better than baseline" when it is.
- First run (2026-10-14, 305 actions): held-out accuracy 0.949 vs baseline
  0.603 on 78 actions. Almost all of the history is benchmark-generated and
  repetitive, which flatters the score; it measures the pipeline, not how
  well the model would do on a real team's incidents.
