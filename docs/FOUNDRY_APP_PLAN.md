# Plan: Fiscal Note Desk (Foundry features + web app + scalability test)

**Status:** approved 2026-11-10 ($20 cap); Phase A in progress. Demo/quick start: [QUICKSTART.md](QUICKSTART.md). **Budget:** $20 of trial credit, all phases together
(credit ends early December 2026). **Teardown:** everything below is added to [TEARDOWN.md](TEARDOWN.md) as it's built.

## Goal

Turn the existing `tx-session-analyst` agent into a small web app that an agency budget analyst could use. Along the way,
use the Foundry features the project hasn't used yet (Knowledge, Tools, Guardrails, Evaluations, Memory, Data,
Fine-tune), and measure how far it scales on trial-sized quota instead of guessing.

## Phases

Each phase ends with a cost check (`tools/cost_report.py` or the measured-usage estimate) and a line in RESULTS.md.
**Stop rule:** if the running total for this plan passes $15, stop and ask before continuing.

| # | Phase | What gets built | Foundry features | Est. cost |
| :- | :--- | :--- | :--- | ---: |
| A | **Evaluation baseline** | 25-question eval set (numbers checked against SQL, plus "don't know" and out-of-scope questions). Run Foundry's built-in evaluators: groundedness, relevance, tool-call accuracy, intent resolution. This replaces the 5-question home-made grader. | Evaluations, Data | $1 |
| B | **Guardrails** | Custom content filter on the deployment; Prompt Shields for jailbreak/injection, including injection hidden in fiscal-note text; groundedness detection. Re-run A to show the before/after. | Guardrails | $1 |
| B2 | **Public-trust guardrails** (added 2026-11-10) | Audience: public officials, lobbyists, advocacy groups, media. Neutral fact service: the same facts for every side of a spending question, no advocacy writing or fund/cut recommendations; decline impersonation of official documents, election misinformation, targeting individuals, security-sensitive detail, partisan characterization; race/ethnicity questions → "not in the data", no stereotypes; hate filter Low. Measured with extra red-team categories and a **paired neutrality test** (the "fund more" and "cut" versions of the same question). | Guardrails, Evaluations | $1 |
| C | **Knowledge + tools** | Retry the Foundry → AI Search connection (it returned HTTP 500 for every type on 2026-11-10). If it works, the agent searches server-side; if not, the current client-side search stays and the attempt is documented. Add code interpreter so the agent can return a chart. **Retrieval comparison:** add 256-dimension `text-embedding-3-small` vectors (integrated vectorization, fits the free tier's 50 MB) and score keyword vs vector vs hybrid + semantic ranker: how often the right note is in the top 6, and answer groundedness. | Knowledge, Tools | $1-2 |
| D | **Memory** | Deploy `text-embedding-3-small`; memory store scoped per signed-in user ("I work in DIR's budget office" → later answers start there). | Memory, Models | $1 |
| E | **Fine-tune experiment** | Train gpt-4.1-mini on the verified extraction rows (train/held-out split by bill), **Developer tier only** (no hourly hosting fee, deployment auto-deletes after 24 h). Compare with the prompted model on held-out notes using the same four checks. Called what it is: distillation of the model's own verified output. | Fine-tune, Data | $3-5 |
| F | **Web app** | Static Web Apps (Free) page: chat with citations + chart panel. Azure Functions (Flex Consumption) backend calls the agent with a managed identity. Entra sign-in in front (no anonymous access: an open chat on a trial subscription invites abuse). | - | $1-2 |
| F2 | **MCP server** (added 2026-11-10) | Remote MCP endpoint on the same Function App: read-only `get_metrics`, `search_fiscal_notes`, `bill_detail` for external agents (Claude, Copilot, ChatGPT) used by journalists, staffers and advocacy groups. Their model reasons; we serve auditable facts with sources. Same Entra sign-in; rate-limited. | Tools | $0 |
| G | **Scalability test** | Load test from this machine against the Functions endpoint: ramp 1 → 5 → 10 → 20 concurrent users, fixed question mix, capped at ~1,500 questions total. Record p50/p95 latency, throttling (HTTP 429) rate and cost per answer; find where it breaks and why. | - | $5-6 |
| H | **Docs** | ARCHITECTURE (app + production design), RESULTS, DRILLS (new questions), STATUS screenshots, TEARDOWN additions. | - | $0 |
| | | | | **≈ $14-18** |

Order: A first (baseline numbers), then B-D, which each re-run A. E is independent. F before G.

## Scalability: what we expect to find, then measure

The app has six moving parts. Each scales differently, and on this subscription the **model quota** is the ceiling.

| Part | As deployed | Limit that bites first | Production move |
| :--- | :--- | :--- | :--- |
| gpt-4.1-mini | GlobalStandard, **50K tokens/min** | One agent answer is ~6-10K tokens (instructions, tool calls, tool results), so **≈ 5-8 answers a minute**. Past that, HTTP 429. Trial subscriptions can't get quota increases. | More TPM; Provisioned Throughput for steady load; two regions behind API Management with load balancing and retry. |
| Agent Service | Threads stored by Foundry | Per-subscription limits; not the bottleneck at this size | Same, monitored |
| AI Search | Free tier: shared, no SLA, 3 indexes, 50 MB | Throughput unknown on shared capacity; one of the things G measures | Basic/S1 with 2+ replicas (99.9% read SLA) |
| Azure SQL | Free offer: 100K vCore-seconds/month, auto-pauses | Cold start after a pause (~1 min, seen in every run so far); the monthly allowance | Data changes once a night, so **cache the metric queries** (results stay correct until the next load) → SQL load near zero; serverless General Purpose with a minimum vCore |
| Functions | Flex Consumption | Scales out by instance; not the bottleneck | Same; set max instances to cap spend |
| Static Web Apps | Free tier, global CDN | Effectively none for this | Standard tier for custom domain + SLA |

**Cost per answer** at gpt-4.1-mini list price ($0.40 / $1.60 per 1M tokens): ~8K in + ~0.5K out ≈ **$0.004**.
1,000 questions ≈ $4; 10,000 a month ≈ $40. Most of the input is repeated instructions and tool schemas, so prompt
caching (automatic, discounted) lowers this further. G checks this number against the measured tokens.

What G produces: a chart of latency and 429 rate against concurrent users, the measured break point, and a short
"what changes at 10x and 100x users" section in ARCHITECTURE.md.

## Teardown additions

Most of this lives in `rg-tx-fiscal-radar-dev` and goes with `infra/down.sh`. Two things don't:

- **The Entra app registration** for the web app's sign-in: tenant-level, not in the resource group.
  `az ad app delete --id <appId>` (the id is recorded in `.env` when it's created).
- **Fine-tuned model and training files:** stored in the Foundry account, deleted with it. The Developer
  deployment deletes itself after 24 hours anyway.

The Functions app, its storage account, Static Web App, embedding deployment and memory store are all in the
resource group and get the project tags (`component=app` for the web pieces).

## Not in scope

- Bing grounding (a separate paid resource; the data doesn't need the web).
- Linking insurance-docai as a second agent (multi-agent handoff): only if time and budget are left after H.
- Public anonymous access to the app.
