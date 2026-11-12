#!/usr/bin/env bash
# Create/refresh the dev resource group + infra/main.bicep, then write endpoints to .env (gitignored).
#   infra/up.sh              deploy
#   infra/up.sh --what-if    show what would change, deploy nothing
# Identity-specific values come from the shell, never from git:
#   OPERATOR_OBJECT_ID  Entra object ID given data-plane roles + SQL/Synapse admin (default: signed-in az user)
#   OPERATOR_LOGIN      login shown as SQL/Synapse admin (default: signed-in az user)
#   CLIENT_IPS          JSON array of IPs allowed through SQL/Synapse firewalls (default: this machine's public IP)
set -euo pipefail
cd "$(dirname "$0")/.."
set -a
# shellcheck source=/dev/null
source config/dev.env
set +a
TAGS=(project=tx-fiscal-radar environment="$TXFR_ENV" owner=williemaize cost-center=personal
      managed-by=bicep repository=github.com/godot107/tx-fiscal-radar)

az account show --query "{sub:name,quota:subscriptionPolicies.quotaId,spendingLimit:subscriptionPolicies.spendingLimit}" -o table
# Values written by later steps (app_auth.sh, txfr.agent create) that the template reads back
if [[ -f .env ]]; then
  for k in APP_CLIENT_ID AGENT_ID AGENT_ID_KNOWLEDGE DR_FOUNDRY_PROJECT_ENDPOINT DR_AGENT_ID; do
    v=$(grep -E "^${k}=" .env | tail -1 | cut -d= -f2-)
    [[ -n "$v" ]] && export "${k}=${v}"
  done
fi
export OPERATOR_OBJECT_ID=${OPERATOR_OBJECT_ID:-$(az ad signed-in-user show --query id -o tsv)}
export OPERATOR_LOGIN=${OPERATOR_LOGIN:-$(az account show --query user.name -o tsv)}
export CLIENT_IPS=${CLIENT_IPS:-"[\"$(curl -s https://api.ipify.org)\"]"}

if [[ "${1:-}" == "--what-if" ]]; then
  if [ "$(az group exists -n "$RG")" = "false" ]; then
    echo "resource group $RG does not exist: a deploy would create it and every resource in main.bicep"
    az bicep build -f infra/main.bicep --stdout > /dev/null && echo "main.bicep compiles"
    exit 0
  fi
  az deployment group what-if -g "$RG" -n main -p infra/params/dev.bicepparam
  exit 0
fi

az group create -n "$RG" -l "$LOCATION" --tags "${TAGS[@]}" -o none
az deployment group create -g "$RG" -n main -p infra/params/dev.bicepparam -o none
infra/env.sh
