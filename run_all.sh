#!/usr/bin/env bash
# Full reproduction: 4 traffic cases x 3 algorithms (static, AQoS, AFQoS).
# 12 runs x (300 s + ~25 s overhead) is about 65 minutes. Leave it running.
# Usage: ./run_all.sh [seconds per run, default 300]
DUR=${1:-300}
cd "$(dirname "$0")"
for CASE in 1 2 3 4; do
  for ALGO in static aqos afqos; do
    ./run_case.sh "$ALGO" "$CASE" "$DUR" | grep -E "^==|done"
  done
done
python analyze.py
