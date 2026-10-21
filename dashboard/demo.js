/* Demo mode: stands in for dashboard/server.py when the page is hosted statically.
   It replays real runs recorded by the experiment (demo/data.json) through the same
   events the live server streams. Active on any public host; use ?demo to force it
   and ?live to force the real backend. */
(() => {
  const host = location.hostname;
  const privateHost = host === "" || /^(localhost|127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)/.test(host) || host.endsWith(".local");
  const q = new URLSearchParams(location.search);
  if (!(q.has("demo") || (!privateHost && !q.has("live")))) return;
  window.DEMO = true;

  const TICK_MS = 250;      // one recorded second per tick
  const STEP_MS = 380;      // pause between script lines
  const RATES = { 1: [7000, 5000, 3000, 1000], 2: [8000, 6000, 4000, 2000], 3: [9000, 7000, 5000, 3000], 4: [7000, 5000, 3000, 4000] };
  const NAMES = ["eMBB", "URLLC", "mMTC", "BE"];
  const realFetch = window.fetch.bind(window);
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const json = (obj, status = 200) => new Response(JSON.stringify(obj), { status, headers: { "Content-Type": "application/json" } });

  let dataP = null;
  const data = () => dataP || (dataP = realFetch("demo/data.json").then(r => r.json()));
  const srcCache = {};
  const source = async name => srcCache[name] || (srcCache[name] = realFetch("demo/src/" + name).then(r => r.text()).then(t => t.split("\n")));

  const listeners = {};
  const emit = (ev, d) => (listeners[ev] || []).forEach(fn => fn({ data: JSON.stringify(d) }));
  const term = (text, kind = "out") => emit("term", { kind, text });
  window.EventSource = class {
    constructor() { setTimeout(() => emit("open", {}), 0); }
    addEventListener(ev, fn) { (listeners[ev] = listeners[ev] || []).push(fn); }
    close() {}
  };

  let token = 0;

  function metrics(algo, cs, snap, sla) {
    return {
      t: snap.t, algo, case: cs, source: "replay",
      slices: snap.s.map((x, i) => ({
        id: i + 1, sla_kbps: sla[i + 1], offered_kbps: x[0], delivered_kbps: x[1],
        predicted_kbps: x[2], asked_kbps: x[3], switch_kbps: null, through_pct: x[4],
      })),
    };
  }

  function statusLine(algo, snap, sla) {
    const parts = snap.s.map((x, i) => {
      const gate = x[3] != null ? x[3] : sla[i + 1];
      return `${NAMES[i]} ${((x[0] || 0) / 1000).toFixed(1)}->${(gate / 1000).toFixed(1)}`;
    });
    return `[${algo.toUpperCase()} t=${Math.floor(snap.t)}s] ${parts.join(" | ")}`;
  }

  async function playOne(job, my) {
    const { algo, cs, dur, index, total } = job;
    const d = await data();
    const meta = { algo, case: cs, duration: dur, compare: total > 1, index, total };
    const tag = `${algo}_case${cs}`;
    const snaps = (d.runs[tag] || []).slice(0, dur);
    const display = `./run_case.sh ${algo} ${cs} ${dur}`;
    term(display, "cmd");
    emit("status", { ...meta, state: "running", display, started: Date.now() / 1000 });
    emit("phase", { phase: "cleanup", ...meta });

    const lines = await source("run_case.sh");
    const nth = (re, n = 0) => { let c = 0; for (let i = 0; i < lines.length; i++) if (re.test(lines[i]) && c++ === n) return i + 1; return null; };
    let last = 0;
    const step = async (ln, text, phase, out) => {
      if (my !== token) return false;
      if (ln && ln > last) {
        last = ln;
        emit("line", { file: "run_case.sh", line: ln, text, ...meta });
        term(`line ${ln}  ${text}`, "trace");
      }
      if (phase) emit("phase", { phase, ...meta });
      if (out) out.split("\n").forEach(l => term(l));
      await sleep(STEP_MS);
      return my === token;
    };
    const rates = RATES[cs];
    const pre = [
      [nth(/sudo mn -c/), "sudo mn -c"],
      [nth(/pkill -f "ryu-manager/), "pkill -f ryu-manager afqos_app.py"],
      [nth(/^sleep 1/), "sleep 1"],
      [nth(/starting Ryu controller/), `echo == ${tag}: starting Ryu controller (log: results/ryu_${tag}.log)`, null, `== ${tag}: starting Ryu controller (log: results/ryu_${tag}.log)`],
      [nth(/^QOS_ALGO=/), `QOS_ALGO=${algo} QOS_OUT=results/${tag}.csv ryu-manager afqos_app.py ryu.app.ofctl_rest`, "controller"],
      [nth(/^sleep 5/), "sleep 5"],
      [nth(/^echo "== every second/), "echo == every second: Mbps each slice sends -> Mbps its gate (meter) allows", null, "== every second: Mbps each slice sends -> Mbps its gate (meter) allows"],
      [nth(/^tail --pid/), "tail --pid=4242 -n 0 -f results/ryu_" + tag + ".log"],
      [nth(/starting Mininet traffic/), `echo == ${tag}: starting Mininet traffic for ${dur}s`, null, `== ${tag}: starting Mininet traffic for ${dur}s`],
      [nth(/^sudo python3/), `sudo python3 -u qostopo.py --case ${cs} --duration ${dur} --logdir results/iperf_${tag}`, "network", "*** Creating network\n*** Adding controller, hosts h1-h8, switches s1-s4\n*** Starting network"],
    ];
    for (const s of pre) if (!(await step(...s))) return false;
    if (my !== token) return false;
    term(`Case ${cs}: average offered rates ${JSON.stringify(rates)} kbps for ${dur} s`);
    emit("phase", { phase: "traffic", ...meta });

    for (const snap of snaps) {
      if (my !== token) return false;
      emit("metrics", metrics(algo, cs, snap, d.sla));
      term(statusLine(algo, snap, d.sla));
      await sleep(TICK_MS);
    }

    const post = [
      [nth(/^kill \$TAILPID/), "kill 4242"],
      [nth(/^pkill -f "ryu-manager/, 1), "pkill -f ryu-manager afqos_app.py", "save"],
      [nth(/sudo mn -c/, 1), "sudo mn -c"],
      [nth(/: done ->/), `echo == ${tag}: done -> results/${tag}.csv`, null, `== ${tag}: done -> results/${tag}.csv`],
    ];
    for (const s of post) if (!(await step(...s))) return false;
    term("[finished, exit code 0]", "exit");
    emit("phase", { phase: "done", ...meta });
    emit("line", { file: "run_case.sh", line: null, ...meta });
    return true;
  }

  async function play(jobs) {
    const my = ++token;
    for (const job of jobs) if (!(await playOne(job, my))) break;
    if (my === token) { emit("status", { state: "idle" }); emit("results", { changed: true }); }
  }

  const busy = () => document.querySelector("#stopBtn") && !document.querySelector("#stopBtn").disabled;

  async function api(path, body) {
    const d = await data();
    if (path === "/api/experiment") {
      if (busy()) return json({ error: "Something is already running. Stop it first." }, 409);
      const algos = body.compare ? ["static", "aqos", "afqos"] : [body.algo];
      const dur = Math.min(+body.duration || 60, 180);
      play(algos.map((a, i) => ({ algo: a, cs: +body.case, dur, index: i, total: algos.length })));
      return json({ ok: true });
    }
    if (path === "/api/replay") {
      const m = /^(\w+?)_case(\d)\.csv$/.exec(body.file || "");
      const snaps = m && d.runs[m[1] + "_case" + m[2]];
      if (!snaps) return json({ error: "saved run not found" }, 404);
      const my = ++token;
      term(`replaying the saved log results/${body.file} (no network needed)`, "exit");
      emit("status", { state: "replay", algo: m[1], case: +m[2], file: body.file, started: Date.now() / 1000 });
      (async () => {
        for (const snap of snaps) {
          if (my !== token) return;
          emit("metrics", metrics(m[1], +m[2], snap, d.sla));
          await sleep(TICK_MS);
        }
        term("[replay finished]", "exit");
        emit("status", { state: "idle" });
      })();
      return json({ ok: true });
    }
    if (path === "/api/stop") {
      const was = busy();
      token++;
      if (was) { term("^C  stopping...", "err"); term("network cleaned up (sudo mn -c)", "exit"); }
      emit("status", { state: "idle" });
      emit("line", { file: "run_case.sh", line: null });
      return json({ ok: true, stopped: was });
    }
    if (path === "/api/command") {
      term(String(body.cmd || ""), "cmd");
      term("Demo mode: there is no VM behind this page, so shell commands are disabled. Clone the repository to run the real thing.", "err");
      return json({ ok: true });
    }
    return json({ error: "not found" }, 404);
  }

  window.fetch = async (input, init) => {
    const url = new URL(typeof input === "string" ? input : input.url, location.href);
    if (!url.pathname.startsWith("/api/")) return realFetch(input, init);
    if (url.pathname === "/api/results") { const d = await data(); return json(d.summary); }
    if (url.pathname === "/api/file") {
      const name = url.searchParams.get("name");
      return new Response((await source(name)).join("\n"), { headers: { "Content-Type": "text/plain" } });
    }
    if (url.pathname === "/api/state") return json({ busy: !!busy() });
    const body = init && init.body ? JSON.parse(init.body) : {};
    return api(url.pathname, body);
  };

  document.addEventListener("DOMContentLoaded", () => {
    const bar = document.createElement("div");
    bar.id = "demoBar";
    bar.innerHTML = '<b>Demo mode</b> Replaying real runs recorded from the experiment, sped up. No network is running behind this page. ' +
      '<a href="https://github.com/08pranav/5g-slicing" target="_blank" rel="noopener">Run it for real</a>';
    const css = document.createElement("style");
    css.textContent = "#demoBar{position:fixed;left:50%;bottom:14px;transform:translateX(-50%);z-index:50;max-width:min(92vw,720px);" +
      "background:#142530;color:#EDF1F4;font:600 14px/1.35 var(--sans);padding:9px 16px;border-radius:999px;box-shadow:0 4px 18px rgba(20,37,48,.35);text-align:center}" +
      "#demoBar b{background:#C47A08;color:#fff;border-radius:999px;padding:1px 9px;margin-right:8px}#demoBar a{color:#66C9DD;margin-left:6px;white-space:nowrap}";
    document.head.append(css);
    document.body.append(bar);
  });
})();
