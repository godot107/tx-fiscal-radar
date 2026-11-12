# Practice questions

Practice questions on Azure data engineering topics ("Microsoft Azure Data Engineering, Intelligence and Analytics", "AI Model Build",
Power Platform, Databricks, Azure SQL), each answered from what this project actually does. Read the answer, then
say it out loud in your own words with the project's numbers. Every answer points at the file that proves it.

## Phase 1 - Infrastructure as code, identity, cost control

**1. How do you provision an Azure data platform repeatably?**
Bicep, one template (`infra/main.bicep`), parameters per environment (`infra/params/dev.bicepparam`), and a wrapper
(`infra/up.sh`) that runs `what-if` before deploying and writes the outputs to `.env`. Identity-specific values (my
object ID, my IP) come from the shell, never from git. Rerunning is safe: ARM deployments are declarative.

**2. How do services authenticate to each other without secrets?**
System-assigned managed identities plus RBAC: Data Factory → lake (Storage Blob Data Contributor), Data Factory → Key
Vault (Secrets User), Synapse → lake, the Foundry project → AI Search (Index Data Reader). Shared keys are disabled on
storage, SQL is Entra-only (`azureADOnlyAuthentication`), Foundry and Search have local auth disabled. Azure SQL users
for identities are `CREATE USER [adf-name] FROM EXTERNAL PROVIDER` (`sql/00_identities.sql`).

**3. What is Key Vault doing if nothing has a password?**
It holds the one optional external credential (a Socrata app token). Data Factory reads it with a Web activity using
its managed identity and `secureOutput: true`, so the value never appears in run history. Key Vault is in RBAC mode
(no access policies).

**4. How do you keep a lab from running up a bill?**
Free tiers where they exist (Azure SQL free offer with `freeLimitExhaustionBehavior: AutoPause`, AI Search free), a Spark
pool that auto-pauses after 5 minutes, serverless SQL instead of a dedicated pool, Log Analytics daily cap, RG budgets
($25/$50/$100, actual and forecast alerts). Actual spend is in `docs/COSTS.md`.

**5. Two deployment problems you hit and how you handled them.**
(a) The first Synapse workspace create failed with a generic `CreateWorkspaceError`; deleting the failed workspace and
redeploying worked. (b) Every Foundry connection PUT returned HTTP 500 (even a dummy one), so the connection became an
optional Bicep parameter and the agent got a client-side search tool, with a runbook step to switch back. The point:
make the failure explicit in code and docs instead of hand-fixing in the portal.

**6. Tagging and governance?**
Required tags (project, environment, owner, cost-center, managed-by) on the RG and every taggable resource from one
`tags` variable; `tools/check_tags.sh` lists anything missing. Exceptions it knows: system `master` databases, and
Synapse's managed RG, which has a deny assignment (nobody can write to it; Synapse copies the tags itself).

## Phase 2 - Ingestion with Data Factory

**7. What does "metadata-driven" mean in your pipelines?**
One control table (`ctl.ingest_source`) lists every source: kind (ftp, ftp_manifest, http), source path, sink path,
file pattern, schedule (once / daily / weekly) and watermark. `pl_ingest` looks up the due sources
(`ctl.usp_sources_due @mode`), a ForEach (2 at a time) calls one generic child pipeline per source, and a Switch routes
by kind. Adding a session is an INSERT, not a new pipeline.

**8. How do you load incrementally from a source that doesn't give you modified dates?**
The standard pattern is `modifiedDatetimeStart/End` on the copy source with a watermark. This FTP server doesn't report
last-modified times (ADF error `UserErrorLastModifiedNotSupported`), so I used the Legislature's change manifest
(`history_periodic.xml`): copy the XML into a SQL staging table (XML attributes `@id/@path/@timestamp` mapped with a
`collectionReference`), compute the changed 100-bill folders after the watermark in a stored procedure, re-copy only
those folders, and set the watermark to the newest manifest timestamp (the source's clock, not mine). A weekly full
copy reconciles anything a manifest window might miss.

Follow-up I learned the hard way: the manifest is a rolling *today* list. It had 3,157 entries in the evening and an
empty `<bills />` after midnight, which made the copy fail (ADF can't infer a schema from an empty XML). Fixes: check
the file size first and log "no change"; run the incremental at 11:30 PM, before the reset. Lesson: confirm a change
feed's window before you schedule against it.

**9. Why copy whole folders instead of only the changed files?**
Cost and politeness. A Copy activity is billed per DIU-minute with a minimum, so one copy per changed file would cost
hundreds of minimums a day during session. One folder holds up to 100 bills: a few dozen folder copies a day.

**10. How do you make sure a failed copy doesn't move the watermark?**
The child pipeline logs to `ctl.ingest_run` on both paths. `ctl.usp_log_ingest` advances the watermark only when status
is Succeeded, and never backwards. On failure it logs, then a Fail activity re-raises the error so the parent run fails
and the metric alert (`PipelineFailedRuns > 0`) emails within 5 minutes. (A try/catch without the Fail activity would
report success - a classic ADF trap.)

**11. How do you run ADF as code?**
The JSON lives in `adf/` in the git-integration layout (linkedService/dataset/pipeline/trigger). Pipelines are generated
by `adf/_generate.py` so repeated parts (logging, failure handling) can't drift; CI regenerates and fails on a diff.
`adf/deploy.sh` PUTs each object through the ARM REST API in dependency order; triggers deploy Stopped.

**12. How is the source treated responsibly?**
The Legislative Council asks data companies not to scrape capitol.texas.gov, so everything comes from the published
FTP, two connections per copy, incremental after the backfill.

## Phase 3 - Transformation (Synapse Spark, serverless SQL)

**13. Walk through the medallion layers.**
Bronze: raw files exactly as published (re-parse any time). Silver: Delta tables by business key (bill, bill_action,
bill_subject, fiscal_note, fiscal_note_agency, fiscal_note_amount, quarantine). Gold: flat Parquet for SQL and BI
(latest note per bill, version drift, agency spend).

**14. How is the Spark job incremental and idempotent?**
It reads only bronze files modified after the newest `source_modified` already in silver (`modifiedAfter` on the
binaryFile reader), then MERGEs parents by key and replaces children per key (delete matching keys, append). Rerunning
the same files gives the same silver. `full=true` re-parses everything.

**15. How do you deal with bad input?**
Every parse error goes to `silver/quarantine` with the path and reason; the batch never dies. The first run
quarantined 61 files, which surfaced two real format facts: a version code C (conference committee report) and 53
notes saying "the fiscal implications cannot be determined". Both became parser features with tests.

**16. How do you get your own Python code onto a Spark pool?**
The parsers are plain Python (stdlib only), zipped and uploaded to the lake, and added with `sc.addPyFile`. No pool
package install (slow, and it restarts the pool), and the same code runs in pytest, Synapse and Databricks.

**17. What is serverless SQL for here?**
Ad-hoc questions over the lake without loading anything: views over Delta (`FORMAT='DELTA'`) and Parquet, a bronze
inventory using `filepath()`, and a CETAS snapshot that writes a query result back to the lake as Parquet. It reads
the lake through a database-scoped credential using the workspace managed identity.

## Phase 4 - Serving model (Azure SQL)

**18. Describe the star schema.**
Dimensions: `dim_bill` (SCD type 2), `dim_agency` (Comptroller names preferred), `dim_session`, `dim_date` (with Texas
fiscal year Sep-Aug). Facts: bill actions, fiscal notes (every version, `is_latest`), five-year amounts, agency spend,
AI agency costs, AI mandates; bridges for bill↔agency and bill↔subject. Durable key `bill_id = '89R:HB2'`.

**19. Why SCD2 on bills, and how is it implemented?**
A bill's status changes daily during session; history answers "how long did it sit in committee". A SHA-256 hash of the
tracked attributes; when it changes, close the current row (`valid_to_utc`, `is_current = 0`) and insert a new one.
A filtered unique index guarantees one current row per bill. Caption/authors are type 1 (overwritten).

**20. How is the load made safe to rerun and safe to fail?**
Data Factory truncates and fills staging; one procedure (`dw.usp_load_all`) does everything in a transaction with
`XACT_ABORT`, logs Running/Succeeded/Failed with the error to `ctl.load_run`, and rethrows. It uses DELETE, not
TRUNCATE, because it runs as Data Factory's identity with EXECUTE only: ownership chaining covers DML, but TRUNCATE
needs ALTER on the table.

**21. A data quality catch from the load.**
The Comptroller dataset has one grand-total row with no agency ($189.9B for FY2024). Loading it would double the
state's spending. It is excluded in the transform and used as a check: the agency rows must add up to it.

**22. How do you prove the warehouse is right?**
Reconcile to an independent number: the Legislature's own report says 1,213 House and Senate bills passed both chambers
in 2025 (`reports/passed.htm`). `dw.v_data_quality` compares. It took two fixes to match:
(1) the "passed" rule had counted "Record vote" actions, which also happen on amendments; final passage is the action
titled exactly "Passed" or "Passed as amended" (the test that had locked in the wrong date was fixed too);
(2) eight bills had passed each chamber in *different versions* that were never reconciled (no concurrence, no
conference report). The Legislature counts bills enrolled in identical text, so "passed both chambers" became
"has an enrollment action". A bill-by-bill diff against the official list found both.

**22b. A schema change that broke the load, and the fix.**
Adding `enrolled_date` evolved the silver Delta table in place (`autoMerge`), and Delta appends new columns at the
end. Gold inherited that order and the Parquet → SQL copy matched columns by position, so `False` landed in a date
column. Fix: gold selects columns in the declared schema order, so the contract with SQL doesn't depend on how
silver evolved. Lesson: make the hand-off between layers an explicit contract (column list or explicit copy mapping).

## Phase 5 - Analytics and Power BI

**23. What does the dashboard let someone decide?**
See `docs/QUESTIONS.md`: where bills stall (staffing for hearings), which agencies carry the most new cost relative to
their size (prepare first), what takes effect when (implementation calendar), which estimates moved most (contingency),
and which 2027 bills name my agency (daily triage).

**24. How did you build Power BI without Power BI Desktop?**
REST API: a push dataset defined in code (tables, measures) and filled from the SQL views; the report written as PBIR
JSON (`powerbi/report/`) and uploaded with the Fabric item-definition API, which works on a PPU workspace. In
production I'd use an Import model on Azure SQL with scheduled refresh and RLS by agency.

**25. How would you do row-level security here?**
A mapping table user → agency_code, a role filter on `dim_agency[agency_code]` using `USERPRINCIPALNAME()`, propagating
through the bridge to bills and costs. Push datasets don't support RLS, one reason production would use Import.

## Phase 5b - AI on unstructured data

**26. Where is the AI, and why is it needed?**
A fiscal note's tables give one cost for the whole bill; which agency pays is only in the prose. gpt-4.1-mini with a
strict JSON schema (structured outputs) extracts per-agency amounts and the duties a bill creates.

**27. How do you know the model is right?**
Deterministic checks on every row, not a sample: the quote must appear verbatim in the note, the amount must be one of
the dollar figures written in that quote (catches the model adding numbers), and the agency must be on the note's
Source Agencies list. Bill level: do the rows add up to the note's own total (complete split) or not (lower bound).
Rows that fail go to a review queue on the dashboard. Bills naming a single agency are a known-answer subset.

**28. What went wrong in the first version, and what changed?**
v1 asked for signed amounts; the model sometimes wrote costs as positive numbers and once summed two printed figures.
v2 asks for the amount exactly as printed plus a `direction` enum, and code applies the sign; the amount-in-quote check
catches arithmetic. Prompt versions are part of the cache key, so results are reproducible and reruns are free.

**29. What does it cost?**
Tokens are logged per run; see `docs/RESULTS.md` (cents per session at gpt-4.1-mini list prices).

## Phase 6 - CI/CD and operations

**30. What runs in CI?**
pytest (parsers, AI checks, the Spark transform on local Spark with real 2025 files), Bicep build, ADF JSON regenerated
and diffed, JSON validity, shellcheck. Deploy is a separate workflow using GitHub OIDC (no client secret), off until the
federated credential exists.

**31. How would you promote dev → test → prod?**
Same Bicep with a parameter file per environment; ADF git integration with a collaboration branch and the publish
branch's ARM template deployed by a release pipeline with environment approvals; parameterize linked-service endpoints
per environment (they're `${VAR}` placeholders here); triggers stopped during deploy and restarted after.

**32. What do you monitor?**
`PipelineFailedRuns` metric alert → email; ADF and Synapse diagnostics in Log Analytics (resource-specific tables
`ADFPipelineRun`, `ADFActivityRun`); `ctl.ingest_run` / `ctl.load_run` for business-level freshness; the dashboard's
Trust page shows reconciliation and last-load time.

## Phase 6b - Foundry agent

**33. Describe the agent.**
`tx-session-analyst` in Foundry Agent Service on gpt-4.1-mini. Tools: AI Search over bill cards, agency cards and
fiscal-note sections (every document carries the source URL), and `get_metrics`, a function with a fixed menu of
read-only parameterized SQL queries. The model chooses a metric and arguments; it never writes SQL.

**34. How do you stop it from making up numbers?**
Instructions require every number to come from a tool result in the conversation; counts and totals come from
`get_metrics`, not from adding up snippets; answers cite bill ids and URLs; "not in the data" is an allowed answer.
`python -m txfr.agent eval` asks fixed questions whose correct answers are computed from SQL at eval time, including
one it must decline.

**35. How is it reachable "via an endpoint"?**
The agent lives in the Foundry project; any client with an Entra token and the Azure AI User role creates a thread and
a run against its id. Server-side tools run inside Foundry; the SQL function runs in the caller (here `txfr.agent`).
Next step: put `get_metrics` behind an Azure Function and register it as an OpenAPI tool so any client gets numbers
without local code.

## Databricks comparison (preferred qualification)

**36. Synapse Spark or Databricks?**
Same Spark code (the transform runs unchanged on both; insurance-docai's lakehouse ran on Databricks with Unity
Catalog). Databricks: better runtime, Unity Catalog governance, MLflow, Photon, jobs/serverless compute; Synapse:
in-workspace serverless SQL and pipelines, simpler if the client is all-Microsoft. On this trial Azure Databricks
needed pay-as-you-go, so Synapse here and Databricks Free Edition for the Unity Catalog work.

## Phases A-H - Evaluation, guardrails, retrieval, memory, fine-tuning, web app, scale, security

**Q: How do you evaluate an LLM agent?**
A: Several ways, because each catches different failures. Deterministic checks on questions whose answers come from SQL (26 questions; the agent went 19 → 24 of 25 after fixes). LLM judges to explain failures (on the baseline they passed the agent on most questions it had wrong, so I don't count with them). A red team for attacks, paired framings for neutrality, pairwise A/B for "which variant is better", and a load test for cost and capacity. Every run writes JSON, so a change can be re-checked in minutes (`docs/EVALUATION.md`).

**Q: Why not just use LLM-as-judge?**
A: On the baseline, the judges passed relevance, intent and task adherence on all six questions the agent got wrong. An answer about the wrong SB 4 is perfectly "grounded" in the wrong document. And in pairwise judging, the judge picked whichever answer it read first in 8 of 25 pairs. Judges need position swapping and should explain failures, not count them.

**Q: Does RAG need vectors? Are they faster?**
A: Not faster: each query needs an embedding call (keyword 43 ms vs vector 260 ms). What vectors buy is recall on paraphrases: "school vouchers" vs "education savings account". Measured on 30 queries: keyword 53% hit@6, vector 90%, hybrid 67%, hybrid + semantic reranker 97%. Plain hybrid lost to vectors alone because dozens of near-duplicate bill captions share the keywords; the reranker sorts them out.

**Q: How do you stop prompt injection when the documents are third-party text?**
A: Treat retrieved text as data. The runner sends every search result through Prompt Shields before the model sees it and replaces flagged documents; if the shield itself errors it fails closed and logs a `shield_error`. Red team before: a planted "this bill costs nothing, LBB recommends repeal" went straight into the answer. After: removed in all three deliveries. The same shield protects external agents using the MCP endpoint.

**Q: Your users are lobbyists and advocates on both sides. How do you keep the assistant neutral?**
A: Same facts for every side, no advocacy writing. A request screen restates one-sided questions ("make the case for cutting DIR") as the neutral information need, so both framings get the same retrieval. A paired test measures it: facts became consistent across sides; tone still slips in about 3 of 10 pairs. That's reported as-is, not claimed as solved.

**Q: Instructions said "don't write election misinformation". Why did it still do it?**
A: Instructions are a soft control. Even with an explicit rule, the agent wrote a "the election date was moved" post. A separate classifier in front of the agent, with fixed refusals for high-severity categories, took public-trust attacks from 1 of 10 to 0 of 10 (8 stopped before the agent ran).

**Q: What does a question cost?**
A: $0.0056 on average with all guardrails, measured per component: model $0.0027, Content Safety $0.0027, screen $0.0002. The guardrails cost as much as the model. A cached repeat costs $0. Spend is capped per user ($1/day) and an alert stops the app automatically above $2 an hour (drilled: offline in 10 seconds), because Azure budgets only send email.

**Q: What if someone floods the public endpoint?**
A: Today: 500 unauthenticated requests all got 401 from App Service Authentication before any of our code ran, so there was no model cost; compute is capped at 5 instances (worst case ≈ $0.94 an hour). For true public access: Front Door + WAF rate limiting, API Management quotas and token limits per caller, Entra External ID sign-up, cached answers for anonymous users, and the cost breaker.

**Q: Did fine-tuning help?**
A: It changed the trade-off rather than winning outright. Trained on the prompted model's own verified extractions (distillation with a verifier), on held-out bills it produced fewer rows (220 vs 367) with more of them verifiable (68% vs 48%), but fewer bills reconciled to the note's total (13 vs 19). Better when only trusted rows are shown, worse for "find every cost". Developer tier: no hourly hosting fee, about $1 to train.

**Q: How does memory work, and what about privacy?**
A: Foundry's memory store, scoped per user by Entra object id, opt-in on the page, told to keep work context only (agency, role, topics), 30-day TTL. Test: user A's "what is my agency facing?" is answered for their agency in a new session; user B's identical question isn't. Opt-in also lets non-personalized answers be cached.

**Q: How do you know the A/B result means anything?**
A: Mostly it doesn't, at this size: 6 wins vs 5 with 14 ties gives a 95% interval of 28%-79% for the win rate. Detecting a 60/40 preference takes roughly 100-200 votes. The online side-by-side in the app is built so the page never learns which variant is which (server logs the mapping before the vote).

**Q: What did the security scans find?**
A: pip-audit: one advisory in pyspark (local tests only; pinned to the fixed version). detect-secrets and a git-history grep: no secrets. Checkov: 48 failed checks, triaged: 4 fixed (SQL auditing to Log Analytics, HTTP/2, FTP and basic-auth publishing off), about 25 production-only (private endpoints, zone redundancy, CMK), the rest not applicable or parser misses. GitHub secret scanning isn't available on this private repo, so detect-secrets runs in CI.

**Q: What would you not do again?**
A: Trust "it deployed" as "it works". A deploy that missed a new module returned ImportError in production until a manual test; every deploy now ends with an authenticated end-to-end smoke test. And batch API calls without reading the limits: a 6-document Prompt Shields call (limit 5) failed closed and silently emptied search results until the eval caught it.

**Q: You raised the model quota 6× and throughput barely moved. Why?**
A: Azure OpenAI's rate limiter counts each request's *maximum* tokens (prompt + allowed output) when it arrives, not
what it ends up using. The Agent Service sets no output limit by default, so every model call inside a run reserved a
large output allowance. Capping `max_completion_tokens` at 2,000 per run (answers average ~190) took 20 users from
12.6 to 45.9 answers/min at the same 300K quota, with 0% throttling, and bounds what one run can spend.
