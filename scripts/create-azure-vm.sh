#!/usr/bin/env bash
# Create a NEW Azure VM just for YONEXIS (never touches other resource groups).
#   scripts/create-azure-vm.sh [resource-group=yonexis-rg] [vm-name=yonexis-vm] [regions...]
# Regions are tried in order (India first, then nearby); in each region only sizes that this subscription may use
# there (az vm list-skus, no location restriction) are tried, cheapest 2 vCPU / 8 GB first.
# Ubuntu 22.04, 64 GB disk, tagged app=yonexis (deploy-azure.sh only deploys to VMs with that tag).
# SSH is allowed only from your current public IP; the dashboard itself is never opened to the internet.
set -euo pipefail
RG="${1:-yonexis-rg}"; VM="${2:-yonexis-vm}"; shift $(( $# > 2 ? 2 : $# )) || true
REGIONS="${*:-centralindia southindia westindia southeastasia eastasia uaenorth northeurope westeurope eastus}"
SIZES="${SIZES:-Standard_B2ms Standard_B2as_v2 Standard_B2s_v2 Standard_D2as_v5 Standard_D2s_v5 Standard_D2as_v4 Standard_D2s_v4 Standard_D2s_v3 Standard_DC2ds_v3 Standard_D2ads_v5 Standard_D2ds_v5}"
if az vm show -g "$RG" -n "$VM" >/dev/null 2>&1; then echo "VM $VM already exists in $RG - nothing to create."; exit 0; fi
echo "Will create: VM $VM in resource group $RG, Ubuntu 22.04, 64 GB disk, 2 vCPU / 8 GB."
echo "Regions tried in order: $REGIONS"
read -r -p "Create it? This starts Azure billing for the VM. [y/N] " ok; [ "$ok" = "y" ] || [ "$ok" = "Y" ] || exit 1
first="${REGIONS%% *}"
if [ "$(az group exists -n "$RG")" != "true" ]; then az group create -n "$RG" -l "$first" --tags app=yonexis -o none; fi
ERR=$(mktemp); trap 'rm -f "$ERR"' EXIT; CREATED=""
for LOC in $REGIONS; do
  echo "==> $LOC: checking which sizes this subscription may use"
  ALLOWED=$(az vm list-skus -l "$LOC" --resource-type virtualMachines \
            --query "[?length(restrictions[?type=='Location'])==\`0\`].name" -o tsv 2>/dev/null || true)
  [ -n "$ALLOWED" ] || { echo "    no sizes available to this subscription here"; continue; }
  for SIZE in $SIZES; do
    grep -qx "$SIZE" <<<"$ALLOWED" || continue
    echo "    trying $SIZE"
    if az vm create -g "$RG" -n "$VM" -l "$LOC" --image Ubuntu2204 --size "$SIZE" --admin-username yonexis \
         --generate-ssh-keys --public-ip-sku Standard --os-disk-size-gb 64 --nsg-rule SSH --tags app=yonexis \
         --vnet-name "$VM-vnet-$LOC" --nsg "$VM-nsg-$LOC" --public-ip-address "$VM-ip-$LOC" -o table 2>"$ERR"; then
      CREATED="$SIZE in $LOC"; CLOC="$LOC"; break 2
    fi
    echo "      failed: $(grep -m1 -oE '(Message|ERROR): .{0,220}' "$ERR" || tail -n1 "$ERR")"
  done
done
[ -n "$CREATED" ] || { echo "No region had a usable size. Paste this output to Claude."; exit 1; }
echo "==> created $VM: $CREATED"
MYIP=$(curl -fsS https://api.ipify.org)
NSG="$VM-nsg-$CLOC"
az network nsg rule update -g "$RG" --nsg-name "$NSG" -n default-allow-ssh --source-address-prefixes "$MYIP/32" -o none
echo
echo "VM ready. SSH allowed only from $MYIP (if your IP changes, edit rule default-allow-ssh in $NSG)."
echo "Next: scripts/deploy-azure.sh $RG $VM"
