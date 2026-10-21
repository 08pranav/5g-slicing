#!/usr/bin/env python3
"""Bundle saved experiment CSVs into dashboard/demo/data.json for the static demo.

    python3 dashboard/build_demo_data.py [results_dir]

Each run is converted to the per-second snapshots the live dashboard would have
streamed, using the same RunState logic as server.py.
"""
import csv
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import server  # noqa: E402

results = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else server.RESULTS
server.RESULTS = results


def r1(v):
    return None if v is None else round(v, 1)


def snapshots(name, algo, case):
    with open(os.path.join(results, name)) as f:
        rows = sorted(csv.DictReader(f), key=lambda r: float(r["t"]))
    state = server.RunState(algo, case, "replay")
    clock, idx, out = float(rows[0]["t"]), 0, []
    while idx < len(rows):
        clock += 1
        while idx < len(rows) and float(rows[idx]["t"]) <= clock:
            state.feed(rows[idx])
            idx += 1
        snap = state.snapshot()
        out.append({"t": snap["t"], "s": [
            [r1(s["offered_kbps"]), r1(s["delivered_kbps"]), r1(s["predicted_kbps"]),
             r1(s["asked_kbps"]), None if s["through_pct"] is None else round(s["through_pct"], 2)]
            for s in snap["slices"]]})
    return out


def main():
    runs = {}
    for name in sorted(os.listdir(results)):
        m = re.match(r"(\w+?)_case(\d)\.csv$", name)
        if m:
            runs[name[:-4]] = snapshots(name, m.group(1), int(m.group(2)))
    summary = server.summarise_results()
    for r in summary["runs"]:
        r["mtime"] = 0
    out = os.path.join(HERE, "demo", "data.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        json.dump({"sla": server.SLA, "runs": runs, "summary": summary}, f, separators=(",", ":"))
    print("wrote %s: %d runs, %.0f KB" % (out, len(runs), os.path.getsize(out) / 1024))


main()
