#!/usr/bin/env python3
"""Helper for capture-fixture.sh: assembles one fixture JSON file from the
raw kubectl/query-logs output already written to disk by the caller. Not a
fixture framework - just the JSON-assembly step that is awkward to do
correctly in bash (argv length limits, quoting) and is only ever called from
capture-fixture.sh. Nothing here alters or filters the captured content.
"""
import json
import sys

(
    out_file,
    fault,
    trigger_desc,
    captured_at,
    fault_script,
    fault_exit,
    fault_log_path,
    pods_pa_path,
    pods_ds_path,
    events_path,
    logs_pa_path,
    logs_ds_path,
) = sys.argv[1:13]


def read_text(path):
    with open(path) as f:
        return f.read()


def read_json(path):
    text = read_text(path)
    return json.loads(text) if text.strip() else None


fixture = {
    "fault": fault,
    "layer": "cluster",
    "captured_at": captured_at,
    "trigger": trigger_desc,
    "fault_script": fault_script,
    "fault_script_exit_code": int(fault_exit),
    "fault_script_output": read_text(fault_log_path),
    "evidence": {
        "pods_payment_api": read_json(pods_pa_path),
        "pods_downstream_stub": read_json(pods_ds_path),
        "events": read_json(events_path),
        "logs_payment_api": read_text(logs_pa_path).splitlines(),
        "logs_downstream_stub": read_text(logs_ds_path).splitlines(),
    },
}

with open(out_file, "w") as f:
    json.dump(fixture, f, indent=2, sort_keys=False)
    f.write("\n")

print(f"wrote {out_file}")
