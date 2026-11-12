#!/usr/bin/env bash
# Phase F: package and deploy Fiscal Note Desk to the Function App (remote build installs requirements.txt).
#   app/deploy.sh
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a
RG=rg-tx-fiscal-radar-dev
if [[ "${1:-}" == "dr" ]]; then   # Phase I: deploy the same package to the DR pilot-light app
  APP_NAME=$DR_APP_NAME; APP_URL=$DR_APP_URL; RG=rg-tx-fiscal-radar-dr
fi
: "${APP_NAME:?run infra/up.sh with deployApp=true first}"
BUILD=$(mktemp -d)
cp app/function_app.py app/host.json app/requirements.txt "$BUILD"/
cp -r app/static "$BUILD"/static
mkdir -p "$BUILD"/txfr

cp src/txfr/*.py "$BUILD"/txfr/   # all modules: heavy libraries (Spark, pandas) are imported inside functions, never at module load
(cd "$BUILD" && zip -qr app.zip .)
# Flex Consumption takes config-zip (the newer `az functionapp deploy` returns 415). With basic-auth publishing off,
# the CLI's post-deploy health check can hang after the deployment has finished, so cap its wait and verify ourselves.
az functionapp deployment source config-zip -g "$RG" -n "$APP_NAME" --src "$BUILD/app.zip" \
  --build-remote true --timeout 420 -o none || echo "(CLI wait ended; checking the app directly)"
for _ in $(seq 1 30); do   # 401 = the app is up behind sign-in
  [[ "$(curl -s -o /dev/null -w '%{http_code}' "${APP_URL}/api/health")" == "401" ]] && break
  sleep 10
done
rm -rf "$BUILD"
# Smoke test through the whole path (sign-in → app → agent → SQL). Cached after the first deploy of the day.
TOKEN=$(az account get-access-token --resource "api://${APP_CLIENT_ID}" --query accessToken -o tsv)
BODY=$(mktemp)
CODE=$(curl -s -m 200 -o "$BODY" -w '%{http_code}' -H "Authorization: Bearer ${TOKEN}" -H "Content-Type: application/json" \
       -X POST "${APP_URL}/api/ask" -d '{"question":"How many House and Senate bills were enacted in the 2025 regular session?"}')
if [[ "$CODE" == "429" ]] && grep -q '"daily limit"' "$BODY"; then
  # sign-in, app, storage and the cost cap all worked; the agent path wasn't exercised (deploying user is over today's cap)
  echo "smoke test: app up; deploying user is over today's cost cap, so the agent path was NOT exercised"
elif [[ "$CODE" == "200" ]]; then
  echo "smoke test: /api/ask 200"
else
  echo "smoke test failed: /api/ask returned $CODE: $(head -c 300 "$BODY")"; rm -f "$BODY"; exit 1
fi
rm -f "$BODY"
echo "deployed → ${APP_URL}"
