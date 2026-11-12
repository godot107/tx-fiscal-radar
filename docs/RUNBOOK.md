# Runbook

Everything runs from the project folder with the venv: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -e .`
and an `az login` as the operator (browser flow: `az login --tenant <tenant-id>`; device-code sign-in is blocked by the
tenant's security defaults, error AADSTS530035).

## Build from nothing

| Step | Command | What it does |
| :--- | :--- | :--- |
| 1 | `infra/up.sh --what-if` then `infra/up.sh` | Resource group + every resource in `infra/main.bicep`; writes `.env` from the outputs |
| 2 | `tools/check_tags.sh --fix-rg` | Tags Synapse's managed resource group; lists any resource missing a required tag |
| 3 | `.venv/bin/python tools/run_sql.py sql/00_identities.sql sql/01_control.sql sql/02_serving.sql sql/03_analytics.sql` | Data Factory's database user, control tables + sources, staging + star schema, analytics views |
| 4 | `az keyvault secret set --vault-name <kv> -n socrata-app-token --value anonymous` | Placeholder (or a real Socrata app token) |
| 5 | `synapse/deploy.sh` | Uploads `txfr.zip`, imports `nb_transform`, grants Data Factory the Synapse roles to run it |
| 6 | `python3 adf/_generate.py && adf/deploy.sh` | Pipelines/triggers JSON → factory (triggers arrive **Stopped**) |
| 7 | `tools/adf_run.sh pl_daily '{"mode":"daily","full_transform":true}'` | First end-to-end run: backfill, transform, load |
| 8 | `.venv/bin/python tools/run_serverless.py synapse/sql/serverless.sql` | Serverless lake database, views, CETAS |
| 9 | `.venv/bin/python -m txfr.ai_extract --write` then `tools/adf_run.sh pl_load_sql` | AI extraction → gold → SQL |
| 10 | `.venv/bin/python -m txfr.search_index` and `.venv/bin/python -m txfr.agent create` | Search index + Foundry agent |
| 11 | `.venv/bin/python -m txfr.powerbi publish` and `... report` | Power BI dataset + report (needs the Power BI work-account login) |

## Daily operations

| Task | How |
| :--- | :--- |
| Turn the schedule on / off | `az datafactory trigger start -g rg-tx-fiscal-radar-dev --factory-name <adf> -n tr_daily` (and `tr_weekly`); `... trigger stop ...` |
| Run now | `tools/adf_run.sh pl_daily` (daily mode) or `tools/adf_run.sh pl_daily '{"mode":"weekly"}'` |
| Watch a run | `tools/adf_wait.sh <runId>` prints each activity, files read/written, errors |
| What was ingested | `tools/run_sql.py --query "SELECT TOP 20 * FROM ctl.ingest_run ORDER BY run_id DESC"` |
| What was loaded | `tools/run_sql.py --query "SELECT TOP 10 * FROM ctl.load_run ORDER BY load_id DESC"` |
| Data checks | `tools/run_sql.py --query "SELECT * FROM dw.v_data_quality"`: `passed_89r_hb_sb` must equal `passed_89r_hb_sb_official` (1213) |
| Files the parser rejected | serverless: `SELECT * FROM silver.quarantine` |
| Refresh Power BI | `.venv/bin/python -m txfr.powerbi publish` after the pipeline finishes |
| Re-run AI extraction | `.venv/bin/python -m txfr.ai_extract --write` (cached by prompt hash in `data/ai_cache/`: unchanged notes cost nothing) |

## Recovery

| Situation | Action |
| :--- | :--- |
| Re-copy one source from scratch | `tools/run_sql.py --query "EXEC ctl.usp_reset_watermark 'legis-89R-fiscalnotes-house'"`, then run `pl_ingest` |
| Rebuild silver/gold from bronze | `tools/adf_run.sh pl_daily '{"mode":"daily","full_transform":true}'` (parses every bronze file again; MERGE keeps it idempotent) |
| Manifest gap (a daily 11:30 PM run was missed: the manifest has already reset) | Run weekly mode once: `tools/adf_run.sh pl_ingest '{"mode":"weekly"}'` copies the live session's folders in full |
| SQL load failed half way | Nothing to undo: `dw.usp_load_all` runs in one transaction (`XACT_ABORT`) and logs `Failed` + the error in `ctl.load_run`; fix and rerun `pl_load_sql` |
| Free SQL allowance used up | The database auto-pauses instead of billing (`freeLimitExhaustionBehavior: AutoPause`); it resumes next month |
| Synapse workspace create failed (`CreateWorkspaceError`) | Seen once on first deploy; delete the failed workspace (`az synapse workspace delete`) and rerun `infra/up.sh` |
| Foundry connection PUT returns HTTP 500 | Seen 2026-11-10 for every connection type on the account. The agent uses the client-side `search_session` tool instead. To retry: `infra/up.sh` with `deploySearchConnection=true` in `infra/params/dev.bicepparam`, then `python -m txfr.agent create` (switches to the server-side search tool automatically) |
| Power BI `No Power BI login` | `AZURE_CONFIG_DIR=~/.azure-powerbi az login --allow-no-subscriptions --tenant <tenant-id>` (work account) |

## Known issues

| Issue | Why | Workaround |
| :--- | :--- | :--- |
| FTP has no file modification times | Server answers no MDTM/MLSD; ADF raises `UserErrorLastModifiedNotSupported` | Manifest-driven incremental + weekly full copy (see ARCHITECTURE.md) |
| Enrolled fiscal notes often list no Source Agencies | LBB leaves the box empty on the final version | Agencies come from the latest version that lists any (`build_gold`) |
| Fiscal-note HTML has unbalanced `</div>` | Generated markup | The parser reads text by element id and the date from the whole document |
| The change manifest is "changes today" only | `history_periodic.xml` held 3,157 entries at 05:30 UTC and an empty `<bills />` at 07:30 UTC (after midnight Central); ADF can't infer a schema from an empty XML (`UserErrorTypeInSchemaTableNotSupported`) | Size check first (empty → log "no change"); daily trigger at 11:30 PM Central; Sunday full copy covers a missed day |
| New silver column lands at the end | Delta schema evolution appends columns; a positional copy then misaligns | Gold selects columns in declared schema order (`build_gold`) |
| ForEach `batchCount` queues are fixed | The backfill's ForEach put the two largest folders in the same queue; the other queue finished early and sat idle | Order sources largest-first, or raise `batchCount` for backfills |
| Manifest timestamps are Central time without zone | Source format `11/9/2026 9:00 AM` | Stored as source-clock watermark; never compared with UTC |
| AI Search free tier: 50 MB, 3 indexes | Free SKU | Index holds enacted-bill notes + all cards; full-text of every version would need Basic |

## Teardown

Full checklist (pause option, purges, Power BI, Databricks, verification): [TEARDOWN.md](TEARDOWN.md).

```bash
infra/down.sh                                  # asks; deletes the resource group (Synapse's managed RG goes with it)
az keyvault purge -n <kv> --location centralus # Key Vault is soft-deleted for 7 days
az cognitiveservices account purge -g rg-tx-fiscal-radar-dev -n <ai-account> -l eastus2
az resource list --tag project=tx-fiscal-radar -o table   # must print nothing
```

## Nightly maintenance (Function App timer, 06:30 UTC)

Runs after the 11:30 PM CT pipeline and logs one `OPS_EVENT` line to App Insights:

1. **Privacy retention**: deletes web-app questions kept past their 30-day date (`src/txfr/retention.py`; by hand:
   `tools/retention.py [--delete]`).
2. **Power BI refresh** (off until enabled): reloads the push dataset from the warehouse with the app's managed identity.

```kusto
AppTraces | where Message startswith "OPS_EVENT" | project TimeGenerated, Message | order by TimeGenerated desc
```

### Enabling the Power BI refresh (one time, tenant admin)

The app identity is a service principal, and Power BI only accepts service principals when the tenant allows it:

1. Create a security group for apps that may call Power BI and add the app identity:
   ```bash
   az ad group create --display-name txfr-powerbi-apps --mail-nickname txfr-powerbi-apps
   az ad group member add --group txfr-powerbi-apps --member-id "$(grep ^APP_PRINCIPAL_ID= .env | cut -d= -f2)"
   ```
2. Power BI → Settings → Admin portal → Tenant settings → Developer settings → **Service principals can call Fabric
   public APIs** → Enabled for **specific security groups** → `txfr-powerbi-apps`. (Takes up to 15 minutes.)
3. Give the identity Member access to the workspace (as the workspace admin):
   ```bash
   T=$(AZURE_CONFIG_DIR=~/.azure-powerbi az account get-access-token --resource https://analysis.windows.net/powerbi/api --query accessToken -o tsv)
   curl -s -X POST -H "Authorization: Bearer $T" -H "Content-Type: application/json" \
     "https://api.powerbi.com/v1.0/myorg/groups/<workspace-id>/users" \
     -d "{\"identifier\":\"$(grep ^APP_PRINCIPAL_ID= .env | cut -d= -f2)\",\"principalType\":\"App\",\"groupUserAccessRight\":\"Member\"}"
   ```
4. Set `param pbiRefresh = true` in `infra/params/dev.bicepparam` and run `infra/up.sh`.

Scoping the tenant setting to one group keeps every other service principal in the tenant out of Power BI.
