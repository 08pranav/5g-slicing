#!/usr/bin/env python3
# Mininet topology + bursty traffic generator for the AFQoS reproduction.
#
# Reproduces the test network of Deo, Chaudhary & Assaf (2024), PeerJ Computer
# Science 10:e1848 (Fig. 1), adapted from the authors' research_code/qostopo.py
# (Zenodo, https://doi.org/10.5281/zenodo.10184077, CC BY 4.0).
#
#  h1 --|                                      |-- h5   (eMBB server,  UDP 5111)
#  h2 --|  s1 ---- s2 ======== s3 ---- s4      |-- h6   (URLLC server, UDP 5112)
#  h3 --|        (meters)                      |-- h7   (mMTC server,  UDP 5113)
#  h4 --|                                      |-- h8   (best effort,  UDP 5114)
#
# Changes from the authors' script:
#   * controller IP is a command-line option (default 127.0.0.1, same VM)
#   * h6 IP fixed (the original gave h6 and h7 the same address, .133)
#   * traffic case chosen with --case instead of editing the bw list
#   * runs one trial of --duration seconds and exits (original looped forever)
#   * --userspace and --no-cpu-limit switches for environments without the
#     OVS kernel module or cgroups (not needed on a normal Ubuntu VM)
#   * iperf clients use --no-udp-fin when available (iperf 2.1+), otherwise its
#     end-of-test retries inflate the offered load by ~12% or more
# Traffic shape (constant -> sine -> x^2 bursts, +/-20% random jitter, new
# iperf burst every 2 s, each class phase-shifted by 60 s) is unchanged.
#
# Run (inside the VM, after starting Ryu):  sudo python3 qostopo.py --case 3

import argparse
import math
import os
import random
import time
from functools import partial

import requests
from mininet.cli import CLI
from mininet.link import TCLink
from mininet.log import setLogLevel
from mininet.net import Mininet
from mininet.node import CPULimitedHost, Host, OVSSwitch, RemoteController
from mininet.topo import Topo

# offered average rates per class (kbps) for the paper's four cases
CASES = {
    1: [7000, 5000, 3000, 1000],   # 80% of the 20 Mbps slice budget (underutilised)
    2: [8000, 6000, 4000, 2000],   # 100% (fully utilised)
    3: [9000, 7000, 5000, 3000],   # 120% (overloaded)
    4: [7000, 5000, 3000, 4000],   # 95%, best-effort class needs more
}
PORTS = [5111, 5112, 5113, 5114]
LINK_BW = 30        # Mbit/s on the switch-to-switch links (authors' value)
LINK_DELAY = "10ms"


class QosTopo(Topo):
    def build(self, cpu=None):
        hopts = {"cpu": cpu} if cpu else {}
        left = [self.addHost("h%d" % i, ip="192.168.0.12%d/24" % i,
                             mac="00:00:00:00:00:2%d" % i, **hopts) for i in range(1, 5)]
        right = [self.addHost("h%d" % (i + 4), ip="192.168.0.13%d/24" % i,
                              mac="00:00:00:00:00:3%d" % i, **hopts) for i in range(1, 5)]
        s1, s2, s3, s4 = [self.addSwitch("s%d" % i, protocols="OpenFlow13") for i in range(1, 5)]
        for h in left:
            self.addLink(h, s1)
        for h in right:
            self.addLink(h, s4)
        # switch ports: s2 port1 <- s1, s2 port2 -> s3 ; s3 port1 <- s2, s3 port2 -> s4
        self.addLink(s1, s2, cls=TCLink, bw=LINK_BW, delay=LINK_DELAY)
        self.addLink(s2, s3, cls=TCLink, bw=LINK_BW, delay=LINK_DELAY)
        self.addLink(s3, s4, cls=TCLink, bw=LINK_BW, delay=LINK_DELAY)


def rest_post(base, path, payload, retries=20):
    for _ in range(retries):
        try:
            r = requests.post(base + path, json=payload, timeout=3)
            if r.status_code == 200:
                return True
        except requests.RequestException:
            pass
        time.sleep(0.5)
    raise RuntimeError("Ryu REST call failed: %s (is ryu-manager running with ryu.app.ofctl_rest?)" % path)


def add_meter(base, dpid, meter_id, rate):
    rest_post(base, "/stats/meterentry/add", {
        "dpid": dpid, "flags": "KBPS", "meter_id": meter_id,
        "bands": [{"type": "DROP", "rate": rate, "burst_size": 0}]})


def add_flow(base, dpid, udp_dst, meter_id):
    rest_post(base, "/stats/flowentry/add", {
        "dpid": dpid, "cookie": 1, "cookie_mask": 1, "table_id": 0,
        "idle_timeout": 0, "hard_timeout": 0, "priority": 2, "flags": 1,
        "match": {"udp_dst": udp_dst, "ip_proto": 17, "eth_type": 2048},
        "actions": [{"type": "METER", "meter_id": meter_id}, {"type": "OUTPUT", "port": 2}]})


def mapper(val, in1, in2, out1, out2):
    m = (out2 - out1) / (in2 - in1)
    return m * val + (out1 - m * in1)


def determine_bandwidth(base, t):
    """0-60 s constant, 60-180 s one sine period, 180-300 s an x^2 curve,
    all within +/-20% of the base rate (authors' determineBandwidth)."""
    variance = 0.2
    t = t % 300
    lo, hi = base - variance * base, base + variance * base
    if t < 60:
        return base
    if t < 180:
        x = mapper(t, 60, 180, 0, 2 * math.pi)
        return mapper(math.sin(x), -1, 1, lo, hi)
    x = mapper(t, 180, 300, -2, 2)
    return mapper(x ** 2, 0, 4, lo, hi)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", type=int, choices=[1, 2, 3, 4], default=3)
    ap.add_argument("--duration", type=int, default=300, help="seconds of traffic (paper: 300)")
    ap.add_argument("--controller-ip", default="127.0.0.1")
    ap.add_argument("--rest", default="http://127.0.0.1:8080")
    ap.add_argument("--logdir", default="results/iperf")
    ap.add_argument("--userspace", action="store_true", help="OVS userspace datapath")
    ap.add_argument("--no-cpu-limit", action="store_true", help="plain hosts, no cgroup CPU share")
    ap.add_argument("--cli", action="store_true", help="drop into the Mininet CLI at the end")
    args = ap.parse_args()

    setLogLevel("info")
    bw = CASES[args.case]
    cpu = None if args.no_cpu_limit else 4 / 8   # authors: 4 cores shared by 8 hosts
    host_cls = Host if args.no_cpu_limit else CPULimitedHost
    switch_cls = partial(OVSSwitch, datapath="user") if args.userspace else OVSSwitch

    def build_net(cpu_share, hcls):
        return Mininet(topo=QosTopo(cpu=cpu_share), switch=switch_cls, host=hcls, link=TCLink,
                       controller=RemoteController("ryu", ip=args.controller_ip, port=6633),
                       autoSetMacs=False)

    try:
        net = build_net(cpu, host_cls)
        net.start()
    except Exception as exc:   # e.g. cgroup v2 hosts where CPULimitedHost cannot start
        if args.no_cpu_limit:
            raise
        print("*** CPU-limited hosts failed (%s); retrying with plain hosts" % exc)
        from mininet.clean import cleanup
        cleanup()
        net = build_net(None, Host)
        net.start()
    time.sleep(2)
    net.pingAll()

    # 4 slices = 4 meters on each core switch; the controller app re-sizes the s2 meters
    for dpid in (2, 3):
        for meter_id in range(1, 5):
            add_meter(args.rest, dpid, meter_id, 10000)
        for meter_id, port in enumerate(PORTS, start=1):
            add_flow(args.rest, dpid, port, meter_id)

    os.makedirs(args.logdir, exist_ok=True)
    clients = net.get("h1", "h2", "h3", "h4")
    servers = net.get("h5", "h6", "h7", "h8")
    for srv, port in zip(servers, PORTS):
        srv.cmd("iperf -s -u -p %d -i 2 > %s/server_%s.txt 2>&1 &" % (port, args.logdir, srv.name))

    # iperf 2.1+ keeps re-sending its end-of-test packet until the server answers,
    # which adds ~12% hidden traffic per burst (much more when the meter drops it).
    # --no-udp-fin turns that off so the switch sees only the requested rate.
    help_text = os.popen("iperf --help 2>&1").read()
    no_fin = " --no-udp-fin" if "no-udp-fin" in help_text else ""

    print("Case %d: average offered rates %s kbps for %d s" % (args.case, bw, args.duration))
    start = time.time()
    while time.time() - start < args.duration:
        t = int(time.time() - start)
        for k, (cli, srv, port) in enumerate(zip(clients, servers, PORTS)):
            y = determine_bandwidth(bw[k], t + 60 * k)        # phase shift per class
            y = random.randint(int(y - 0.2 * y), int(y + 0.2 * y))
            cli.cmd("iperf -u -c %s -p %d -b %.3fm -t 2%s >> %s/client_%s.txt 2>&1 &"
                    % (srv.IP(), port, y / 1000.0, no_fin, args.logdir, cli.name))
        time.sleep(2)

    time.sleep(4)   # let the last bursts drain
    if args.cli:
        CLI(net)
    net.stop()
    os.system("pkill -9 iperf")   # background iperf servers outlive the hosts otherwise


if __name__ == "__main__":
    main()
