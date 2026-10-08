# Adaptive 5G Network Slicing with SDN

A working reproduction of **Deo, Chaudhary & Assaf (2024), "Adaptive quality of service
for packet loss reduction using OpenFlow meters"**, *PeerJ Computer Science* 10:e1848
([paper](https://doi.org/10.7717/peerj-cs.1848)), built as a Computer Networks mini project.

Four traffic slices share one congested 20 Mbps link. An SDN controller (Ryu) watches
every slice once per second and re-sizes each slice's bandwidth limit on the switch, so a
busy slice can borrow from a quiet one without starving it. We compare three ways of
sharing the link under four load levels, from 80% to 120% of capacity, and show it all
live in a web dashboard.

![Live dashboard during an overloaded run](docs/dashboard-preview.png)

## Contents

- [How it works](#how-it-works)
- [Repository layout](#repository-layout)
- [Setup](#setup)
- [Running experiments](#running-experiments)
- [Live dashboard](#live-dashboard)
- [Expected results](#expected-results)
- [Differences from the authors' code](#differences-from-the-authors-code)
- [Troubleshooting](#troubleshooting)
- [Credits](#credits)

## How it works

![Architecture: Ryu reads counters on s2 and s3 and re-sizes the four slice meters on s2 every second](docs/architecture.svg)

Each slice has its own OpenFlow meter on switch **s2**, the congestion point. Every
second the controller:

1. reads each slice's byte counters from s2 (traffic offered) and s3 (traffic delivered),
2. predicts each slice's rate for the next second with a least-squares line over the
   last 10 samples (the paper's "predictive" rate approximation),
3. runs one of three sharing rules and writes the new meter rates to s2.

| Slice (our 5G label) | Paper name | Guaranteed rate | Traffic |
|---|---|---|---|
| eMBB, video | QoS 1 | 8 Mbps | h1 to h5, UDP 5111 |
| URLLC, gaming and remote surgery | QoS 2 | 6 Mbps | h2 to h6, UDP 5112 |
| mMTC, IoT sensors | QoS 3 | 4 Mbps | h3 to h7, UDP 5113 |
| Best effort | QoS 4 (BE) | 2 Mbps | h4 to h8, UDP 5114 |

The paper calls the slices QoS classes 1 to 4; the eMBB, URLLC and mMTC labels are our
mapping to 5G slice types.

| Sharing rule | What it does every second |
|---|---|
| `static` | Keeps every meter at its guaranteed rate. Our extra baseline: no adaptation. |
| `aqos` | AQoS (Boley, Jung & Kettimuthu, 2017), the paper's baseline. Caps a quiet slice at exactly its current rate and gives the spare to busy slices. |
| `afqos` | AFQoS, the paper's algorithm. Lends spare bandwidth in proportion to each slice's need, leaves quiet slices room to grow, and takes from best effort first when every slice is over budget. |

Traffic is bursty UDP from iperf: a new 2-second burst every 2 s, following a constant,
then sine, then x² pattern with ±20% random jitter, each slice phase-shifted by 60 s
(the authors' traffic generator, unchanged).

## Repository layout

| Path | What it is |
|---|---|
| `afqos_app.py` | Ryu controller app: rate prediction, AQoS and AFQoS allocation, CSV logging |
| `qostopo.py` | Mininet topology (paper Fig. 1) and the bursty iperf traffic generator |
| `run_case.sh` | Runs one experiment: controller, network, traffic, then cleanup |
| `run_all.sh` | Runs all 4 cases × 3 rules (about 65 minutes), then the analysis |
| `analyze.py` | Throughput per slice next to the paper's numbers, plus charts in `results/` |
| `setup_vm.sh` | One-time install inside Ubuntu: Mininet, Open vSwitch, iperf, Ryu |
| `dashboard/server.py` | Web dashboard backend (Python standard library only) |
| `dashboard/index.html` | Web dashboard page |
| `docs/` | Screenshots |

## Setup

Mininet needs a Linux kernel, so on a Mac everything runs in an Ubuntu VM via
[Multipass](https://multipass.run). Windows works the same way with Multipass for Windows.

```bash
# on the Mac
brew install --cask multipass
multipass launch 22.04 --name sdn --cpus 4 --memory 4G --disk 15G
multipass shell sdn

# inside the VM: get the code (the repository is private, so sign in first)
sudo apt-get install -y gh
gh auth login                   # GitHub.com, HTTPS, log in with a web browser
gh repo clone 08pranav/5g-slicing
cd 5g-slicing
bash setup_vm.sh                # about 10 minutes
. ~/ryu-env/bin/activate
```

No GitHub login in the VM? Download the code on the Mac instead (**Code > Download ZIP**),
then `multipass transfer 5g-slicing-main.zip sdn:/home/ubuntu/` and unzip it
inside the VM.

Two version pins matter, and `setup_vm.sh` handles both: Ryu 4.34 needs an older Python,
so it runs in a Python 3.8 environment, and it needs `eventlet==0.30.2`.

## Running experiments

```bash
./run_case.sh afqos 3 60        # rule, case (1-4), seconds; 1-minute smoke test
./run_all.sh                    # the full comparison, 300 s per run
python analyze.py               # table vs the paper + PNG charts in results/
```

While a run is going, the terminal prints one line per second, for example
`[AFQOS t=42s] eMBB 10.8->8.7 | URLLC 9.4->7.1 | mMTC 5.0->4.4 | BE 3.4->0.0`
(Mbps each slice sends -> Mbps its meter allows).

| Case | Average offered rate, kbps (eMBB / URLLC / mMTC / BE) | Link load |
|---|---|---|
| 1 | 7000 / 5000 / 3000 / 1000 | 80% |
| 2 | 8000 / 6000 / 4000 / 2000 | 100% |
| 3 | 9000 / 7000 / 5000 / 3000 | 120% |
| 4 | 7000 / 5000 / 3000 / 4000 | 95%, best effort needs more |

To copy results to the Mac:

```bash
multipass exec sdn -- bash -c "cd /home/ubuntu/5g-slicing && zip -qr results.zip results"
multipass transfer sdn:/home/ubuntu/5g-slicing/results.zip .
```

## Live dashboard

A web page that runs the experiments from buttons and shows them two ways at once:

- **The friendly side:** experiment choices in plain words, an animated network map
  where each slice passes through its gate on s2 and dropped packets fall away, a card per
  slice with how much of its traffic got through, a running plain-English explanation,
  charts over time, and a comparison of the three rules against the paper.
- **Under the hood:** the script with the line being executed highlighted and explained,
  the controller code with the allocation function flashing each time it runs, a working
  terminal, and a table of the controller's per-second decisions.

```bash
# inside the VM, from the repository folder (no sudo)
python3 dashboard/server.py
```

Open the address it prints (for example `http://192.168.64.5:8000`) in a browser on the
Mac; `multipass info sdn` also shows the VM's address. **Replay a saved run** plays back
any `results/*.csv` without starting the network, which is a safe fallback during a live
demo. The page can run commands inside the VM, so keep it on the Multipass private network.

## Expected results

Per-slice throughput reported in the paper (Figs. 10 to 13), in %, eMBB / URLLC /
mMTC / best effort:

| Case | AQoS | AQoS avg | AFQoS | AFQoS avg |
|---|---|---|---|---|
| 1 (80%) | 90.2 / 90.2 / 89.8 / 88.4 | 89.66 | 99.99 / 99.99 / 99.99 / 99.95 | 99.98 |
| 2 (100%) | 90.7 / 90.5 / 90.3 / 81.5 | 88.23 | 99.99 / 99.96 / 99.99 / 92.02 | 97.99 |
| 3 (120%) | 90.7 / 90.6 / 90.2 / 40.9 | 78.11 | 99.83 / 99.11 / 99.53 / 48.93 | 86.85 |
| 4 (95%) | 90.3 / 90.3 / 90.0 / 88.2 | 89.70 | 99.99 / 99.99 / 99.99 / 93.70 | 98.42 |

The paper averaged 30 trials of 300 s on a physical PC; `analyze.py` prints our numbers
next to these. Throughput is measured as bytes counted at s3 (after the meter) divided by
bytes counted at s2 (before it).

**An observation beyond the paper:** when AFQoS asks for a 0 kbps meter for best effort,
Open vSwitch rejects the update, so best effort keeps whatever rate was last accepted. You
can watch it with `sudo ovs-ofctl -O OpenFlow13 dump-meters s2` during case 3. This likely
explains why best effort still gets about half its traffic through at 120% load in the
paper, and why that number varies from run to run.

## Differences from the authors' code

The allocation logic (AQoS, and AFQoS's `allocUnder`, `allocOver` and `allocation`) and
the rate prediction are unchanged. The plumbing around them was simplified so it runs in
one VM:

- MySQL logging replaced with `results/<rule>_case<N>.csv`
- controller address is `127.0.0.1` (the authors ran Ryu in a second VM)
- h6's IP corrected to 192.168.0.132 (the original script gave h6 and h7 the same address)
- traffic case chosen with `--case`, one trial per run instead of an endless loop
- `static` baseline added; a blocking `input()` sanity check removed
- s2 meters set to the guaranteed rates as soon as the slice flows appear
- iperf clients use `--no-udp-fin` when available: iperf 2.1+ keeps re-sending its
  end-of-test packet until the server replies, which added about 12% hidden traffic per
  burst in our tests, and far more for a slice whose meter drops those packets

## Troubleshooting

| Problem | Fix |
|---|---|
| `ryu-manager: command not found` | `. ~/ryu-env/bin/activate` |
| `ImportError: cannot import name 'ALREADY_HANDLED'` | `pip install eventlet==0.30.2` inside `ryu-env` |
| `Specified qdisc kind is unknown` | `sudo apt install linux-modules-extra-$(uname -r)`, then restart the VM |
| cgroup errors from CPU-limited hosts | The script retries with plain hosts; to force it: `TOPO_ARGS="--no-cpu-limit" ./run_case.sh ...` |
| Leftover network after a crash or Ctrl+C | `sudo mn -c` |
| `multipass launch` says the remote is unreachable | Wait a minute and retry, or turn off VPN / switch networks |
| Dashboard won't open on the Mac | Check the address with `multipass info sdn` and that `server.py` is still running |

## Credits

- Paper: K. Deo, K. Chaudhary, M. Assaf, "Adaptive quality of service for packet loss
  reduction using OpenFlow meters", *PeerJ Computer Science* 10:e1848, 2024.
  https://doi.org/10.7717/peerj-cs.1848
- The controller and topology build on the authors' published code: K. Deo, "Adaptive
  Quality of Service using Predictive Rate Approximation using OpenFlow Meters", Zenodo,
  2023, https://doi.org/10.5281/zenodo.10184077, licensed CC BY 4.0. Our changes are
  listed above.
- AQoS baseline: J. M. Boley, E.-S. Jung, R. Kettimuthu, "Adaptive QoS for data
  transfers using software-defined networking", 2017.
- Built with [Mininet](https://mininet.org), [Open vSwitch](https://www.openvswitch.org),
  [Ryu](https://github.com/faucetsdn/ryu) and [iperf](https://iperf.fr).
