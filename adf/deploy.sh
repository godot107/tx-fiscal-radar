#!/usr/bin/env bash
# Deploy the factory's JSON (this folder, ADF git-integration layout) through the ARM REST API.
# ${VAR} placeholders are filled from .env (infra/env.sh). Order: linked services → datasets → pipelines → triggers.
#   adf/deploy.sh                 deploy everything
#   adf/deploy.sh pipeline/pl_ingest.json   deploy one file
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; source config/dev.env; set +a
API="https://management.azure.com${DATA_FACTORY_ID}"
put() {  # $1 = file
  local f=$1 kind name body
  kind=$(basename "$(dirname "$f")"); name=$(basename "$f" .json)
  case $kind in linkedService) kind=linkedservices;; dataset) kind=datasets;; pipeline) kind=pipelines;; trigger) kind=triggers;; esac
  body=$(python3 -c 'import json,os,re,sys; t=re.sub(r"\$\{(\w+)\}", lambda m: os.environ[m.group(1)], open(sys.argv[1]).read()); print(json.dumps({"properties": json.loads(t)["properties"]}))' "$f")
  az rest --method put --url "$API/$kind/$name?api-version=2018-06-01" --body "$body" -o none && echo "  $kind/$name"
}
if (($#)); then for f in "$@"; do put "adf/$f"; done; exit 0; fi
for f in adf/linkedService/*.json adf/dataset/*.json; do put "$f"; done
# children before parents (ExecutePipeline references must exist)
for f in pl_copy_group pl_manifest_apply pl_ingest_manifest pl_ingest_source pl_ingest pl_transform pl_load_sql pl_daily; do [ -f "adf/pipeline/$f.json" ] && put "adf/pipeline/$f.json"; done
for f in adf/trigger/*.json; do [ -f "$f" ] && put "$f"; done
echo "Triggers are deployed Stopped. Start: az datafactory trigger start -g \$RG --factory-name \$DATA_FACTORY -n tr_daily"
