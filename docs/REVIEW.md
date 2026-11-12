# Review against the textbook library

A design review of tx-fiscal-radar against the books in the local textbook knowledge base (textbook-kb), done on
2026-11-10 after the build. Each finding cites the book and page, states what this project does, and what changed.
"Fixed" items are in the code; "Documented" items are production recommendations that don't belong in a dev lab.

Books used:
- Andersson, *Learning Microsoft Azure* (O'Reilly, 2023): the Azure book in the library
- Lee et al., *Delta Lake: The Definitive Guide* (O'Reilly)
- Chambers & Zaharia, *Spark: The Definitive Guide* (O'Reilly)
- Damji et al., *Learning Spark*, 2nd ed. (O'Reilly)
- Storment & Fuller, *Cloud FinOps*, 2nd ed. (O'Reilly, 2023)
- McMahon, *Machine Learning Engineering with Python* (Packt, 2023)

(The library has no Kimball-style data-warehouse book; the SCD2 design follows standard practice and is tested by the
reconciliation check rather than by citation.)

## Findings

| # | Topic | What the book says | This project | Status |
| :--- | :--- | :--- | :--- | :--- |
| 1 | Delta MERGE with duplicate keys | MERGE semantics match each target row against the source; when several source rows match one target row the merge cannot decide and fails (Lee, ch. 4, pp. 98-99) | Silver MERGE by business key. Duplicates were unlikely (one bronze file per bill/version) but not impossible | **Fixed**: one row per key before MERGE, newest bronze file wins (`write_silver`) |
| 2 | Small files and VACUUM | Small transactions (daily upserts) leave many small files; compact them with OPTIMIZE, and VACUUM even append-only tables because failed writes leave orphan files; keep a minimum retention (7 days) (Lee, ch. 5 and 10, pp. 146, 337, 344) | Daily incremental MERGEs into silver | **Fixed**: the weekly run passes `maintenance=true`: OPTIMIZE + VACUUM 168 h (`maintain_silver`) |
| 3 | Python in Spark | The cost of Python UDFs is serializing rows to Python; vectorized (pandas/Arrow) UDFs move batches instead (Chambers & Zaharia, pp. 120, 326) | Parsing is necessarily Python (HTML/XML) | **Fixed during the build** for portability: `rdd.mapPartitions` → `mapInPandas` (Arrow batches), which also runs on Spark Connect (Databricks serverless, verified) |
| 4 | Idempotent pipelines | "Mandate idempotency in your DAGs": same inputs, same result, however many times it runs (McMahon, p. 431) | Watermark only advances on success; silver MERGE; staging truncate+insert; star load in one transaction | **Met**. Verified by rerunning the load (same row counts) and by a second daily run that copied 0 changed folders (`docs/RESULTS.md`) |
| 5 | Late data and watermarks | Watermarks bound how late data may arrive; anything later needs another path (Damji et al., pp. 266-268) | The manifest watermark uses the source clock; a manifest window could in principle be missed | **Met**: the weekly full copy is that "other path" |
| 6 | Managed identities, Key Vault | Use managed identities to authenticate to Storage, Key Vault, SQL instead of credentials in code; Key Vault for any secrets that remain (Andersson, ch. 9, pp. 288-290) | Every service-to-service call is managed identity + RBAC; shared keys, SQL passwords and Foundry/Search keys disabled; Key Vault holds one optional token | **Met** |
| 7 | Storage access | Prefer user-delegation SAS (Entra-backed), set SAS expiry policies, HTTPS only (Andersson, pp. 193-194) | No SAS at all: Entra RBAC only, HTTPS only, TLS 1.2 | **Met** (stricter) |
| 8 | Resource locks | `CannotDelete` locks protect production resources from accidental deletion (Andersson, p. 375) | Dev lab is meant to be torn down (`infra/down.sh`) | **Documented**: add a `CannotDelete` lock on the prod resource group and the lake account in the prod parameter set |
| 9 | Key Vault recovery | (Andersson, p. 290) soft delete protects secrets | Soft delete 7 days, purge protection off so the lab can be purged | **Documented**: prod enables purge protection (90-day retention) |
| 10 | Reliability | Distributed systems should retry transient errors and plan for regional failure (region pairs, availability zones) (Andersson, pp. 92-93, 404) | Copy/SP/Lookup activities retry 2-3 times; LRS storage, single region | **Documented**: prod uses ZRS (or GZRS) for the lake, SQL zone redundancy, and the paired region for disaster recovery; the bronze layer is re-creatable from the public FTP, so RPO for bronze is effectively zero |
| 11 | Governance and lineage | Microsoft Purview catalogs data and tracks lineage and lifecycle (Andersson, pp. 244-245) | Lineage is implicit (bronze path → silver `source_path` → SQL `source_path` → agent citations) | **Documented**: register the lake, Synapse and Azure SQL in Purview and connect Data Factory for automatic lineage (not on the trial) |
| 12 | Monitoring | Monitor health and performance per resource group with alerts (Andersson, p. 374) | Metric alert on failed pipeline runs → email; diagnostics to Log Analytics; business-level freshness in `ctl.*` and the Trust page | **Met** |
| 13 | Cost allocation | Tag what you can, and use the hierarchy (accounts/resource groups) to allocate what can't be tagged (Storment & Fuller, pp. 202, 212-213) | Required tags on every taggable resource; budgets scoped to the resource group, which also catches untaggable costs | **Met**. `tools/check_tags.sh` lists the known untaggables (system `master` DBs, Synapse's deny-assigned managed RG) |
| 14 | Infrastructure as code | Bicep/ARM for Azure-native IaC; policy as code alongside (Andersson, pp. 356-360) | Bicep with what-if; no Azure Policy assignments | **Documented**: deploy a tag-enforcement policy (insurance-docai has `governance.bicep` ready) once the subscription is out of trial |

## What the review changed

- `src/txfr/spark_jobs.py`: dedupe-before-MERGE (#1), `maintain_silver` OPTIMIZE + VACUUM (#2).
- `synapse/notebooks/nb_transform.ipynb` and `adf/_generate.py`: `maintenance` parameter, true on the weekly trigger.
- Production recommendations (#8-#11, #14) are collected in `docs/ARCHITECTURE.md` → *Production reference*.
