#!/usr/bin/env bash
# Install or update the simulation on a Debian 13 VM. Run as root from the unpacked bundle (deploy/gcp.sh push):
#   sudo bash deploy/setup_vm.sh <hostname> <ip> [auto|off]
# auto: HTTPS for <hostname> (Caddy gets a Let's Encrypt certificate) and plain HTTP on the bare address;
# off: plain HTTP for both. Re-running updates the code and restarts the services; recorded runs, twin
# sessions, the limits in /etc/aisim/aisim.env and the API key in /etc/aisim/secrets.env are kept.
set -euo pipefail
HOST=${1:?usage: setup_vm.sh <hostname> <ip> [auto|off]}
IP=${2:?usage: setup_vm.sh <hostname> <ip> [auto|off]}
TLS=${3:-auto}
SRC=$(cd "$(dirname "$0")/.." && pwd)
BASE=/opt/aisim
APP=$BASE/app
VENV=$BASE/venv
export DEBIAN_FRONTEND=noninteractive

echo "== packages"
need=()
for p in python3-venv bubblewrap caddy curl e2fsprogs util-linux; do dpkg -s "$p" >/dev/null 2>&1 || need+=("$p"); done
if [ ${#need[@]} -gt 0 ]; then
  apt-get update -q
  apt-get install -y -q "${need[@]}"
fi

echo "== user and folders"
id aisim >/dev/null 2>&1 || useradd --system --home-dir $BASE --no-create-home --shell /usr/sbin/nologin aisim
mkdir -p $APP/sim_sessions $APP/harness_runs /var/lib/aisim /etc/aisim
chmod 700 /etc/aisim

echo "== swap (2 GB)"
if ! swapon --show=NAME --noheadings | grep -qx /swapfile; then
  if [ ! -f /swapfile ]; then fallocate -l 2G /swapfile; chmod 600 /swapfile; mkswap /swapfile >/dev/null; fi
  swapon /swapfile
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "== runs volume (5 GB file system of its own, so the agent's analysis output can't fill the disk)"
if ! mountpoint -q $APP/harness_runs; then
  if [ ! -f /var/lib/aisim/runs.img ]; then
    fallocate -l 5G /var/lib/aisim/runs.img
    mkfs.ext4 -q -F -m 0 /var/lib/aisim/runs.img
  fi
  grep -q 'runs.img' /etc/fstab || echo "/var/lib/aisim/runs.img $APP/harness_runs ext4 loop,nosuid,nodev,noexec 0 2" >> /etc/fstab
  systemctl daemon-reload
  mount $APP/harness_runs
  rmdir $APP/harness_runs/lost+found 2>/dev/null || true
fi

echo "== code"
find $APP -mindepth 1 -maxdepth 1 ! -name harness_runs ! -name sim_sessions -exec rm -rf {} +
for d in twin harness dashboard mhapi calibration tests deploy; do cp -r "$SRC/$d" $APP/; done
cp "$SRC/experiment_protocol.md" $APP/
chmod -R u=rwX,go=rX $APP/twin $APP/harness $APP/dashboard $APP/mhapi $APP/calibration $APP/tests $APP/deploy
# recorded runs to replay (only those not there yet)
[ -d "$SRC/harness_runs" ] && cp -r --update=none "$SRC/harness_runs/." $APP/harness_runs/
[ -d "$SRC/sim_sessions" ] && cp -r --update=none "$SRC/sim_sessions/." $APP/sim_sessions/
chown -R aisim:aisim $APP/harness_runs $APP/sim_sessions

echo "== python"
[ -x $VENV/bin/python ] || python3 -m venv $VENV
$VENV/bin/pip install -q --disable-pip-version-check -r $APP/deploy/requirements.txt
$VENV/bin/python -m compileall -q $APP/twin $APP/harness $APP/mhapi >/dev/null

echo "== settings"
if [ ! -f /etc/aisim/aisim.env ]; then
  umask 077
  cat > /etc/aisim/aisim.env <<EOF
# Public demo limits (harness/limits.py). After editing: systemctl restart aisim-harness
AISIM_PUBLIC=1
AISIM_RUN_USD=6
AISIM_DAY_USD=40
AISIM_RUN_MIN=90
AISIM_IP_RUNS=4
# Open the site once as /?admin=<this token> to lift the limits in that browser
AISIM_ADMIN_TOKEN=$(head -c 18 /dev/urandom | base64 | tr '+/' '-_')
EOF
  umask 022
fi
[ -f /etc/aisim/secrets.env ] || echo "   note: no API key yet; run deploy/gcp.sh key"

echo "== services"
install -m 644 $APP/deploy/aisim-twin.service $APP/deploy/aisim-harness.service /etc/systemd/system/
if [ "$TLS" = off ]; then SITES="http://$HOST, http://$IP"; else SITES="$HOST, http://$IP"; fi
sed "s|__SITES__|$SITES|" $APP/deploy/Caddyfile > /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1 || { echo "bad Caddyfile"; exit 1; }
systemctl daemon-reload
systemctl enable -q aisim-twin aisim-harness caddy
systemctl restart aisim-twin
systemctl restart aisim-harness
systemctl restart caddy
for i in $(seq 30); do curl -sf -o /dev/null http://127.0.0.1:8750/api/config && break; sleep 1; done
systemctl is-active aisim-twin aisim-harness caddy | paste -sd' ' | sed 's/^/   twin, harness, caddy: /'
curl -sf http://127.0.0.1:8750/api/config >/dev/null && echo "   dashboard answering"
