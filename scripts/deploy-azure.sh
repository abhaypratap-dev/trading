#!/usr/bin/env bash
# Deploy the `main` branch (no AI model) to an Azure VM with Docker.
#   scripts/deploy-azure.sh [resource-group] [vm-name] [--copy-data]
# Needs: az login done, SSH access to the VM (the key Azure set up for its admin user).
# What it does: packs the committed `main` branch (git archive - nothing uncommitted, no data, no .env),
# copies it and your .env (with LLM_ENABLED=false) to the VM, installs Docker there if missing and starts the stack.
# --copy-data also copies your local database (candles, trades, settings) to the VM, replacing the VM's.
set -euo pipefail
cd "$(dirname "$0")/.."
COPY_DATA=0; ARGS=()
for a in "$@"; do [ "$a" = "--copy-data" ] && COPY_DATA=1 || ARGS+=("$a"); done
RG="${ARGS[0]:-}"; VM="${ARGS[1]:-}"

if [ -z "$VM" ]; then
  echo "Azure VMs in this subscription:"; az vm list -d --query "[].{name:name, group:resourceGroup, ip:publicIps, state:powerState, size:hardwareProfile.vmSize}" -o table
  n=$(az vm list --query "length(@)" -o tsv)
  if [ "$n" != "1" ]; then echo "usage: $0 <resource-group> <vm-name> [--copy-data]"; exit 1; fi
  RG=$(az vm list --query "[0].resourceGroup" -o tsv); VM=$(az vm list --query "[0].name" -o tsv)
fi
IP=$(az vm show -d -g "$RG" -n "$VM" --query publicIps -o tsv)
USR=$(az vm show -g "$RG" -n "$VM" --query osProfile.adminUsername -o tsv)
[ -n "$IP" ] || { echo "VM $VM has no public IP (is it running?)"; exit 1; }
echo "==> deploying branch main to $VM ($USR@$IP)"
SSH=(ssh -o StrictHostKeyChecking=accept-new "$USR@$IP")

git rev-parse --verify main >/dev/null
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
git archive --format=tar.gz -o "$TMP/yonexis.tgz" main
grep -v '^LLM_ENABLED=' .env > "$TMP/yonexis.env"; echo "LLM_ENABLED=false" >> "$TMP/yonexis.env"
scp -o StrictHostKeyChecking=accept-new "$TMP/yonexis.tgz" "$TMP/yonexis.env" "$USR@$IP:/tmp/"

"${SSH[@]}" 'bash -s' <<'REMOTE'
set -e
if ! command -v docker >/dev/null; then
  echo "==> installing Docker"; curl -fsSL https://get.docker.com | sudo sh; sudo usermod -aG docker "$USER"
fi
mkdir -p ~/yonexis && tar -xzf /tmp/yonexis.tgz -C ~/yonexis && rm /tmp/yonexis.tgz
install -m 600 /tmp/yonexis.env ~/yonexis/.env && rm /tmp/yonexis.env
mkdir -p ~/yonexis/data/sandbox_queue && sudo chown -R 1000:1000 ~/yonexis/data
cd ~/yonexis
echo "==> building and starting (first time takes a few minutes)"
sudo docker compose -f docker-compose.yml -f docker-compose.azure.yml up -d --build
sudo docker compose ps
REMOTE

if [ "$COPY_DATA" = "1" ]; then
  echo "==> copying the local database to the VM (replaces the VM's data)"
  docker compose exec -T db pg_dump -U yonexis -Fc yonexis > "$TMP/db.dump"
  scp "$TMP/db.dump" "$USR@$IP:/tmp/db.dump"
  "${SSH[@]}" 'bash -s' <<'REMOTE'
set -e
cd ~/yonexis
C="sudo docker compose -f docker-compose.yml -f docker-compose.azure.yml"
$C stop api scheduler
$C exec -T db psql -U yonexis -d yonexis -c "SELECT timescaledb_pre_restore();"
$C exec -T db pg_restore -U yonexis -d yonexis --clean --if-exists --no-owner < /tmp/db.dump || true
$C exec -T db psql -U yonexis -d yonexis -c "SELECT timescaledb_post_restore();"
rm /tmp/db.dump
$C start api scheduler
REMOTE
fi

echo
echo "Done. Open the dashboard through an SSH tunnel (it is not exposed to the internet):"
echo "   ssh -N -L 8000:localhost:8000 $USR@$IP     then open http://localhost:8000"
