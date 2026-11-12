# Dashboard (Power BI)

Report **Texas Fiscal Radar**, six pages, built as code by `src/txfr/powerbi.py` (definition in `powerbi/report/`,
PBIR format). Data: push dataset of the same name, filled from the Azure SQL views in `sql/03_analytics.sql`.

| Page | For | Visuals | Source view |
| :--- | :--- | :--- | :--- |
| 1. Session pulse | Leadership, weekly | Cards: 2027 bills filed, 2025 filed / enacted / share enacted / vetoed. Line: cumulative filings by days since prefiling, 2027 vs 2025. Bars: committees where 2025 bills stopped | `v_session_funnel`, `v_filing_pace`, `v_committee_bottleneck` |
| 2. Watchlist (2027) | Government relations, daily | Agency-subject dropdown; table of 2027 bills with hearing / fiscal-note flags and last action | `v_watchlist` |
| 3. Fiscal exposure | Budget office | Cards: enacted cost 2026-27, bills with cost, new FTEs, costs that can't be determined. Stacked bars: direct vs AI-attributed cost by agency. Tables: largest enacted bills; costs that grow after the biennium | `v_bill_cost`, `v_agency_exposure` |
| 4. Readiness | Agency leadership | Agencies ranked by new cost load (% of FY2024 spend) with cost, FTEs, shared bills, AI rows to review. Implementation calendar filtered by agency | `v_agency_exposure`, `v_implementation_calendar` |
| 5. Estimate drift | Budget analysts | Scatter first vs latest estimate; table of biggest changes | `v_estimate_drift` |
| 6. Trust & review | Data team, reviewers | Cards: passed bills in the warehouse vs the Legislature's official count, gap, last load, failed ingests (7 days). Table: AI extractions waiting for review, with the quoted evidence | `v_data_quality`, `fact_agency_cost_ai` |

## Publishing

```bash
AZURE_CONFIG_DIR=~/.azure-powerbi az login --allow-no-subscriptions --tenant <tenant-id>   # work account, once
.venv/bin/python -m txfr.powerbi publish     # dataset + rows (re-run after each pipeline run = the refresh)
.venv/bin/python -m txfr.powerbi report      # report definition → Fabric API
```

Until the report is published, `docs/img/*.png` (from `tools/make_figures.py`, same views) and the executed notebook
`notebooks/walkthrough.ipynb` show the same numbers.

## Running it as an operational dashboard

| Need | Development (as built) | Production |
| :--- | :--- | :--- |
| Freshness | `publish` after the 4 AM pipeline | Import model on Azure SQL, scheduled refresh after `pl_daily` (or a Data Factory Web activity calling the refresh API) |
| Agency-specific view | Dropdown slicers | Row-level security: role filter on `dim_agency[agency_code]` from a user → agency table, `USERPRINCIPALNAME()` |
| Alerts | n/a (push dataset) | Data alerts on page-2 cards (new hearings for watched bills) and a weekly subscription email for leadership |
| Trust | Trust page | Same, plus an endorsement (certified) on the semantic model |
