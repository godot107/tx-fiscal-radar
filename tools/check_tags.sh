#!/usr/bin/env bash
# List every resource in the project's resource groups with its required tags; flag anything missing one.
#   tools/check_tags.sh            check
#   tools/check_tags.sh --fix-rg   also tag Synapse's managed resource group (created by Synapse, not Bicep)
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=/dev/null
source config/dev.env
MANAGED_RG=rg-txfr-synapse-managed-$TXFR_ENV
REQUIRED=(project environment owner cost-center managed-by)

if [[ "${1:-}" == "--fix-rg" ]] && [ "$(az group exists -n "$MANAGED_RG")" = "true" ]; then
  if az group update -n "$MANAGED_RG" --tags project=tx-fiscal-radar environment="$TXFR_ENV" owner=williemaize \
      cost-center=personal managed-by=synapse repository=github.com/godot107/tx-fiscal-radar -o none 2>/dev/null; then
    echo "tagged $MANAGED_RG (managed-by=synapse)"
  else
    # Synapse protects its managed RG with a deny assignment: nobody can write to it, tags included.
    echo "$MANAGED_RG: deny assignment (owned by Synapse) - cannot be tagged; it holds no billable resources without a managed VNet"
  fi
fi

missing=0
for rg in "$RG" "$MANAGED_RG"; do
  [ "$(az group exists -n "$rg")" = "true" ] || continue
  echo "== $rg  (group tag project=$(az group show -n "$rg" --query "tags.project" -o tsv))"
  while IFS=$'\t' read -r name type tags; do
    if [[ "$type" == "Microsoft.Sql/servers/databases" && "$name" == */master ]]; then
      echo "  --  $type/$name (system database: no tags, no cost)"; continue
    fi
    lacks=()
    for k in "${REQUIRED[@]}"; do [[ "$tags" == *"\"$k\""* ]] || lacks+=("$k"); done
    if ((${#lacks[@]})); then echo "  MISSING ${lacks[*]}: $type/$name"; missing=$((missing+1)); else echo "  ok  $type/$name"; fi
  done < <(az resource list -g "$rg" --query "[].[name,type,to_string(tags)]" -o tsv)
done
echo "untagged-or-incomplete resources: $missing"
echo "anything tagged project=tx-fiscal-radar outside these groups:"
az resource list --tag project=tx-fiscal-radar --query "[?resourceGroup!='$RG' && resourceGroup!='$MANAGED_RG'].id" -o tsv
