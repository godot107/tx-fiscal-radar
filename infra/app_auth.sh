#!/usr/bin/env bash
# Phase F: the Entra app registration for Fiscal Note Desk's sign-in (App Service Authentication).
# Tenant-level object: NOT in the resource group, so infra/down.sh doesn't remove it (see docs/TEARDOWN.md).
#   infra/app_auth.sh          create or update; writes APP_CLIENT_ID to .env
#   infra/app_auth.sh delete   remove it
# Single-tenant, ID-token sign-in, no client secret. It also exposes an API scope and pre-authorizes the Azure CLI,
# so `az account get-access-token --resource api://<appId>` works for the load test (Phase G) without a browser.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a
NAME=txfr-fiscal-note-desk
SUFFIX=${LAKE_ACCOUNT#sttxfr}
HOST="func-txfr-${SUFFIX}.azurewebsites.net"
AZ_CLI=04b07795-8ddb-461a-bbee-02f9e1bf7b46   # Microsoft Azure CLI (first-party public client)

APP_ID=$(az ad app list --display-name "$NAME" --query "[0].appId" -o tsv)
if [[ "${1:-}" == "delete" ]]; then
  [[ -n "$APP_ID" ]] && az ad app delete --id "$APP_ID" && echo "deleted $NAME ($APP_ID)"
  sed -i '/^APP_CLIENT_ID=/d' .env
  exit 0
fi
if [[ -z "$APP_ID" ]]; then
  APP_ID=$(az ad app create --display-name "$NAME" --sign-in-audience AzureADMyOrg \
           --web-redirect-uris "https://${HOST}/.auth/login/aad/callback" --enable-id-token-issuance true \
           --query appId -o tsv)
  echo "created $NAME: $APP_ID"
fi
OBJ=$(az ad app show --id "$APP_ID" --query id -o tsv)
SCOPE_ID=$(az ad app show --id "$APP_ID" --query "api.oauth2PermissionScopes[0].id" -o tsv)
SCOPE_ID=${SCOPE_ID:-$(python3 -c 'import uuid; print(uuid.uuid4())')}
# API: identifier api://<appId>, scope user_impersonation, Azure CLI pre-authorized (load test tokens)
BODY=$(python3 - "$APP_ID" "$SCOPE_ID" "$AZ_CLI" <<'PY'
import json, sys
app, scope, cli = sys.argv[1:]
print(json.dumps({"identifierUris": [f"api://{app}"], "tags": ["project=tx-fiscal-radar", "environment=dev", "managed-by=script"],
  "api": {"requestedAccessTokenVersion": 2,
          "oauth2PermissionScopes": [{"id": scope, "value": "user_impersonation", "type": "User", "isEnabled": True,
             "adminConsentDisplayName": "Use Fiscal Note Desk", "adminConsentDescription": "Ask Fiscal Note Desk questions",
             "userConsentDisplayName": "Use Fiscal Note Desk", "userConsentDescription": "Ask Fiscal Note Desk questions"}],
          "preAuthorizedApplications": [{"appId": cli, "delegatedPermissionIds": [scope]}]}}))
PY
)
# Graph needs the scope to exist before an app can be pre-authorized for it: two PATCHes
FIRST=$(python3 -c 'import json,sys; b=json.loads(sys.argv[1]); b["api"]["preAuthorizedApplications"]=[]; print(json.dumps(b))' "$BODY")
az rest --method patch --url "https://graph.microsoft.com/v1.0/applications/${OBJ}" --body "$FIRST" -o none
az rest --method patch --url "https://graph.microsoft.com/v1.0/applications/${OBJ}" --body "$BODY" -o none
# redirect URIs: the primary app, plus the DR app when it exists (Phase I)
URIS=("https://${HOST}/.auth/login/aad/callback")
[[ -n "${DR_APP_HOST:-}" ]] && URIS+=("https://${DR_APP_HOST}/.auth/login/aad/callback")
az ad app update --id "$APP_ID" --web-redirect-uris "${URIS[@]}"
az ad sp show --id "$APP_ID" -o none 2>/dev/null || az ad sp create --id "$APP_ID" -o none
sed -i '/^APP_CLIENT_ID=/d' .env && echo "APP_CLIENT_ID=${APP_ID}" >> .env
echo "APP_CLIENT_ID=${APP_ID} → .env (redirect https://${HOST}/.auth/login/aad/callback)"
