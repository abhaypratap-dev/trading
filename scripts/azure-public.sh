#!/usr/bin/env bash
# Make the dashboard on the Azure VM reachable at a public HTTPS URL, behind a username/password.
#   scripts/azure-public.sh [resource-group=yonexis-rg] [vm-name=yonexis-vm] [dns-label=yonexis-dash]
# - gives the VM's public IP the name <dns-label>.<region>.cloudapp.azure.com
# - opens ports 80/443 (SSH stays limited to your IP)
# - runs Caddy on the VM: free Let's Encrypt certificate + basic auth in front of the API (127.0.0.1:8000)
# - points the VM's Upstox redirect at https://<name>/api/upstox/callback
# Caddy lives in ~/caddy on the VM, outside ~/yonexis, so deploy-azure.sh never touches it.
# The password is generated once and saved to ~/.yonexis-dashboard-pass on this Mac (rerun keeps it).
set -euo pipefail
RG="${1:-yonexis-rg}"; VM="${2:-yonexis-vm}"; LABEL="${3:-yonexis-dash}"
if [ "$(az vm show -g "$RG" -n "$VM" --query tags.app -o tsv 2>/dev/null)" != "yonexis" ]; then
  echo "Refusing: VM $VM in $RG is not tagged app=yonexis."; exit 1
fi
NIC=$(az vm show -g "$RG" -n "$VM" --query "networkProfile.networkInterfaces[0].id" -o tsv)
PIPID=$(az network nic show --ids "$NIC" --query "ipConfigurations[0].publicIPAddress.id" -o tsv)
NSG=$(az network nic show --ids "$NIC" --query "networkSecurityGroup.id" -o tsv | awk -F/ '{print $NF}')
az network public-ip update --ids "$PIPID" --dns-name "$LABEL" -o none
FQDN=$(az network public-ip show --ids "$PIPID" --query dnsSettings.fqdn -o tsv)
IP=$(az network public-ip show --ids "$PIPID" --query ipAddress -o tsv)
USR=$(az vm show -g "$RG" -n "$VM" --query osProfile.adminUsername -o tsv)
az network nsg rule create -g "$RG" --nsg-name "$NSG" -n allow-web --priority 1010 --access Allow --protocol Tcp \
   --direction Inbound --destination-port-ranges 80 443 --source-address-prefixes Internet -o none

PASSFILE=~/.yonexis-dashboard-pass
[ -s "$PASSFILE" ] || { (umask 077; openssl rand -base64 18 | tr -d '/+=' > "$PASSFILE"); }
PASS=$(cat "$PASSFILE")

ssh -o StrictHostKeyChecking=accept-new "$USR@$IP" "FQDN='$FQDN' PASS='$PASS' bash -s" <<'REMOTE'
set -e
mkdir -p ~/caddy
HASH=$(sudo docker run --rm caddy:2.8.4 caddy hash-password --plaintext "$PASS")
cat > ~/caddy/Caddyfile <<EOF
$FQDN {
	encode gzip
	basic_auth {
		yonexis $HASH
	}
	reverse_proxy 127.0.0.1:8000
}
EOF
sudo docker rm -f caddy >/dev/null 2>&1 || true
sudo docker run -d --name caddy --restart unless-stopped --network host \
  -v ~/caddy/Caddyfile:/etc/caddy/Caddyfile:ro -v caddy_data:/data -v caddy_config:/config caddy:2.8.4 >/dev/null
cd ~/yonexis
sed -i '/^UPSTOX_REDIRECT_URI=/d' .env && echo "UPSTOX_REDIRECT_URI=https://$FQDN/api/upstox/callback" >> .env
sudo docker compose -f docker-compose.yml -f docker-compose.azure.yml up -d api scheduler >/dev/null
REMOTE

echo
echo "Dashboard: https://$FQDN   (user: yonexis, password in $PASSFILE)"
echo "Upstox: set the app's Redirect URL to https://$FQDN/api/upstox/callback (developer console)."
