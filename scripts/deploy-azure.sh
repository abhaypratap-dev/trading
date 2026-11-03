#!/usr/bin/env bash
# Deploy the `main` branch (no AI model) to an Azure VM with Docker.
#   scripts/deploy-azure.sh <resource-group> <vm-name> [--copy-data | --data-only]   (VM must be tagged app=yonexis)
# Needs: az login done, SSH access to the VM (the key Azure set up for its admin user).
# What it does: packs the committed `main` branch (git archive - nothing uncommitted, no data, no .env),
# copies it and your .env (with LLM_ENABLED=false) to the VM, installs Docker there if missing and starts the stack.
# --copy-data also copies your local database (candles, trades, settings) to the VM, replacing the VM's.
# --data-only copies only the database (no code redeploy).
set -euo pipefail
cd "$(dirname "$0")/.."
COPY_DATA=0; DATA_ONLY=0; ARGS=()
for a in "$@"; do case "$a" in --copy-data) COPY_DATA=1;; --data-only) COPY_DATA=1; DATA_ONLY=1;; *) ARGS+=("$a");; esac; done
RG="${ARGS[0]:-}"; VM="${ARGS[1]:-}"

if [ -z "$RG" ] || [ -z "$VM" ]; then
  echo "usage: $0 <resource-group> <vm-name> [--copy-data | --data-only]   (create one first: scripts/create-azure-vm.sh)"; exit 1
fi
# safety: only a VM created for YONEXIS (tag app=yonexis) - never another server that happens to be in the subscription
if [ "$(az vm show -g "$RG" -n "$VM" --query tags.app -o tsv 2>/dev/null)" != "yonexis" ]; then
  echo "Refusing: VM $VM in $RG is not tagged app=yonexis (it was not created for YONEXIS)."; exit 1
fi
IP=$(az vm show -d -g "$RG" -n "$VM" --query publicIps -o tsv)
USR=$(az vm show -g "$RG" -n "$VM" --query osProfile.adminUsername -o tsv)
[ -n "$IP" ] || { echo "VM $VM has no public IP (is it running?)"; exit 1; }
if [ "$DATA_ONLY" = "1" ]; then
  echo "==> uploading the local database to $VM ($USR@$IP) - replaces the VM's data, code is not redeployed"
  read -r -p "Upload? [y/N] " ok
else
  echo "==> deploying branch main to $VM ($USR@$IP)"
  read -r -p "Deploy to this VM? [y/N] " ok
fi
[ "$ok" = "y" ] || [ "$ok" = "Y" ] || exit 1
SSH=(ssh -o StrictHostKeyChecking=accept-new "$USR@$IP")
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT

if [ "$DATA_ONLY" = "0" ]; then

git rev-parse --verify main >/dev/null
git archive --format=tar.gz -o "$TMP/yonexis.tgz" main
grep -v -e '^LLM_ENABLED=' -e '^MT5_COMMON_DIR=' .env > "$TMP/yonexis.env"; echo "LLM_ENABLED=false" >> "$TMP/yonexis.env"
# public URL set up by azure-public.sh -> Upstox must send the login back there, not to localhost
FQDN=$(az vm show -d -g "$RG" -n "$VM" --query fqdns -o tsv)
if [ -n "$FQDN" ]; then
  sed -i.bak '/^UPSTOX_REDIRECT_URI=/d' "$TMP/yonexis.env"
  echo "UPSTOX_REDIRECT_URI=https://$FQDN/api/upstox/callback" >> "$TMP/yonexis.env"
fi
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
fi

if [ "$COPY_DATA" = "1" ]; then
  echo "==> copying the local database to the VM (replaces the VM's data)"
  docker compose exec -T db pg_dump -U yonexis -Fc yonexis > "$TMP/db.dump"
  scp "$TMP/db.dump" "$USR@$IP:/tmp/db.dump"
  "${SSH[@]}" 'bash -s' <<'REMOTE'
set -e
cd ~/yonexis
C="sudo docker compose -f docker-compose.yml -f docker-compose.azure.yml"
# always bring the app back, even if a step below fails
trap '$C start api scheduler' EXIT
$C stop api scheduler
# psql execs get </dev/null: otherwise they read the rest of this script from bash -s's stdin and it silently ends
# restore into a fresh, empty database (TimescaleDB's recommended way; --clean over hypertables is unreliable)
$C exec -T db psql -U yonexis -d postgres -c "DROP DATABASE IF EXISTS yonexis WITH (FORCE);" -c "CREATE DATABASE yonexis;" </dev/null
$C exec -T db psql -U yonexis -d yonexis -c "CREATE EXTENSION IF NOT EXISTS timescaledb;" -c "SELECT timescaledb_pre_restore();" </dev/null
$C exec -T db pg_restore -U yonexis -d yonexis  --no-owner < /tmp/db.dump 2>&1 | grep -v 'already exists' || true
$C exec -T db psql -U yonexis -d yonexis -c "SELECT timescaledb_post_restore();" </dev/null
$C exec -T db psql -U yonexis -d yonexis -Atc "SELECT 'restored: ' || (SELECT count(*) FROM watchlist) || ' watchlist symbols, ' || (SELECT count(*) FROM candles) || ' candles';" </dev/null
rm /tmp/db.dump
REMOTE
fi

echo
echo "Done. Open the dashboard through an SSH tunnel (it is not exposed to the internet):"
echo "   ssh -N -L 8000:localhost:8000 $USR@$IP     then open http://localhost:8000"
