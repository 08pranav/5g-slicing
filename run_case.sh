#!/usr/bin/env bash
# Run one experiment: start the Ryu app, run the Mininet traffic, stop both.
# Usage:  ./run_case.sh <afqos|aqos|static> <case 1-4> [seconds, default 300]
# Extra Mininet options can be passed with TOPO_ARGS, e.g.
#   TOPO_ARGS="--userspace" ./run_case.sh afqos 3 60
# CPU limiting is off by default: where cgroup limits fail, the fallback inside
# qostopo.py runs a Mininet cleanup that kills the controller. TOPO_ARGS="" turns it back on.
set -u
ALGO=${1:-afqos}
CASE=${2:-3}
DUR=${3:-300}
TOPO_ARGS=${TOPO_ARGS-"--no-cpu-limit"}
TAG="${ALGO}_case${CASE}"
cd "$(dirname "$0")"
mkdir -p results

sudo mn -c >/dev/null 2>&1 || true
pkill -f "ryu-manager afqos_app.py" 2>/dev/null || true
sleep 1

echo "== $TAG: starting Ryu controller (log: results/ryu_${TAG}.log)"
QOS_ALGO=$ALGO QOS_OUT="results/${TAG}.csv" \
  ryu-manager afqos_app.py ryu.app.ofctl_rest > "results/ryu_${TAG}.log" 2>&1 &
sleep 5
# show the controller's per-second decisions while the experiment runs
# (--pid makes tail stop when this script ends, so nothing is left running)
echo "== every second: Mbps each slice sends -> Mbps its gate (meter) allows"
tail --pid=$$ -n 0 -f "results/ryu_${TAG}.log" | grep --line-buffered " t=[0-9]*s\] eMBB" &
TAILPID=$!

echo "== $TAG: starting Mininet traffic for ${DUR}s"
sudo python3 -u qostopo.py --case "$CASE" --duration "$DUR" \
  --logdir "results/iperf_${TAG}" ${TOPO_ARGS:-}

kill $TAILPID 2>/dev/null || true
pkill -f "ryu-manager afqos_app.py" 2>/dev/null || true
sudo mn -c >/dev/null 2>&1 || true
echo "== $TAG: done -> results/${TAG}.csv"
