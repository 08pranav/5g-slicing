#!/usr/bin/env python3
"""Turn the controller CSV logs into the paper's throughput table and figures.

Throughput of a slice = bytes that got through the s2 meter (counted at s3)
divided by bytes offered to the meter (counted at s2), as a percentage. This
mirrors the paper, which used flow statistics from both congestion switches.

Usage:  python3 analyze.py            (reads results/*.csv, writes results/*.png)
        python3 analyze.py --warmup 15 --tail 5
"""
import argparse
import csv
import glob
import os
import re
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

SLICES = {1: "eMBB (QoS 1)", 2: "URLLC (QoS 2)", 3: "mMTC (QoS 3)", 4: "Best effort (QoS 4)"}

# Paper, Figs. 10-13: per-class throughput (%) for QoS 1..4
PAPER = {
    ("aqos", 1): [90.22, 90.20, 89.80, 88.42], ("afqos", 1): [99.99, 99.99, 99.99, 99.95],
    ("aqos", 2): [90.73, 90.50, 90.33, 81.50], ("afqos", 2): [99.99, 99.96, 99.99, 92.02],
    ("aqos", 3): [90.68, 90.62, 90.24, 40.88], ("afqos", 3): [99.83, 99.11, 99.53, 48.93],
    ("aqos", 4): [90.32, 90.31, 89.97, 88.21], ("afqos", 4): [99.99, 99.99, 99.99, 93.70],
}
CASE_LABEL = {1: "Case 1: 80% load", 2: "Case 2: 100% load",
              3: "Case 3: 120% load", 4: "Case 4: best effort needs more"}


def load(path):
    rows = []
    with open(path) as f:
        for r in csv.DictReader(f):
            rows.append({"t": float(r["t"]), "dpid": int(r["dpid"]), "m": int(r["meter_id"]),
                         "bytes": int(r["bytes_delta"]), "rate": float(r["rate_kbps"]),
                         "meter": float(r["new_meter_kbps"]) if r["new_meter_kbps"] != "nan" else None})
    return rows


def throughput(rows, warmup, tail):
    active = [r["t"] for r in rows if r["dpid"] == 2 and r["rate"] > 100]
    if not active:
        return None
    t_start, t_end = min(active) + warmup, max(active) - tail
    offered, delivered = defaultdict(int), defaultdict(int)
    for r in rows:
        if t_start <= r["t"] <= t_end:
            (offered if r["dpid"] == 2 else delivered)[r["m"]] += r["bytes"]
    return {m: 100.0 * min(delivered[m] / offered[m], 1.0) for m in sorted(offered) if offered[m]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="results")
    ap.add_argument("--warmup", type=float, default=15, help="seconds ignored at the start")
    ap.add_argument("--tail", type=float, default=5, help="seconds ignored at the end")
    args = ap.parse_args()

    results = {}
    for path in sorted(glob.glob(os.path.join(args.dir, "*_case*.csv"))):
        m = re.match(r"(\w+?)_case(\d)\.csv", os.path.basename(path))
        if not m:
            continue
        algo, case = m.group(1), int(m.group(2))
        rows = load(path)
        tp = throughput(rows, args.warmup, args.tail)
        if tp:
            results[(algo, case)] = (tp, rows)

    if not results:
        print("No results found in %s/" % args.dir)
        return

    # ---- table
    print("\nThroughput per slice (%)  -- ours vs paper (Deo et al. 2024, Figs. 10-13)\n")
    print("%-8s %-5s %-34s %-7s %s" % ("algo", "case", "eMBB / URLLC / mMTC / BE", "avg", "paper avg"))
    for (algo, case), (tp, _) in sorted(results.items(), key=lambda k: (k[0][1], k[0][0])):
        vals = [tp.get(i, float("nan")) for i in range(1, 5)]
        avg = sum(vals) / len(vals)
        paper = PAPER.get((algo, case))
        paper_avg = "%.2f" % (sum(paper) / 4) if paper else "-"
        print("%-8s %-5d %-34s %-7.2f %s" % (algo, case, " / ".join("%.1f" % v for v in vals), avg, paper_avg))

    # ---- figure 1: per-case bar chart, ours vs paper
    cases = sorted({c for (_, c) in results})
    fig, axes = plt.subplots(1, len(cases), figsize=(4.6 * len(cases), 4.6), squeeze=False)
    for ax, case in zip(axes[0], cases):
        series = []
        for algo in ("static", "aqos", "afqos"):
            if (algo, case) in results:
                tp = results[(algo, case)][0]
                series.append(("%s (ours)" % algo.upper(), [tp.get(i, 0) for i in range(1, 5)]))
            if (algo, case) in PAPER:
                series.append(("%s (paper)" % algo.upper(), PAPER[(algo, case)]))
        width = 0.8 / max(len(series), 1)
        for k, (label, vals) in enumerate(series):
            ax.bar([i + k * width for i in range(4)], vals, width, label=label,
                   hatch="//" if "paper" in label else None, alpha=0.9)
        ax.set_xticks([i + width * (len(series) - 1) / 2 for i in range(4)])
        ax.set_xticklabels(["eMBB", "URLLC", "mMTC", "BE"])
        ax.set_ylim(0, 105)
        ax.set_title(CASE_LABEL[case])
        ax.set_ylabel("Throughput (%)")
        ax.grid(axis="y", alpha=0.3)
    handles, labels = axes[0][0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", bbox_to_anchor=(0.5, 0.0),
               ncol=min(len(labels), 2 * len(cases) + 1), fontsize=8)
    fig.tight_layout()
    out = os.path.join(args.dir, "throughput_comparison.png")
    fig.savefig(out, dpi=150, bbox_inches="tight")
    print("\nSaved", out)

    # ---- figure 2: meter allocation over time for each adaptive run
    for (algo, case), (_, rows) in sorted(results.items()):
        if algo == "static":
            continue
        fig, ax = plt.subplots(figsize=(9, 4))
        for i in range(1, 5):
            pts = [(r["t"], r["meter"] / 1000) for r in rows if r["dpid"] == 2 and r["m"] == i and r["meter"] is not None]
            off = [(r["t"], r["rate"] / 1000) for r in rows if r["dpid"] == 2 and r["m"] == i]
            if pts:
                line, = ax.plot(*zip(*pts), label="%s meter" % SLICES[i])
                ax.plot(*zip(*off), linestyle=":", color=line.get_color(), alpha=0.6)
        ax.set_xlabel("time (s)")
        ax.set_ylabel("Mbps")
        ax.set_title("%s, %s: meter rate the controller asked for (solid) vs offered traffic (dotted)"
                     % (algo.upper(), CASE_LABEL[case]), fontsize=10)
        ax.legend(fontsize=7, ncol=2)
        ax.grid(alpha=0.3)
        fig.tight_layout()
        out = os.path.join(args.dir, "allocation_%s_case%d.png" % (algo, case))
        fig.savefig(out, dpi=150)
        print("Saved", out)


if __name__ == "__main__":
    main()
