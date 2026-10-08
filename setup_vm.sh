#!/usr/bin/env bash
# One-time setup inside the Ubuntu VM (Multipass). Takes about 10 minutes.
# Tested on Ubuntu 24.04; Ubuntu 22.04 works the same way.
set -e

echo "== 1/4 Mininet, Open vSwitch, iperf"
sudo apt-get update
sudo apt-get install -y mininet openvswitch-switch iperf python3-requests zip unzip curl
# tc "netem" (used for the 10 ms link delay) is in linux-modules-extra on Ubuntu cloud images
sudo apt-get install -y "linux-modules-extra-$(uname -r)" \
  || echo "!! linux-modules-extra not found: links will run without the 10 ms delay"
sudo systemctl enable --now openvswitch-switch

echo "== 2/4 Python 3.8 environment for Ryu (Ryu 4.34 does not run on newer Pythons)"
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
uv venv -p 3.8 "$HOME/ryu-env"
# shellcheck disable=SC1091
. "$HOME/ryu-env/bin/activate"
uv pip install "setuptools==67.6.1" pip wheel

echo "== 3/4 Ryu controller + libraries"
python -m pip install ryu
python -m pip install "eventlet==0.30.2" numpy requests matplotlib   # eventlet pin: newer ones break Ryu
grep -q "ryu-env/bin/activate" "$HOME/.bashrc" || echo '. "$HOME/ryu-env/bin/activate"' >> "$HOME/.bashrc"

echo "== 4/4 Checks"
ryu-manager --version
sudo mn --version
sudo mn -c >/dev/null 2>&1 || true
echo "Setup finished. Open a new shell (or run: . ~/ryu-env/bin/activate) and then: ./run_case.sh afqos 3 60"
