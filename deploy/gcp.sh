#!/usr/bin/env bash
# Put the simulation (twin + agent harness + dashboard) on a small Google Cloud VM, public.
#
#   deploy/gcp.sh create        its own network (80/443/22 open), static address, Debian 13 VM (e2-small)
#                               with no service account: the box holds no Google credentials and can't
#                               reach other VMs in the project
#   deploy/gcp.sh push [off]    copy the code and recorded runs, install, (re)start; "off" serves plain HTTP only
#   deploy/gcp.sh key           copy ANTHROPIC_API_KEY from .env into a root-only file on the VM
#   deploy/gcp.sh url | status | logs | ssh | stop | start | delete | bundle <file>
#
# Settings: PROJECT (default: gcloud's), ZONE (us-central1-a), VM (aisim), MACHINE (e2-small).
set -euo pipefail
PROJECT=${PROJECT:-$(gcloud config get-value project 2>/dev/null)}
ZONE=${ZONE:-us-central1-a}
REGION=${ZONE%-*}
VM=${VM:-aisim}
MACHINE=${MACHINE:-e2-small}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
SEED_RUNS="20261012_182240 20261012_183208"   # recorded runs visitors can replay

g() { gcloud --project "$PROJECT" "$@"; }
ip() { g compute addresses describe "$VM-ip" --region "$REGION" --format='value(address)'; }
host() { echo "$(ip | tr . -).sslip.io"; }
on_vm() { g compute ssh "$VM" --zone "$ZONE" --quiet --command "$1"; }

bundle() {   # the runtime files only: never .env or other local files
  local out=$1 runs=() sessions=()
  for r in $SEED_RUNS; do
    runs+=("harness_runs/$r")
    for s in $(grep -o 'sim_sessions/sim_[0-9_-]*' "$ROOT/harness_runs/$r/trace.jsonl" | sort -u); do sessions+=("$s"); done
  done
  (cd "$ROOT" && COPYFILE_DISABLE=1 tar czf "$out" --no-xattrs --no-mac-metadata --exclude='__pycache__' --exclude='.DS_Store' --exclude='.mpl' \
    twin/*.py twin/operator.html twin/README.md harness/*.py harness/README.md dashboard \
    mhapi/__init__.py mhapi/tools.py mhapi/client.py mhapi/cli.py mhapi/safety.toml \
    calibration/well_positions_fit.json experiment_protocol.md tests/test_twin.py tests/test_harness.py \
    deploy "${runs[@]}" "${sessions[@]}")
  if tar tzf "$out" | grep -qE '(^|/)\.env$'; then echo "refusing: .env in the bundle" >&2; exit 1; fi
}

case "${1:-}" in
  create)
    g services enable compute.googleapis.com
    g compute networks describe "$VM-net" >/dev/null 2>&1 ||
      g compute networks create "$VM-net" --subnet-mode custom
    g compute networks subnets describe "$VM-subnet" --region "$REGION" >/dev/null 2>&1 ||
      g compute networks subnets create "$VM-subnet" --network "$VM-net" --region "$REGION" --range 10.200.0.0/24
    g compute firewall-rules describe "$VM-web" >/dev/null 2>&1 ||
      g compute firewall-rules create "$VM-web" --network "$VM-net" --direction INGRESS --action ALLOW \
        --rules tcp:80,tcp:443 --source-ranges 0.0.0.0/0
    g compute firewall-rules describe "$VM-ssh" >/dev/null 2>&1 ||
      g compute firewall-rules create "$VM-ssh" --network "$VM-net" --direction INGRESS --action ALLOW \
        --rules tcp:22 --source-ranges 0.0.0.0/0
    g compute addresses describe "$VM-ip" --region "$REGION" >/dev/null 2>&1 ||
      g compute addresses create "$VM-ip" --region "$REGION"
    g compute instances describe "$VM" --zone "$ZONE" >/dev/null 2>&1 ||
      g compute instances create "$VM" --zone "$ZONE" --machine-type "$MACHINE" \
        --image-family debian-13 --image-project debian-cloud --boot-disk-size 20GB --boot-disk-type pd-balanced \
        --subnet "$VM-subnet" --address "$(ip)" --no-service-account --no-scopes \
        --metadata block-project-ssh-keys=TRUE --labels app=aisim
    echo "VM $VM at $(ip); next: deploy/gcp.sh push && deploy/gcp.sh key"
    ;;
  push)   # BUNDLE=<file> pushes a bundle made earlier (deploy/gcp.sh bundle <file>)
    tmp=$(mktemp -d)
    if [ -n "${BUNDLE:-}" ]; then cp "$BUNDLE" "$tmp/aisim.tgz"; else bundle "$tmp/aisim.tgz"; fi
    echo "bundle: $(du -h "$tmp/aisim.tgz" | cut -f1)"
    g compute scp "$tmp/aisim.tgz" "$VM:/tmp/aisim.tgz" --zone "$ZONE" --quiet
    rm -rf "$tmp"
    on_vm "rm -rf /tmp/aisim && mkdir /tmp/aisim && tar xzf /tmp/aisim.tgz -C /tmp/aisim && sudo bash /tmp/aisim/deploy/setup_vm.sh $(host) $(ip) ${2:-auto}"
    "$0" url
    ;;
  key)
    grep -qE '^(export )?ANTHROPIC_API_KEY=.' "$ROOT/.env" || { echo "no ANTHROPIC_API_KEY in .env" >&2; exit 1; }
    grep -E '^(export )?ANTHROPIC_API_KEY=' "$ROOT/.env" | sed 's/^export //' |
      on_vm "sudo sh -c 'mkdir -p /etc/aisim && chmod 700 /etc/aisim && umask 077 && cat > /etc/aisim/secrets.env' &&
             echo 'key installed' && (sudo systemctl restart aisim-harness 2>/dev/null && echo 'harness restarted' || true)"
    ;;
  bundle)
    bundle "${2:-aisim.tgz}"
    ;;
  url)
    echo "https://$(host)/   (plain HTTP: http://$(ip)/)"
    ;;
  status)
    on_vm "systemctl is-active aisim-twin aisim-harness caddy; df -h /opt/aisim/app/harness_runs | tail -1; free -m | head -2"
    ;;
  logs)
    on_vm "sudo journalctl -u aisim-harness -u aisim-twin -u caddy -n ${2:-80} --no-pager"
    ;;
  ssh)
    g compute ssh "$VM" --zone "$ZONE"
    ;;
  stop | start)
    g compute instances "$1" "$VM" --zone "$ZONE"
    ;;
  delete)
    g compute instances delete "$VM" --zone "$ZONE" --quiet
    g compute addresses delete "$VM-ip" --region "$REGION" --quiet
    g compute firewall-rules delete "$VM-web" "$VM-ssh" --quiet
    g compute networks subnets delete "$VM-subnet" --region "$REGION" --quiet
    g compute networks delete "$VM-net" --quiet
    ;;
  *)
    sed -n '2,10p' "$0"
    exit 1
    ;;
esac
