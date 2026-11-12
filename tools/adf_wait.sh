#!/usr/bin/env bash
# Wait for a Data Factory pipeline run and print its status plus each activity's result.
#   tools/adf_wait.sh <runId> [timeout_seconds]
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a
RUN=$1; LIMIT=${2:-1800}; API="https://management.azure.com${DATA_FACTORY_ID}"
for ((t=0; t<LIMIT; t+=20)); do
  s=$(az rest --method get --url "$API/pipelineruns/$RUN?api-version=2018-06-01" --query status -o tsv)
  [[ "$s" =~ ^(Succeeded|Failed|Cancelled)$ ]] && break
  sleep 20
done
echo "pipeline run $RUN: $s"
now=$(date -u +%Y-%m-%dT%H:%M:%SZ)
az rest --method post --url "$API/pipelineruns/$RUN/queryActivityruns?api-version=2018-06-01" \
  --body "{\"lastUpdatedAfter\":\"2026-01-01T00:00:00Z\",\"lastUpdatedBefore\":\"$now\"}" \
  --query "value[].{activity:activityName,status:status,error:error.message,filesRead:output.filesRead,written:output.filesWritten,bytes:output.dataRead}" -o table
[ "$s" = "Succeeded" ]
