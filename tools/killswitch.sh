#!/usr/bin/env bash
# Blue team (respond): take Fiscal Note Desk (web + MCP) offline fast, and bring it back. docs/SECURITY.md → Incident runbook.
#   tools/killswitch.sh status
#   tools/killswitch.sh stop     # app stopped (every route, incl. /mcp) + sign-in disabled for the app registration
#   tools/killswitch.sh start    # reverse both
#   tools/killswitch.sh full     # stop + the data pipeline schedules (tr_daily, tr_weekly)
# Stopping is safe: no data is deleted, the warehouse and Foundry agent are untouched.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a
RG=rg-tx-fiscal-radar-dev

status() {
  echo "app:      $(az functionapp show -g "$RG" -n "$APP_NAME" --query state -o tsv)"
  echo "sign-in:  $(az ad sp show --id "$APP_CLIENT_ID" --query accountEnabled -o tsv) (accountEnabled)"
  for t in tr_daily tr_weekly; do
    echo "$t: $(az rest --method get --url "https://management.azure.com${DATA_FACTORY_ID}/triggers/$t?api-version=2018-06-01" --query properties.runtimeState -o tsv)"
  done
  echo "http:     $(curl -s -o /dev/null -w '%{http_code}' "$APP_URL/api/health") (401 = up behind sign-in; 403/503 = stopped)"
}

case "${1:-status}" in
  status) status ;;
  stop|full)
    az functionapp stop -g "$RG" -n "$APP_NAME"
    az ad sp update --id "$APP_CLIENT_ID" --set accountEnabled=false   # no new sign-ins or tokens for the app
    if [[ "$1" == "full" ]]; then
      for t in tr_daily tr_weekly; do
        az rest --method post --url "https://management.azure.com${DATA_FACTORY_ID}/triggers/$t/stop?api-version=2018-06-01" -o none
      done
    fi
    echo "stopped at $(date -u +%FT%TZ)"; status ;;
  start)
    az ad sp update --id "$APP_CLIENT_ID" --set accountEnabled=true
    az functionapp start -g "$RG" -n "$APP_NAME"
    echo "started at $(date -u +%FT%TZ) (pipeline schedules unchanged: start them separately if 'full' stopped them)"; status ;;
  *) echo "usage: $0 status|stop|start|full"; exit 1 ;;
esac
