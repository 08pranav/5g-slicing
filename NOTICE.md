# Attribution

This project exists because of the research and code of others. All credit for the
algorithm and the experimental design goes to them.

## The paper

Krishneel Deo, Kaylash Chaudhary and Mansour Assaf (2024).
**Adaptive quality of service for packet loss reduction using OpenFlow meters.**
*PeerJ Computer Science* 10:e1848. https://doi.org/10.7717/peerj-cs.1848

The AFQoS algorithm, the predictive rate approximation, the network topology, the
traffic model, the four load cases and the reference throughput figures used for
comparison are theirs.

## The authors' code

Krishneel Deo, *Adaptive Quality of Service using Predictive Rate Approximation using
OpenFlow Meters*, Zenodo, 2023. https://doi.org/10.5281/zenodo.10184077
Licensed under [Creative Commons Attribution 4.0](https://creativecommons.org/licenses/by/4.0/)
(CC BY 4.0).

`afqos_app.py` and `qostopo.py` are adapted from this code. The allocation functions
(AQoS, `allocUnder`, `allocOver`, `allocation`), the rate approximation and the
traffic generator are kept as the authors wrote them, and remain under CC BY 4.0.

### Changes made to the authors' code

- MySQL logging replaced with a CSV file per run
- controller address set to `127.0.0.1` (the authors ran Ryu in a second VM)
- h6's IP address corrected (the original gave h6 and h7 the same address)
- traffic case chosen with `--case`; one trial per run instead of an endless loop
- a `static` baseline added; a blocking `input()` sanity check removed
- s2 meters set to the guaranteed rates as soon as the slice flows appear
- iperf clients use `--no-udp-fin` when available
- readable per-second console output

## The baseline algorithm

J. M. Boley, E.-S. Jung and R. Kettimuthu, *Adaptive QoS for data transfers using
software-defined networking*, 2017 (the AQoS baseline).

## Tools

[Mininet](https://mininet.org), [Open vSwitch](https://www.openvswitch.org),
[Ryu](https://github.com/faucetsdn/ryu) and [iperf](https://iperf.fr).

## This repository's own work

The web dashboard (including its static demo mode), the run and analysis scripts,
the VM setup and the documentation were written by Pranav Koradiya as a Computer
Networks mini project, and are released under the MIT license (see `LICENSE`).
