# Ryu controller app: adaptive bandwidth slicing with OpenFlow meters.
#
# Reproduces: K. Deo, K. Chaudhary, M. Assaf (2024), "Adaptive quality of
# service for packet loss reduction using OpenFlow meters",
# PeerJ Computer Science 10:e1848, https://doi.org/10.7717/peerj-cs.1848
#
# Adapted from the authors' research_code/afqos.py
# (Zenodo, https://doi.org/10.5281/zenodo.10184077, CC BY 4.0).
# The allocation logic (AQoS, AFQoS: allocUnder / allocOver / allocation) and
# the predictive rate approximation follow the authors' code. Changes:
#   * MySQL logging replaced with a CSV file (no database needed)
#   * algorithm and output file chosen with environment variables
#   * a "static" mode (fixed slices, no adaptation) added as an extra baseline
#   * the interactive input() sanity check removed (it would freeze Ryu)
#   * s2 meters set to the SLA rates (8/6/4/2 Mbps) as soon as the slice flows
#     appear, instead of running at the 10 Mbps placeholder until the first
#     allocation (the first seconds are excluded from analysis either way)
#   * readable per-second console output for live demos
#
# Run:  QOS_ALGO=afqos QOS_OUT=results/afqos_case3.csv \
#       ryu-manager afqos_app.py ryu.app.ofctl_rest

import csv
import math
import os
import time

import numpy as np
from ryu.app import simple_switch_13
from ryu.controller import ofp_event
from ryu.controller.handler import MAIN_DISPATCHER, DEAD_DISPATCHER, set_ev_cls
from ryu.lib import hub

SLICE_NAMES = {1: "eMBB", 2: "URLLC", 3: "mMTC", 4: "BE"}


class AdaptiveSlicing13(simple_switch_13.SimpleSwitch13):
    def __init__(self, *args, **kwargs):
        super(AdaptiveSlicing13, self).__init__(*args, **kwargs)

        self.algo = os.environ.get("QOS_ALGO", "afqos").lower()   # afqos | aqos | static
        self.out_path = os.environ.get("QOS_OUT", "results/run.csv")
        self.POLL_INTVL = 1          # seconds between flow-stats polls (paper: 1 s)
        self.RATE_CONV = 1024        # authors' conversion (bytes*8/1024 -> "kbps")
        self.POCKET = int(os.environ.get("QOS_POCKET", "10"))  # samples used for prediction
        self.SWITCH_TO_CONTROL = 2   # meters are re-written on s2 (congestion point)
        self.BE = 4                  # best-effort class id

        # pre-configured (SLA) rates in kbps, total = 20,000 (paper Section "Results")
        self.configured = {1: 8000, 2: 6000, 3: 4000, 4: 2000}

        self.datapaths = {}
        self.prev_bytes = {2: {}, 3: {}}
        self.rate_q = {2: {m: [] for m in self.configured}, 3: {m: [] for m in self.configured}}
        self.time_q = []
        self.t0 = None
        self.sla_applied = False

        out_dir = os.path.dirname(self.out_path)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        self.csv_file = open(self.out_path, "w", newline="")
        self.csv = csv.writer(self.csv_file)
        self.csv.writerow(["t", "algo", "dpid", "meter_id", "bytes_delta",
                           "rate_kbps", "predicted_kbps", "new_meter_kbps"])

        self.logger.info("AFQoS reproduction app: algo=%s, log=%s", self.algo, self.out_path)
        self.monitor_thread = hub.spawn(self._monitor)

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def _state_change_handler(self, ev):
        dp = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            self.datapaths[dp.id] = dp
        elif ev.state == DEAD_DISPATCHER and dp.id in self.datapaths:
            del self.datapaths[dp.id]

    def _monitor(self):
        while True:
            for dp in list(self.datapaths.values()):
                if dp.id in (2, 3):
                    parser = dp.ofproto_parser
                    ofp = dp.ofproto
                    req = parser.OFPFlowStatsRequest(dp, 0, ofp.OFPTT_ALL, ofp.OFPP_ANY,
                                                     ofp.OFPG_ANY, 0, 0, parser.OFPMatch())
                    dp.send_msg(req)
            hub.sleep(self.POLL_INTVL)

    @staticmethod
    def _meter_of(flow):
        for inst in flow.instructions:
            if hasattr(inst, "meter_id"):
                return inst.meter_id
        return None

    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def flow_stats_reply_handler(self, ev):
        dpid = ev.msg.datapath.id
        now = time.time()
        if self.t0 is None:
            self.t0 = now
        t = now - self.t0

        # per-class byte counters from the priority-2 (slice) flows
        counters = {}
        for flow in ev.msg.body:
            if flow.priority != 2:
                continue
            m = self._meter_of(flow)
            if m in self.configured:
                counters[m] = flow.byte_count
        if len(counters) < len(self.configured):
            return  # slice flows not installed yet

        # first time we see the slice flows on s2: enforce the SLA rates
        if dpid == self.SWITCH_TO_CONTROL and not self.sla_applied:
            self.modify_meters(ev.msg.datapath, self.configured)
            self.sla_applied = True

        prev_all = dict(self.prev_bytes[dpid])
        self.prev_bytes[dpid] = dict(counters)
        if len(prev_all) < len(self.configured):
            return  # first sample only initialises the counters

        rates = {}
        deltas = {}
        for m, b in counters.items():
            prev = prev_all[m]
            delta = b - prev if b >= prev else b   # counter reset -> start again
            deltas[m] = delta
            rates[m] = delta * 8 / self.RATE_CONV / self.POLL_INTVL
            self.rate_q[dpid][m].append(rates[m])
            self.rate_q[dpid][m] = self.rate_q[dpid][m][-self.POCKET:]

        predicted, new_meter = {}, {}
        if dpid == self.SWITCH_TO_CONTROL:
            self.time_q.append(t)
            self.time_q = self.time_q[-self.POCKET:]
            if len(self.time_q) >= 2 and all(len(q) == len(self.time_q) for q in self.rate_q[2].values()):
                for m in self.configured:
                    predicted[m] = self.approximate_rate(self.time_q, self.rate_q[2][m])
                if self.algo != "static" and len(self.time_q) >= self.POCKET:
                    new_meter = self.meter_allocation(dict(self.configured), predicted)
                    self.modify_meters(ev.msg.datapath, new_meter)
                self.print_status(t, rates, new_meter)

        for m in sorted(deltas):
            self.csv.writerow([round(t, 3), self.algo, dpid, m, deltas[m], round(rates[m], 2),
                               round(predicted.get(m, float("nan")), 2),
                               round(new_meter.get(m, float("nan")), 2)])
        self.csv_file.flush()

    def approximate_rate(self, x, y):
        """Authors' 'predictive' method: least-squares line over the last
        POCKET samples, evaluated one poll interval into the future."""
        n = len(x)
        x = np.array(x, dtype=float)
        y = np.array(y, dtype=float)
        denom = n * np.sum(x ** 2) - np.sum(x) ** 2
        if denom == 0:
            return float(y[-1])
        m = (n * np.sum(x * y) - np.sum(x) * np.sum(y)) / denom
        c = (np.sum(y) * np.sum(x ** 2) - np.sum(x) * np.sum(x * y)) / denom
        next_poll = x[-1] + (x[1] - x[0])
        return max(float(m * next_poll + c), 0.0)

    def meter_allocation(self, allocated, current):
        if self.algo == "aqos":
            return self.meter_allocation_aqos(allocated, current)
        return self.meter_allocation_afqos(allocated, current)

    def meter_allocation_aqos(self, allocated, current):
        """Baseline AQoS (Boley, Jung & Kettimuthu 2017) as implemented by the authors."""
        need, unused = {}, {}
        for ids in allocated:
            need[ids] = current[ids] - allocated[ids] if current[ids] >= allocated[ids] else 0
            unused[ids] = allocated[ids] - current[ids] if current[ids] < allocated[ids] else 0
        new_meter = {}
        for i in need:
            new_meter[i] = min(allocated[i], current[i])
            if need[i] > 0:
                for j in unused:
                    if unused[j] > 0:
                        give = min(need[i], unused[j])
                        need[i] -= give
                        unused[j] -= give
                        new_meter[i] += give
        be = self.BE
        if sum(list(need.values())[:-1]) > 0:
            for i in need:
                if i == be:
                    break
                if need[i] > 0 and new_meter[be] > 0:
                    give = min(need[i], new_meter[be])
                    need[i] -= give
                    unused[be] = 0
                    new_meter[i] += give
                    new_meter[be] -= give
        return new_meter

    def meter_allocation_afqos(self, allocated, current):
        """Algorithm 1 (meterAllocation) from the paper."""
        need, unused = {}, {}
        for ids in allocated:
            need[ids] = current[ids] - allocated[ids] if current[ids] >= allocated[ids] else 0
            unused[ids] = allocated[ids] - current[ids] if current[ids] < allocated[ids] else 0
        if sum(need.values()) <= sum(unused.values()):
            return self.alloc_under(allocated, current, need, unused)
        return self.alloc_over(allocated, current, need, unused)

    def alloc_under(self, allocated, current, need, unused):
        """Algorithm 3 (allocUnder): spare capacity covers every need."""
        new_meter = {}
        ratio = sum(need.values()) / sum(unused.values())
        for ids in need:
            new_meter[ids] = max(allocated[ids], current[ids])
            if unused[ids] > 0:
                new_meter[ids] = allocated[ids] - ratio * unused[ids]
        return new_meter

    def alloc_over(self, allocated, current, need, unused):
        """Algorithm 2 (allocOver): needs exceed spare capacity."""
        be = self.BE
        round_off = 5
        new_meter, percent_need = {}, {}
        total_need = sum(list(need.values())[:-1])
        for ids in need:
            new_meter[ids] = min(allocated[ids], current[ids])
            percent_need[ids] = 0 if total_need == 0 else round(need[ids] / total_need, round_off)

        total_unused = rem_unused = sum(unused.values())
        need, new_meter, rem_unused = self.allocation(need, new_meter, be, percent_need,
                                                      total_unused, rem_unused, round_off)
        total_need = round(sum(list(need.values())[:-1]), round_off)
        if total_need > 0:
            be_available = allocated[be] - unused[be]
            if total_need >= be_available:
                new_meter[be] = 0
                for ids in need:
                    percent_need[ids] = 0 if total_need == 0 else round(need[ids] / total_need, round_off)
                total_unused = rem_unused = be_available
                need, new_meter, rem_unused = self.allocation(need, new_meter, be, percent_need,
                                                              total_unused, rem_unused, round_off)
            else:
                new_meter[be] = new_meter[be] - total_need
                for ids in need:
                    if ids != be:
                        new_meter[ids] += need[ids]
        elif rem_unused > 0:
            new_meter[be] += min(min(rem_unused, current[be]), need[be])
        return new_meter

    @staticmethod
    def allocation(need, new_meter, be, percent_need, total_unused, rem_unused, round_off):
        """Algorithm 4: share spare bandwidth among priority classes by need."""
        for ids in need:
            if ids == be:
                continue
            given = round(percent_need[ids] * total_unused, round_off)
            need[ids] -= given
            new_meter[ids] += given
            rem_unused -= given
        return need, new_meter, rem_unused

    def modify_meters(self, datapath, meter_rate):
        ofp = datapath.ofproto
        parser = datapath.ofproto_parser
        for ids, rate in meter_rate.items():
            if rate < 0:
                continue
            band = parser.OFPMeterBandDrop(rate=int(rate), burst_size=0)
            datapath.send_msg(parser.OFPMeterMod(datapath=datapath, command=ofp.OFPMC_MODIFY,
                                                 flags=ofp.OFPMF_KBPS, meter_id=ids, bands=[band]))

    def print_status(self, t, rates, new_meter):
        """One line per second: Mbps each slice sends -> Mbps its gate (meter) allows."""
        parts = []
        for m in sorted(self.configured):
            meter = new_meter.get(m, self.configured[m])
            parts.append("%s %.1f->%.1f" % (SLICE_NAMES[m].strip(), rates.get(m, 0) / 1000, meter / 1000))
        print("[%s t=%ds] %s" % (self.algo.upper(), t, " | ".join(parts)), flush=True)
