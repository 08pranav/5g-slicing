#!/usr/bin/env python3
"""Live demo dashboard for the AFQoS network-slicing experiment.

Run it inside the Ubuntu VM, from the repository folder, WITHOUT sudo:

    python3 dashboard/server.py

then open  http://<VM address>:8000  in the browser on your Mac.
(Find the VM address on the Mac with:  multipass info sdn  -> "IPv4".)

The page can start experiments, replay saved runs and run shell commands in
the repository folder, so keep it on the VM's private network (the Multipass default).
Only the Python standard library is used.
"""
import csv
import io
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RESULTS = os.path.join(ROOT, "results")
PORT = int(os.environ.get("DASH_PORT", "8000"))
RYU_BIN = os.path.expanduser("~/ryu-env/bin")

ALGOS = ("static", "aqos", "afqos")
SLA = {1: 8000, 2: 6000, 3: 4000, 4: 2000}           # kbps, same as afqos_app.py
WARMUP = 10                                            # seconds left out of the % figures
SHOWN_FILES = ("run_case.sh", "afqos_app.py", "qostopo.py", "analyze.py")

# Paper, Figs. 10-13: per-slice throughput (%) for QoS 1..4
PAPER = {
    "aqos": {1: [90.22, 90.20, 89.80, 88.42], 2: [90.73, 90.50, 90.33, 81.50],
             3: [90.68, 90.62, 90.24, 40.88], 4: [90.32, 90.31, 89.97, 88.21]},
    "afqos": {1: [99.99, 99.99, 99.99, 99.95], 2: [99.99, 99.96, 99.99, 92.02],
              3: [99.83, 99.11, 99.53, 48.93], 4: [99.99, 99.99, 99.99, 93.70]},
}


class Bus:
    """Fan-out of server-sent events to every open page."""

    def __init__(self):
        self.lock = threading.Lock()
        self.subs = []
        self.history = []          # recent terminal lines, replayed to new pages
        self.last = {}             # latest status / metrics / line / phase

    def subscribe(self):
        q = queue.Queue(maxsize=5000)
        with self.lock:
            self.subs.append(q)
            backlog = list(self.history) + list(self.last.values())
        for item in backlog:
            q.put_nowait(item)
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.subs:
                self.subs.remove(q)

    def publish(self, event, data):
        item = (event, data)
        with self.lock:
            if event == "term":
                self.history.append(item)
                del self.history[:-400]
            elif event in ("status", "metrics", "line", "phase"):
                self.last[event] = item
            subs = list(self.subs)
        for q in subs:
            try:
                q.put_nowait(item)
            except queue.Full:
                pass


BUS = Bus()


def term(text, kind="out"):
    BUS.publish("term", {"kind": kind, "text": text})


class RunState:
    """Per-slice numbers built from the controller's CSV rows."""

    def __init__(self, algo, case, source):
        self.algo, self.case, self.source = algo, case, source
        self.t = 0.0
        self.rate = {2: {}, 3: {}}
        self.pred, self.asked = {}, {}
        self.cum = {2: defaultdict(int), 3: defaultdict(int)}

    def feed(self, row):
        try:
            t = float(row["t"])
            dpid, m = int(row["dpid"]), int(row["meter_id"])
            nbytes, rate = int(row["bytes_delta"]), float(row["rate_kbps"])
        except (KeyError, ValueError):
            return
        if dpid not in (2, 3) or m not in SLA:
            return
        self.t = max(self.t, t)
        self.rate[dpid][m] = rate
        if dpid == 2:
            for key, store in (("predicted_kbps", self.pred), ("new_meter_kbps", self.asked)):
                try:
                    v = float(row.get(key, "nan"))
                    if v == v:          # not NaN
                        store[m] = v
                except ValueError:
                    pass
        if t >= WARMUP:
            self.cum[dpid][m] += nbytes

    def snapshot(self, switch_meters=None):
        slices = []
        for m in sorted(SLA):
            off, dlv = self.rate[2].get(m), self.rate[3].get(m)
            c2, c3 = self.cum[2][m], self.cum[3][m]
            slices.append({
                "id": m,
                "sla_kbps": SLA[m],
                "offered_kbps": off,
                "delivered_kbps": dlv,
                "predicted_kbps": self.pred.get(m),
                "asked_kbps": self.asked.get(m),
                "switch_kbps": (switch_meters or {}).get(m),
                "through_pct": min(100.0, 100.0 * c3 / c2) if c2 > 0 else None,
            })
        return {"t": round(self.t, 1), "algo": self.algo, "case": self.case,
                "source": self.source, "slices": slices}


def read_switch_meters():
    """Actual meter rates installed on switch s2 (what the switch enforces)."""
    try:
        out = subprocess.run(["sudo", "-n", "ovs-ofctl", "-O", "OpenFlow13", "dump-meters", "s2"],
                             capture_output=True, text=True, timeout=2).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    found = re.findall(r"meter=(\d+)\s+kbps.*?rate=(\d+)", out, re.S)
    return {int(m): int(r) for m, r in found} or None


class Metrics:
    """Every second: tail the live CSV (or step through a saved one) and publish."""

    def __init__(self):
        self.lock = threading.Lock()
        self.mode = None            # None | "live" | "replay"
        self.state = None
        self.path, self.offset, self.partial = None, 0, ""
        self.rows, self.idx, self.clock, self.speed = [], 0, 0.0, 1.0
        self.started = 0.0
        threading.Thread(target=self._loop, daemon=True).start()

    def watch(self, algo, case):
        with self.lock:
            self.mode = "live"
            self.state = RunState(algo, case, "live")
            self.path = os.path.join(RESULTS, "%s_case%d.csv" % (algo, case))
            self.offset, self.partial = 0, ""
            self.started = time.time()

    def stop_watching(self):
        with self.lock:
            if self.mode == "live":
                self._tail()
                if self.state:
                    BUS.publish("metrics", self.state.snapshot())
                self.mode = None

    def replay(self, name, speed):
        path = os.path.join(RESULTS, os.path.basename(name))
        m = re.match(r"(\w+?)_case(\d)\.csv$", os.path.basename(path))
        if not m or not os.path.isfile(path):
            return False
        with open(path) as f:
            rows = sorted(csv.DictReader(f), key=lambda r: float(r["t"]))
        with self.lock:
            self.mode = "replay"
            self.state = RunState(m.group(1), int(m.group(2)), "replay")
            self.rows, self.idx, self.speed = rows, 0, speed
            self.clock = float(rows[0]["t"]) if rows else 0.0
        return True

    def cancel_replay(self):
        with self.lock:
            if self.mode == "replay":
                self.mode = None
                return True
        return False

    def _tail(self):
        """Read rows appended to the live CSV since the last call (lock held)."""
        if not self.path or not os.path.isfile(self.path):
            return 0
        if os.path.getmtime(self.path) < self.started - 1:
            return 0                # still the previous run's file
        with open(self.path) as f:
            f.seek(self.offset)
            chunk = f.read()
            self.offset = f.tell()
        text = self.partial + chunk
        lines = text.split("\n")
        self.partial = lines.pop()  # unfinished last line, if any
        n = 0
        for line in lines:
            if not line or line.startswith("t,"):
                continue
            vals = next(csv.reader(io.StringIO(line)))
            if len(vals) == 8:
                keys = ["t", "algo", "dpid", "meter_id", "bytes_delta", "rate_kbps",
                        "predicted_kbps", "new_meter_kbps"]
                self.state.feed(dict(zip(keys, vals)))
                n += 1
        return n

    def _loop(self):
        while True:
            time.sleep(1)
            snap, finished = None, None
            with self.lock:
                if self.mode == "live":
                    if self._tail():
                        snap = self.state.snapshot(read_switch_meters())
                elif self.mode == "replay":
                    self.clock += self.speed
                    while self.idx < len(self.rows) and float(self.rows[self.idx]["t"]) <= self.clock:
                        self.state.feed(self.rows[self.idx])
                        self.idx += 1
                    snap = self.state.snapshot()
                    if self.idx >= len(self.rows):
                        self.mode = None
                        finished = self.state
            if snap:
                BUS.publish("metrics", snap)
            if finished:
                term("[replay finished]", "exit")
                BUS.publish("status", {"state": "idle"})


METRICS = Metrics()


TRACE = re.compile(r"^(\++)\[L(\d+)\] (.*)$")


class Runner:
    """Runs one job at a time (experiments and terminal commands) and streams it."""

    def __init__(self):
        self.lock = threading.Lock()
        self.jobs, self.current, self.proc = [], None, None
        self.stopping = False

    def busy(self):
        with self.lock:
            return self.current is not None

    def start(self, jobs):
        with self.lock:
            if self.current is not None:
                return False
            self.jobs, self.stopping = list(jobs), False
            self.current = self.jobs[0]
        threading.Thread(target=self._loop, daemon=True).start()
        return True

    def _loop(self):
        while True:
            with self.lock:
                if self.stopping or not self.jobs:
                    self.current, self.proc = None, None
                    break
                job = self.jobs.pop(0)
                self.current = job
            self._run(job)
        BUS.publish("status", {"state": "idle"})
        BUS.publish("results", {"changed": True})

    @staticmethod
    def env():
        env = os.environ.copy()
        if os.path.isdir(RYU_BIN):
            env["PATH"] = RYU_BIN + os.pathsep + env.get("PATH", "")
        env["PYTHONUNBUFFERED"] = "1"
        return env

    def _run(self, job):
        meta = job["meta"]
        term(job["display"], "cmd")
        BUS.publish("status", dict(meta, state="running", display=job["display"], started=time.time()))
        if job["kind"] == "experiment":
            BUS.publish("phase", {"phase": "cleanup", **meta})
            METRICS.watch(meta["algo"], meta["case"])
        try:
            proc = subprocess.Popen(job["argv"], cwd=ROOT, env=self.env(), stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True,
                                    bufsize=1, errors="replace", start_new_session=True)
        except OSError as exc:
            term("could not start: %s" % exc, "err")
            return
        with self.lock:
            self.proc = proc
        phase, last_line = "cleanup", 0
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            m = TRACE.match(line)
            if m:
                # the sourced script's own lines trace at depth 2 ("++"); deeper = subshells
                if len(m.group(1)) == 2 and job["kind"] == "experiment":
                    lineno, cmd = int(m.group(2)), m.group(3)
                    # background commands (the tail | grep display) can report late;
                    # run_case.sh runs top to bottom, so never move the marker backwards
                    if lineno < last_line:
                        continue
                    last_line = lineno
                    BUS.publish("line", {"file": "run_case.sh", "line": lineno, "text": cmd, **meta})
                    term("line %d  %s" % (lineno, cmd), "trace")
                    new = phase_for(cmd, phase)
                    if new != phase:
                        phase = new
                        BUS.publish("phase", {"phase": phase, **meta})
                continue
            if job["kind"] == "experiment" and line.startswith("Case ") and "offered rates" in line:
                phase = "traffic"
                BUS.publish("phase", {"phase": phase, **meta})
            term(line)
        rc = proc.wait()
        term("[finished, exit code %d]" % rc, "exit")
        if job["kind"] == "experiment":
            METRICS.stop_watching()
            BUS.publish("phase", {"phase": "done" if rc == 0 else "stopped", **meta})
            BUS.publish("line", {"file": "run_case.sh", "line": None, **meta})

    def stop(self):
        with self.lock:
            self.stopping, self.jobs = True, []
            proc = self.proc
        if proc and proc.poll() is None:
            term("^C  stopping...", "err")
            threading.Thread(target=self._kill, args=(proc,), daemon=True).start()
            return True
        return False

    @staticmethod
    def _kill(proc):
        for sig, wait in ((signal.SIGINT, 3), (signal.SIGTERM, 3), (signal.SIGKILL, 1)):
            try:
                os.killpg(proc.pid, sig)
            except (ProcessLookupError, PermissionError):
                break
            deadline = time.time() + wait
            while time.time() < deadline and proc.poll() is None:
                time.sleep(0.1)
            if proc.poll() is not None:
                break
        subprocess.run("sudo -n mn -c >/dev/null 2>&1; pkill -f 'ryu-manager afqos_app.py';"
                       " sudo -n pkill -9 iperf", shell=True, cwd=ROOT)
        term("network cleaned up (sudo mn -c)", "exit")


def phase_for(cmd, current):
    order = ["cleanup", "controller", "network", "traffic", "save"]
    pos = order.index(current) if current in order else 0
    if cmd.startswith("ryu-manager") or "QOS_ALGO=" in cmd:
        return "controller"
    if cmd.startswith("sudo python3"):
        return "network"
    if (cmd.startswith("kill") or cmd.startswith("pkill")) and pos >= 2:
        return "save"
    return current


RUNNER = Runner()


def side_command(cmd):
    """Quick command while an experiment is running (e.g. show the switch meters)."""
    def work():
        term(cmd, "cmd")
        try:
            out = subprocess.run(["bash", "-c", cmd], cwd=ROOT, env=Runner.env(), capture_output=True,
                                 text=True, timeout=30, errors="replace")
            for line in (out.stdout + out.stderr).splitlines():
                term(line)
            term("[finished, exit code %d]" % out.returncode, "exit")
        except subprocess.TimeoutExpired:
            term("[stopped after 30 s: long commands can run once the experiment ends]", "err")
    threading.Thread(target=work, daemon=True).start()


def summarise_results():
    runs = []
    if os.path.isdir(RESULTS):
        for name in sorted(os.listdir(RESULTS)):
            m = re.match(r"(\w+?)_case(\d)\.csv$", name)
            if not m:
                continue
            path = os.path.join(RESULTS, name)
            try:
                with open(path) as f:
                    rows = list(csv.DictReader(f))
            except OSError:
                continue
            active = [float(r["t"]) for r in rows if r["dpid"] == "2" and float(r["rate_kbps"]) > 100]
            if not active:
                continue
            t0, t1 = min(active) + 15, max(active) - 5
            off, dlv = defaultdict(int), defaultdict(int)
            for r in rows:
                t = float(r["t"])
                if t0 <= t <= t1:
                    (off if r["dpid"] == "2" else dlv)[int(r["meter_id"])] += int(r["bytes_delta"])
            through = [round(min(100.0, 100.0 * dlv[i] / off[i]), 2) if off[i] else None for i in range(1, 5)]
            runs.append({"file": name, "algo": m.group(1), "case": int(m.group(2)),
                         "through": through, "seconds": round(max(active) - min(active)),
                         "mtime": os.path.getmtime(path)})
    return {"runs": runs, "paper": PAPER}


class Handler(BaseHTTPRequestHandler):
    server_version = "SlicingDashboard/1.0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else (
            json.dumps(body) if ctype == "application/json" else body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return {}

    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                return self._send(200, f.read(), "text/html")
        if url.path == "/api/stream":
            return self._stream()
        if url.path == "/api/file":
            name = parse_qs(url.query).get("name", [""])[0]
            if name not in SHOWN_FILES:
                return self._send(404, {"error": "unknown file"})
            with open(os.path.join(ROOT, name)) as f:
                return self._send(200, f.read(), "text/plain")
        if url.path == "/api/results":
            return self._send(200, summarise_results())
        if url.path == "/api/state":
            return self._send(200, {"busy": RUNNER.busy()})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        url, body = urlparse(self.path), self._body()
        if url.path == "/api/experiment":
            algo = body.get("algo", "afqos")
            try:
                case, dur = int(body.get("case", 3)), int(body.get("duration", 60))
            except (TypeError, ValueError):
                return self._send(400, {"error": "case and duration must be numbers"})
            if algo not in ALGOS or case not in (1, 2, 3, 4) or not 20 <= dur <= 600:
                return self._send(400, {"error": "bad experiment settings"})
            METRICS.cancel_replay()
            algos = list(ALGOS) if body.get("compare") else [algo]
            # PS4 is set inside the shell (bash ignores an inherited PS4 when run as root),
            # then the script is sourced with tracing on, so every line it runs is reported.
            trace = 'PS4="+[L\\${LINENO}] "; set -x; . ./run_case.sh "$@"'
            jobs = [{"kind": "experiment",
                     "argv": ["bash", "-c", trace, "bash", a, str(case), str(dur)],
                     "display": "./run_case.sh %s %d %d" % (a, case, dur),
                     "meta": {"algo": a, "case": case, "duration": dur, "compare": len(algos) > 1,
                              "index": i, "total": len(algos)}}
                    for i, a in enumerate(algos)]
            if not RUNNER.start(jobs):
                return self._send(409, {"error": "Something is already running. Stop it first."})
            return self._send(200, {"ok": True})
        if url.path == "/api/command":
            cmd = str(body.get("cmd", "")).strip()
            if not cmd:
                return self._send(400, {"error": "empty command"})
            if RUNNER.busy():
                side_command(cmd)
                return self._send(200, {"ok": True, "side": True})
            job = {"kind": "command", "argv": ["bash", "-c", cmd], "display": cmd,
                   "meta": {"command": cmd}}
            RUNNER.start([job])
            return self._send(200, {"ok": True})
        if url.path == "/api/stop":
            stopped = RUNNER.stop()
            if METRICS.cancel_replay():
                term("[replay stopped]", "exit")
                BUS.publish("status", {"state": "idle"})
                stopped = True
            return self._send(200, {"ok": True, "stopped": stopped})
        if url.path == "/api/replay":
            if RUNNER.busy():
                return self._send(409, {"error": "An experiment is running. Stop it first."})
            name = str(body.get("file", ""))
            speed = float(body.get("speed", 1) or 1)
            if not METRICS.replay(name, max(0.5, min(speed, 10.0))):
                return self._send(404, {"error": "saved run not found"})
            term("replaying the saved log results/%s (no network needed)" % os.path.basename(name), "exit")
            st = METRICS.state
            BUS.publish("status", {"state": "replay", "algo": st.algo, "case": st.case,
                                   "file": os.path.basename(name), "started": time.time()})
            return self._send(200, {"ok": True})
        return self._send(404, {"error": "not found"})

    def _stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        q = BUS.subscribe()
        try:
            self.wfile.write(b"retry: 2000\n\n")
            self.wfile.flush()
            while True:
                try:
                    event, data = q.get(timeout=10)
                    msg = "event: %s\ndata: %s\n\n" % (event, json.dumps(data))
                except queue.Empty:
                    msg = ": keep-alive\n\n"
                self.wfile.write(msg.encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            BUS.unsubscribe(q)


def main():
    if os.geteuid() == 0 and os.environ.get("SUDO_USER"):
        print("Run this without sudo (it calls sudo itself where needed).")
        sys.exit(1)
    os.makedirs(RESULTS, exist_ok=True)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    try:
        addrs = subprocess.run(["hostname", "-I"], capture_output=True, text=True).stdout.split()
    except OSError:
        addrs = []
    print("Dashboard running. Open one of these in your Mac's browser:")
    for a in [a for a in addrs if ":" not in a] or ["<VM address>"]:
        print("    http://%s:%d" % (a, PORT))
    print("Press Ctrl+C here to stop the dashboard.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        RUNNER.stop()
        print("\nstopped")


if __name__ == "__main__":
    main()
