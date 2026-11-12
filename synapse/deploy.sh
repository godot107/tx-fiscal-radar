#!/usr/bin/env bash
# Publish the Synapse side: txfr.zip (parsers + Spark jobs) to the lake, notebooks, serverless SQL objects,
# and the Synapse RBAC the factory needs to run notebooks.
#   synapse/deploy.sh            everything
#   synapse/deploy.sh zip        only re-upload txfr.zip (after changing src/txfr)
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; source config/dev.env; set +a
TMP=$(mktemp -d)
( cd src && zip -qr "$TMP/txfr.zip" txfr -x '*/__pycache__/*' )
az storage fs file upload --auth-mode login --account-name "$LAKE_ACCOUNT" -f synapse --path libs/txfr.zip \
  --source "$TMP/txfr.zip" --overwrite -o none && echo "  libs/txfr.zip"
[[ "${1:-}" == "zip" ]] && exit 0

for nb in synapse/notebooks/*.ipynb; do
  name=$(basename "$nb" .ipynb)
  python3 -c 'import os,re,sys; print(re.sub(r"\$\{(\w+)\}", lambda m: os.environ[m.group(1)], open(sys.argv[1]).read()))' "$nb" > "$TMP/$name.ipynb"
  az synapse notebook import --workspace-name "$SYNAPSE_WORKSPACE" --name "$name" --file "@$TMP/$name.ipynb" \
    --spark-pool-name "$SPARK_POOL" --folder-path pipelines -o none && echo "  notebook $name"
done

# Data Factory runs notebooks as its managed identity: Synapse Contributor (run artifacts on pools) +
# Synapse Credential User (use the workspace identity, which reads/writes the lake).
for role in "Synapse Contributor" "Synapse Credential User"; do
  if ! az synapse role assignment list --workspace-name "$SYNAPSE_WORKSPACE" --role "$role" --assignee "$DATA_FACTORY_PRINCIPAL_ID" --query "[0].id" -o tsv 2>/dev/null | grep -q .; then
    az synapse role assignment create --workspace-name "$SYNAPSE_WORKSPACE" --role "$role" \
      --assignee-object-id "$DATA_FACTORY_PRINCIPAL_ID" --assignee-principal-type ServicePrincipal -o none
  fi
  echo "  role $role → $DATA_FACTORY"
done
rm -rf "$TMP"
