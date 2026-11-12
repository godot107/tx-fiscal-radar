# Costs

Free-trial subscription, spending limit on. Budgets on the resource group: $25 / $50 / $100 per month (actual and
forecast alerts to azure@willieman.com). Billed figures from Cost Management lag usage by several hours and the
API was rate-limiting (HTTP 429) when this was written: rerun `python tools/cost_report.py` for the billed numbers.

## Build (2026-11-09/10), estimated from measured usage at list price

| Service | Measured usage | Unit price (list) | Estimate |
| :--- | :--- | :--- | ---: |
| Data Factory: data movement | 16.4 DIU-hours (`tools/adf_usage.py`) | $0.25 / DIU-hour | $4.10 |
| Data Factory: orchestration | 326 activity runs | $1 / 1,000 runs | $0.33 |
| Data Factory: pipeline activities | 2.8 hours | $0.005 / hour | $0.01 |
| Synapse Spark (`sparksmall`, 3 × 4 vCores) | 6 notebook runs, ~70 min of pool time + 5-min auto-pause tails | ~$0.143 / vCore-hour | ≈ $2.50 |
| Synapse serverless SQL | < 2 GB processed | $5 / TB | < $0.01 |
| Foundry gpt-4.1-mini | ≈ 240k prompt + 100k completion tokens (extraction, dry runs, agent) | $0.40 / $1.60 per 1M | ≈ $0.26 |
| Azure SQL (free offer) | within 100k vCore-seconds / month | $0 (auto-pauses when used up) | $0 |
| AI Search (free tier) | 11,224 documents | $0 | $0 |
| Storage, Key Vault, Log Analytics | 124 MB raw + Delta/Parquet; a few hundred secret reads; < 0.1 GB logs | | < $0.20 |
| Databricks Free Edition (comparison run) | serverless, 74 s | free | $0 |
| **Total** | | | **≈ $7.50** |

## Running it every day (if the triggers are started)

| Item | Per day | Per month |
| :--- | ---: | ---: |
| 11:30 PM incremental: manifest + changed folders (a few dozen small copies during session) | ≈ $0.10 | |
| Spark incremental transform (~6 min + 5-min auto-pause tail) | ≈ $0.30 | |
| SQL load (11 staging copies) | ≈ $0.20 | |
| Sunday full reconcile of the live session (~1,200 files now, ~15k by May) | | ≈ $1-4 |
| **Total** | **≈ $0.60** | **≈ $20** |

The triggers `tr_daily` and `tr_weekly` deploy **Stopped** (ADF refuses to update a started trigger: stop both before rerunning `adf/deploy.sh`). They were started on 2026-11-10; stop them to end the daily cost (commands in [STATUS.md](STATUS.md)).

## What keeps it cheap

- No always-on compute: Spark pool auto-pauses after 5 minutes; serverless SQL instead of a dedicated pool; SQL free
  offer that pauses instead of billing.
- Copies are polite and coarse: 2 FTP connections, 2 DIUs, one copy per changed 100-bill folder instead of per file.
- AI calls are cached by prompt hash; re-checking 1,012 extracted rows after a rule change costs nothing.
- Everything is in one tagged resource group: `infra/down.sh` removes it in one step.

## Unit economics: what one question costs (Fiscal Note Desk)

Every request is metered by component (`src/txfr/finops.py`) at list price from the Azure Retail Prices API, checked
2026-11-10: gpt-4.1-mini $0.40 / $0.10 cached / $1.60 per 1M input / cached input / output tokens; text-embedding-3-small
~$0.02 per 1M; Content Safety $0.375 per 1,000 text records (a record = up to 1,000 characters); Functions Flex
$0.000026 per GB-second after 100,000 free GB-s a month. The app returns the cost with each answer and logs one
`COST_EVENT` per request.

Measured on the final 26-question eval (all guardrails on, memory off):

| Component | Per answer | What drives it |
| :--- | ---: | :--- |
| Agent model (gpt-4.1-mini) | $0.0027 | ~6,050 prompt tokens (instructions + tool schemas + tool results, re-sent on every model call within a run) + ~190 output |
| Content Safety (document shield + groundedness) | **$0.0027** | ~7 text records: the groundedness check reads every tool result as its sources |
| Request screen | $0.0002 | ~410 prompt tokens (below the 1,024-token minimum for prompt caching, so never cached) |
| Embeddings (hybrid search) | < $0.0001 | One short query embedding |
| Functions compute | < $0.0001 | Within the monthly free grant at this volume |
| **Total** | **$0.0056** | Numbers $0.0038 · lookups $0.0089 (search + shield) · declines $0.0051 |

What this shows:

- **The guardrails cost as much as the model.** That's a design input, not a reason to drop them: compacting the
  groundedness sources (no JSON whitespace or nulls, search results reduced to title + content) is in, and the check now
  runs only on answers that state figures. Measured on 26 questions: 5 skipped, Content Safety $0.0027 → $0.0024 per
  answer (-11%), total $0.0056 → $0.0055. Most answers state figures, so the next lever is trimming what the check reads.
- **Prompt caching isn't visible here.** The Agent Service's run usage doesn't report cached tokens, so the agent's
  model cost is an upper bound; most of its ~6,000 prompt tokens are the same instructions and tool schemas every
  time, which is what the cache discounts.
- **A cached answer costs $0.** Identical questions (normalized text) within a day are served from the answer cache in
  ~0.1 s; the cache key includes the last warehouse load time, so tonight's load invalidates it. Personalized (memory)
  answers are never cached.

### Showback queries

```kusto
// Cost per day, per user, and cache savings
AppTraces | where Message startswith "COST_EVENT" | extend e = parse_json(substring(Message, 11))
| summarize questions = count(), usd = round(sum(todouble(e.usd_total)), 4),
            cache_hits = countif(tostring(e.status) == "cache_hit") by day = bin(TimeGenerated, 1d), user = tostring(e.user)

// Where the money goes, by component
AppTraces | where Message startswith "COST_EVENT" | extend e = parse_json(substring(Message, 11))
| summarize agent = sum(todouble(e.usd_agent_model)), safety = sum(todouble(e.usd_content_safety)),
            screen = sum(todouble(e.usd_request_screen)), compute = sum(todouble(e.usd_functions))

// Cost per session (the page sends a session id)
AppTraces | where Message startswith "COST_EVENT" | extend e = parse_json(substring(Message, 11))
| summarize questions = count(), usd = sum(todouble(e.usd_total)) by session = tostring(e.session) | top 20 by usd
```

### Spend controls

| Control | Limit | Acts by |
| :--- | :--- | :--- |
| Answer cache | Repeats within a day cost $0, and are served even when a user is over the cap | Blob cache keyed by question + last load |
| Per-user daily cap | $1.00 per user per day (all instances, Table Storage) | HTTP 429 with a message; `GUARD_EVENT cost_cap` |
| Cost circuit breaker | > $2 of metered spend in an hour | Scheduled query → action group → Logic App stops the Function App (drilled: offline 10 s after trigger) |
| Instance cap | 5 Flex instances | Bounds compute and concurrency |
| Model quota | 50K tokens/min on the deployment | Hard ceiling on model spend; excess gets 429 |
| Output cap per agent run | 2,000 tokens (`TXFR_MAX_COMPLETION`) | Bounds one run's spend; also stops output reservations from exhausting the rate limit (load test: 3.6× throughput) |
| Budgets | $25 / $50 / $100 per month | Email only (Azure budgets never stop anything) |

### Phases A-H build cost (2026-11-10, estimated at list price)

| Item | Usage | Estimate |
| :--- | :--- | ---: |
| Agent runs (evals, red teams, neutrality, memory, smoke tests) | ~420 agent answers | ≈ $1.80 |
| Foundry LLM judges (2 scored runs × 5 evaluators) + pairwise + neutrality judge | | ≈ $0.40 |
| Embedding the index (11,224 docs, 256-dim) | 1.55M tokens | $0.03 |
| Fine-tuning (Developer tier) | 353k training tokens at $2.50 / 1M + 59 held-out calls | ≈ $0.95 |
| Content Safety (shields, groundedness) | | ≈ $1.20 |
| Load test + flood | see RESULTS.md §8 | ≈ $1.50 |
| Functions, storage, Logic App, App Insights | free grants / pennies | < $0.20 |
| **Total for phases A-H** | | **≈ $6** of the $20 cap (first estimate; metered total for A-I is ≈ $4, see below) |

### Disaster recovery standby (Phase I)

| Item | Standing cost |
| :--- | ---: |
| DR Foundry account, gpt-4.1-mini + embedding deployments, agent (West US 3) | $0 idle (pay per token when used) |
| DR Function App (Flex, scale to zero), storage, App Insights | ≈ $0 idle |
| Lake Standard_LRS → Standard_GRS | cents per month at this volume |
| Drills (live failover, restore to a temporary Basic DB, index rebuild) | < $1 |

### What this session cost (2026-11-10)

| | |
| :--- | ---: |
| Billed by Cost Management (pipeline build: Data Factory $4.66, Synapse $2.60, Foundry $0.28) | $7.56 |
| Metered, not yet billed: web app traffic $1.68 (566 requests), CLI evals ≈ $0.70, red team/neutrality/memory ≈ $0.35, judges ≈ $0.20, fine-tuning ≈ $0.95, embeddings $0.03, drills and extra loads ≈ $0.15 | ≈ $4 |
| **Session total** | **≈ $11.50-12** of the $200 trial credit; phases A-I ≈ $4 of their $20 cap |
