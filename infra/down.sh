#!/usr/bin/env bash
# Delete EVERYTHING in the dev resource group (asks first). Synapse's managed RG goes with the workspace.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a
# shellcheck source=/dev/null
source config/dev.env
set +a
az resource list -g "$RG" -o table
read -r -p "Delete resource group $RG and all resources above? Type 'dev' to confirm: " ok
[[ "$ok" == "dev" ]] || { echo "aborted"; exit 1; }
az group delete -n "$RG" --yes
echo "Key Vault is soft-deleted for 7 days: az keyvault list-deleted -o table  (purge to reuse the name)"
echo "Verify nothing tagged is left: az resource list --tag project=tx-fiscal-radar -o table"
