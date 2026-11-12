#!/usr/bin/env bash
# Phase I: deploy (or refresh) the DR pilot light in southcentralus and write DR_* values to .env. Times itself: the
# elapsed seconds are the infrastructure part of the app tier's recovery time (docs/DR.md).
#   infra/dr_up.sh              deploy rg-tx-fiscal-radar-dr
#   infra/dr_up.sh delete       remove it (az group delete)
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a
DR_RG=rg-tx-fiscal-radar-dr
DR_LOC=${DR_LOCATION:-westus3}   # needs Agent Service + the Function tool (not in southcentralus)
if [[ "${1:-}" == "delete" ]]; then az group delete -n "$DR_RG" --yes; sed -i '/^DR_/d' .env; exit 0; fi
T0=$(date +%s)
SUFFIX=${LAKE_ACCOUNT#sttxfr}
az group create -n "$DR_RG" -l "$DR_LOC" --tags project=tx-fiscal-radar environment=dr owner=williemaize \
  cost-center=personal managed-by=bicep repository=github.com/godot107/tx-fiscal-radar -o none
OUT=$(az deployment group create -g "$DR_RG" -n dr -f infra/dr.bicep -p primarySuffix="$SUFFIX" \
  primaryAppPrincipalId="$APP_PRINCIPAL_ID" appClientId="$APP_CLIENT_ID" drAgentId="${DR_AGENT_ID:-}" \
  operatorObjectId="$(az ad signed-in-user show --query id -o tsv)" \
  logWorkspaceResourceId="$(az monitor log-analytics workspace list -g rg-tx-fiscal-radar-dev --query '[0].id' -o tsv)" \
  searchName="srch-txfr-${SUFFIX}" sqlServerFqdn="$SQL_SERVER" sqlDatabase="$SQL_DATABASE" \
  --query properties.outputs -o json)
sed -i '/^DR_\(FOUNDRY_PROJECT_ENDPOINT\|OPENAI_ENDPOINT\|APP_NAME\|APP_URL\|APP_HOST\)=/d' .env
python3 - "$OUT" >> .env <<'PY'
import json, sys
o = json.loads(sys.argv[1])
m = {"drFoundryProjectEndpoint": "DR_FOUNDRY_PROJECT_ENDPOINT", "drOpenaiEndpoint": "DR_OPENAI_ENDPOINT",
     "drAppName": "DR_APP_NAME", "drAppUrl": "DR_APP_URL", "drAppHost": "DR_APP_HOST"}
print("\n".join(f"{v}={o[k]['value']}" for k, v in m.items()))
PY
echo "DR infrastructure in $(( $(date +%s) - T0 ))s → .env"
