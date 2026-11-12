# Teardown

How to remove everything this project created, when the project is done.
**Not run yet.** As of 2026-11-10 everything is deployed and the daily schedules are on.

## When

- The free-trial credit ends in **early December 2026**. With the spending limit on, the subscription is then disabled,
  not billed. Tear down before then, or when the project is done, whichever comes first.
- **To pause instead of delete:** stop the two schedules (step 0). Nothing else runs on its own: Spark auto-pauses,
  serverless SQL and the free-offer database cost nothing idle. Running cost drops from about $20/month to nearly $0.

## Before deleting

- [ ] **Billed cost for COSTS.md:** `.venv/bin/python tools/cost_report.py`. Cost data for a deleted resource group is harder to get.
- [ ] **Final screenshots or demo recording:** if you want any, take them now (the report needs the data behind it).
- [ ] **Agent eval:** `docs/agent_eval.json` and `docs/runs/*.json` are already in git; nothing else needs saving.

## Steps

All commands run from the project folder. Names come from `.env` and `config/dev.env`, so nothing is typed by hand.

### 0. Stop the schedules and the app (also the "pause" option)

`tools/killswitch.sh full` stops the web app, disables sign-in to it and stops both pipeline schedules in one step
(reverse with `start` + starting the triggers). Or just the schedules:

```bash
set -a; source .env; set +a
for t in tr_daily tr_weekly; do
  az rest --method post --url "https://management.azure.com${DATA_FACTORY_ID}/triggers/$t/stop?api-version=2018-06-01"
done
```

### 1. Record the names of soft-deleted resources, then delete the resource group

Key Vault and the Foundry (Cognitive Services) account stay recoverable after deletion, so note their names first.

```bash
set -a; source .env; source config/dev.env; set +a
KV=$(echo "$KEY_VAULT_URI" | sed -E 's#https://([^.]+)\..*#\1#')
AI=$(az resource list -g "$RG" --resource-type Microsoft.CognitiveServices/accounts --query "[0].name" -o tsv)
AI_LOC=$(az resource list -g "$RG" --resource-type Microsoft.CognitiveServices/accounts --query "[0].location" -o tsv)
echo "$KV $AI $AI_LOC"          # keep these for step 2

infra/down.sh                   # lists every resource, asks you to type 'dev', deletes rg-tx-fiscal-radar-dev
```

The resource group holds everything: data lake, Data Factory, Synapse (its managed resource group goes with it), Azure
SQL, Key Vault, Log Analytics + alert, budgets, AI Search, Foundry account + project + agent. Deletion takes 10-20 minutes. Since Phase F it also holds the Fiscal Note Desk Function App (`func-txfr-*`), its
storage (`stfntxfr*`, incl. the answer cache and spend table) and Application Insights, the cost-breaker Logic App
and its action group, the alert rules, the embedding deployment, the second agent and the Foundry memory store.

### 1b. The DR pilot light (second region, Phase I)

```bash
infra/dr_up.sh delete                                              # deletes rg-tx-fiscal-radar-dr, removes DR_* from .env
az cognitiveservices account purge -g rg-tx-fiscal-radar-dr -n ai-txfr-dr-<suffix> -l westus3   # release the name
```

The lake's geo-redundancy (Standard_GRS) goes with the primary resource group in step 1.

### 2. Purge the soft-deleted resources

```bash
az keyvault purge -n "$KV" --location "$LOCATION"
az cognitiveservices account purge -g "$RG" -n "$AI" -l "$AI_LOC"
```

### 3. Verify nothing is left

```bash
az resource list --tag project=tx-fiscal-radar -o table      # must print nothing
az group exists -n rg-tx-fiscal-radar-dev                    # false
az keyvault list-deleted -o table                            # no tx-fiscal-radar vault
az cognitiveservices account list-deleted -o table           # no tx-fiscal-radar account
```

### 4. The web app's sign-in registration (tenant-level, not in the resource group)

```bash
infra/app_auth.sh delete        # removes the Entra app registration txfr-fiscal-note-desk and APP_CLIENT_ID from .env
az ad app list --display-name txfr-fiscal-note-desk -o table   # must print nothing
```

Everything else from the Foundry phases (memory store, the second agent, the fine-tuned model and its training
files, the vector index, the Function App, its storage and Application Insights) lives in the resource group or
inside the Foundry account and goes with step 1. The fine-tuned model's Developer deployment deletes itself after
24 hours anyway.

### 4b. Power BI (not in Azure, so step 1 doesn't touch it)

Delete the workspace **tx-fiscal-radar** in Power BI (app.powerbi.com → Workspaces → ⋯ → Workspace settings → Delete
workspace). That removes the report, the semantic model and the work account's Viewer access.

### 5. Databricks Free Edition (no cost, just tidy)

```bash
databricks --profile free schemas delete workspace.txfr --force   # or DROP SCHEMA workspace.txfr CASCADE in a notebook
```

### 6. Local (optional)

```bash
rm -rf data/                    # ~124 MB of downloaded files + AI response cache (all re-creatable)
rm -rf ~/.azure-powerbi         # the separate Power BI login profile
```

## What stays

- **GitHub repo** (code, docs, screenshots): the portfolio piece. Visibility stays private until reviewed.
- **Rebuild:** [RUNBOOK.md](RUNBOOK.md) recreates everything on a new subscription in about an hour. The
  infrastructure is Bicep and the data comes back from the Legislature's FTP site.
- **Bookkeeping after teardown:** update `CLAUDE.md` *State*, `docs/STATUS.md` and the Trello card (#121) to say it's
  torn down, with the date.
