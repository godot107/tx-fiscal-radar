#!/usr/bin/env bash
# Start a Data Factory pipeline run (optionally with JSON parameters) and wait for it.
#   tools/adf_run.sh pl_daily
#   tools/adf_run.sh pl_daily '{"mode":"weekly"}'
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a
PL=$1; BODY=${2:-'{}'}
RUN=$(az rest --method post --url "https://management.azure.com${DATA_FACTORY_ID}/pipelines/$PL/createRun?api-version=2018-06-01" \
      --body "$BODY" --query runId -o tsv)
echo "started $PL: $RUN"
tools/adf_wait.sh "$RUN" "${WAIT_SECONDS:-7200}"
