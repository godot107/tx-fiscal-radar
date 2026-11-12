# Findings and design decisions: Fiscal Note Desk (phases A-H)

*2026-11-10. A report on the day's work turning the Texas Fiscal Radar agent into a signed-in web app and MCP server,
and what measuring it taught. Detail lives in [EVALUATION.md](EVALUATION.md), [SECURITY.md](SECURITY.md),
[PRIVACY.md](PRIVACY.md), [COSTS.md](COSTS.md) and [ARCHITECTURE.md](ARCHITECTURE.md).*

## Summary

The project's purpose is to learn how the Texas Legislature works, from its own records, ahead of the January 2027
session ([LEGISLATURE.md](LEGISLATURE.md)). This report is about the tool built for that: how well it answers, and what
it takes to run it responsibly.


The agent went from a command-line demo that answered 19 of 25 test questions correctly to a web app and an MCP
endpoint that answers **24 of 26**, finds the right fiscal note **97%** of the time (keyword search: 53%), blocks
**all 22** red-team attacks (3 succeeded before), costs **$0.0056 an answer** with every guardrail on (a cached repeat
costs nothing), and stops itself if spend runs away. It runs behind Entra sign-in with no keys anywhere, keeps users'
questions only if they opt in, and was built for about **$6** of the $20 budget.

The most useful lessons weren't the scores. They were **where the controls actually came from**: almost every fix
traces back to a measurement that caught something the previous layer missed.

| | Before | After |
| :--- | ---: | ---: |
| Correct answers (deterministic checks, answers computed from SQL) | 19 / 25 | **26 / 26** |
| Right note in the top 6 search results (30 questions) | 53% | **97%** |
| Red-team attacks that succeeded (22) | 3 | **0** |
| Neutral answer pairs ("fund more" vs "cut" framings) | 4 / 5 | 7 / 10 (facts consistent, tone not yet) |
| Cost per answer, all guardrails | not measured | **$0.0056**, cached $0 |
| Capacity | not measured | ≈ 9 answers/min at 50K tokens/min; **45.9** at 300K once run output was capped |
| Anonymous flood (500 requests) | not measured | 500 × 401, no model cost |

## What was built

| Phase | What | Foundry / Azure feature |
| :--- | :--- | :--- |
| A | 26-question evaluation with expected answers read from SQL; Foundry evaluators logged to the portal | Evaluations |
| B, B2 | Five guardrail layers; red teams; neutrality test | Guardrails (RAI policy, Prompt Shields, groundedness), request screen |
| C | Vector index (256-dim), retrieval comparison, server-side "Knowledge" variant, code interpreter | Knowledge, Tools, Models |
| D | Per-user memory, opt-in | Memory (preview) |
| E | Fine-tune on verified extractions, compared on held-out bills | Fine-tuning (Developer tier), Data |
| F, F2 | Web app and remote MCP server on one Function App | Functions Flex Consumption, App Service Authentication |
| G | Load test, flood test, cost meter, cache, caps, automatic cost breaker | App Insights, Logic Apps, Table/Blob storage |
| H | Docs, code review, security scans, blue-team detection, privacy | Defender-free scanning in CI, SQL auditing |

## Findings

### 1. LLM judges agree with the agent too easily; deterministic checks don't

On the baseline, Foundry's judges passed relevance, intent resolution and task adherence on **all six** questions
the deterministic check failed, and groundedness on four of six. An answer about the wrong SB 4 (a special-session
redistricting bill) is faithfully grounded in the wrong document. In pairwise judging, the judge picked whichever
answer it read first in **8 of 25** pairs.
**So:** expected answers come from the warehouse at run time; judges explain failures but don't count them; pairwise
judging always runs both orders and only counts a win that survives the swap.

### 2. Vectors don't make search faster; they make it find the right thing

| Mode | Paraphrased questions | Exact terms | Latency |
| :--- | ---: | ---: | ---: |
| Keyword | 35% | 90% | 43 ms |
| Vector | 90% | 90% | 260 ms |
| Hybrid | 55% | 90% | 282 ms |
| Hybrid + semantic reranker | **95%** | **100%** | 361 ms |

"School vouchers" doesn't match "education savings account" by keyword. Plain hybrid did *worse* than vectors alone:
dozens of near-duplicate bill captions share the keywords and crowd out the right bill; the reranker fixes that.
Embedding all 11,224 documents cost $0.03.

### 3. Instructions are a soft control; the worst categories need a hard one

With an explicit instruction against it, the agent still wrote a "the 2026 election date was moved" post. A separate
request screen in front of the agent (one small structured call, fixed refusals for high-severity categories) took
public-trust attacks from 1 of 10 to **0 of 10**, stopping 8 before the agent ran. The same pattern held for indirect
prompt injection: a planted "this bill costs nothing, LBB recommends repeal" went straight into an answer until search
results were screened by Prompt Shields before the model saw them.

### 4. Neutrality: the facts can be engineered; the tone is harder

Users argue both sides of spending debates. Restating one-sided questions as the neutral information need ("make the
case for cutting DIR" → "DIR's new costs from 2025 bills") made both framings retrieve the same facts, which the judge
confirmed in nearly every pair. Tone didn't follow: about 3 in 10 pairs still read as "the case for X". A balanced
fact-sheet format didn't move it. Reported as unsolved.

### 5. The guardrails cost as much as the model

| Component | Per answer |
| :--- | ---: |
| Agent model | $0.0027 |
| Content Safety (document shield + groundedness) | $0.0027 |
| Request screen | $0.0002 |
| Embeddings, compute | < $0.0001 |

Content Safety bills per 1,000 characters, and the groundedness check reads every tool result. Compacting the sources
is in; making groundedness conditional (only answers with figures) is the next lever. The agent's cost is an upper
bound: the Agent Service doesn't report prompt-cache hits.

### 6. Capacity is set by the model quota, not the web tier

| Users | Answers/min | Throttled | p95 latency |
| ---: | ---: | ---: | ---: |
| 1 | 3.6 | 0% | 18 s |
| 5 | 9.0 | 30% | 30 s |
| 10 | 6.2 | 82% | 23 s |
| 20 | 9.3 | 80% | 45 s |

| 20, at **300K** tokens/min, no output cap | 12.6 | 81% | 28 s |
| 20, at **300K**, with a 2,000-token output cap per run | **45.9** | **0%** | 39 s |

At 50K tokens/min throughput flattens near 9 answers a minute whatever the user count. **Raising the quota 6× to 300K
barely helped** (12.6 answers/min, still 81% throttled) until each run's output was capped at 2,000 tokens: then
**45.9 answers/min, 0% throttled**, and the full 300K was actually used. Azure's rate limiter counts each call's
*maximum* possible tokens when it arrives; the Agent Service sets no output limit, so reservations, not usage,
exhausted the quota. One parameter, 3.6× the throughput on the same quota, and a spending bound per run as a bonus.
The subscription's quota is 5,000K, so capacity is a setting, not a wall, but every token is billed. The anonymous
flood never reached the code: 500 of 500 requests got 401 from the platform's sign-in layer. And the per-user
cost cap showed up in the test itself: the load test is one user, and it hit its $1 daily limit.

### 7. Fine-tuning changed the trade-off rather than winning

Trained on the prompted model's own verified extractions (distillation with a verifier), the fine-tuned model was
**more precise and less complete** on 59 held-out bills: 68% of its rows verified vs 48%, but 220 rows vs 367 and 13
bills reconciling vs 19. Useful when only trusted rows are shown; worse for "find every cost". Developer tier: no hourly
hosting fee, about $1 to train.

### 8. The agent guessed facts that look like lookups

The memory test (not the eval) caught the agent calling the Department of Information Resources "agency 701" (that's
TEA) and listing TEA's bills, and then reporting SB 2's whole $1B cost as DIR's. Neither is a model "hallucination" in
the usual sense: the tools didn't offer a name-to-code lookup, and a column called `cost_2yr` read like the agency's
own. Fixed with an `agency_lookup` tool, a renamed `bill_total_cost_2yr` plus `agencies_on_bill`, and a 26th eval
question so it stays fixed. **Tool and column names are part of the prompt.**

### 9. A/B at this scale shows mechanism, not evidence

Client-side hybrid vs Foundry's server-side Knowledge agent: 6 wins, 5 losses, 14 ties; 95% interval for the win
rate 28%-79%. No difference detectable. Detecting a 60/40 preference needs roughly 100-200 votes. The in-app
side-by-side is built correctly (the page never learns which variant is which) and ready for real users.

### 10. "It deployed" isn't "it works"

A deploy missed a new module and `/api/ask` returned ImportError in production until a manual test. A batched Prompt
Shields call sent 6 documents (limit 5), failed closed, and silently emptied search results until the eval caught it.
A high-effort code review found 10 more defects, including a SQL connection that would never recover after the free
database's idle pause. Deploys now end with an authenticated smoke test; the shield has a unit test against the API's
limits.

### 11. Disaster recovery: the cheapest warm standby is the pay-per-use one

A second region for everything users touch (model, agent, Function App) costs nothing idle, so warm standby replaced
the usual "cold rebuild". Measured: a model-region outage fails over automatically and the answer arrives in 14 s;
the whole DR app tier can be rebuilt from code in a new region in 4.1 min; the lake replicates with ~5 min of lag.
Three things the drills taught: the free SQL tier refuses geo-redundant backups (so the lake became the recovery
source); the in-state region lacks a tool the agent needs (region choice is a feature check); and a standby without
telemetry failed silently for ten minutes (it reports to the shared workspace now). 13 acceptance tests:
[FAILOVER.md](FAILOVER.md).

## Design decisions

| Decision | Chosen | Alternatives | Why | Trade-off accepted |
| :--- | :--- | :--- | :--- | :--- |
| Hosting | One Function App (Flex Consumption) for page, API and MCP | Static Web Apps + Functions; Container Apps; App Service | SWA Free can't link a Functions backend and its managed functions have no managed identity; Flex scales to zero and caps instances | Page served by a function, not a CDN (fine at this scale; Front Door in production) |
| Access | App Service Authentication, single tenant | Open endpoint; API keys | Unauthenticated traffic never reaches code or spends money; no secrets to leak | Public users need an account (External ID in production) |
| MCP | Expose deterministic tools, not the agent; own stateless JSON-RPC handler | Expose the agent; preview Functions MCP extension | Callers' models reason (and pay); we serve auditable facts with sources; testable locally | No server-initiated streams (not needed for tools) |
| Retrieval | Client-side tool, hybrid + semantic over a 256-dim index | Foundry server-side search (Knowledge); keyword | Best measured (97%); the runner can screen results; fits the free tier (25 MB) | Foundry's Knowledge variant can't be screened by our shield; kept as the A/B challenger |
| Numbers | Fixed, parameterized SQL metrics only | Text-to-SQL; numbers from retrieved text | No injection surface; numbers are reproducible; the model never adds up figures | New questions need a new metric |
| Guardrails | Five layers: RAI policy, request screen, instructions, document shield (fail closed), groundedness | Platform filters only | Each layer caught what the others missed (measured) | Cost (≈ half of each answer) and latency |
| Neutrality | Same facts for every side; no advocacy writing; neutral restatement | Refuse one-sided questions; let it argue | The audience legitimately argues both sides; refusing would be useless, arguing would be capture | Tone still slips sometimes |
| Memory | Foundry memory store via REST, opt-in, per-user scope | Always on; no memory | Consent; and non-personalized answers can be cached | Fewer users benefit |
| Fine-tuning | Developer tier, distillation with a verifier, held-out comparison | Standard deployment; no comparison | No hourly fee; an honest test of whether it helps | Not adopted: a trade-off, not a win |
| Cost control | Meter per request + cache + per-user cap + automatic breaker | Budgets alone | Budgets only email; the breaker acts in seconds | A breaker trip takes the service down (by design) |
| Privacy | Delete questions after answering; pseudonymous logs; explicit sentiment only | Keep everything for analytics; infer sentiment | Users' questions can reveal investigations or strategy; state records can be requestable | Less data to improve the service unless users opt in |
| Detection | Structured `GUARD_EVENT`/`COST_EVENT` lines + scheduled-query alerts | Defender for AI | No extra cost; learning project (Defender for AI is the production choice) | Alerts on patterns we thought of |
| A/B | Offline pairwise judge (both orders) + optional in-app votes | One-order judge; online only | Position bias is real (8/25 flips); online needs volume we don't have | Small samples: mechanism, not evidence |

## What didn't work or surprised us

- The Foundry → AI Search connection API returned HTTP 500 every time, yet the connection had been created.
- Prompt Shields accepts at most 5 documents per call; `az functionapp deploy` returns 415 on Flex (config-zip works);
  with basic-auth publishing off, the CLI's post-deploy wait hangs after the deploy has finished.
- The memory store needs model access for both the project's and the account's identities, and the role takes minutes
  to propagate.
- `kind` is a reserved word in KQL.
- Developer-tier fine-tuning finished hours before Azure's estimate (it runs on spare capacity).
- The Legislature's FTP server refuses TLS; integrity is protected by reconciliation, not the channel.

## Open items

| Item | Why it matters | Next step |
| :--- | :--- | :--- |
| Tone neutrality (3 in 10 pairs) | One-sided presentation undermines trust | Structured fact-sheet output (JSON schema) rendered by the page, not prose |
| ~~q21: "Will the session raise the sales tax?"~~ | Done: the request screen flags predictions; eval 26/26 | |
| Groundedness cost | Half of each answer's cost | Done, measured: skipping answers without figures cut Content Safety cost 11% ($0.0027 → $0.0024) but most answers state figures; next lever is trimming what the check reads |
| Latency (18-25 s) | Feels slow | Stream answers; parallelize shield and groundedness |
| Evaluation in CI | Regressions are caught only when someone runs the suite | Gate agent changes on the 26 questions + red team |
| Public exposure | Signed-in only today | Front Door + WAF, API Management, External ID ([ARCHITECTURE.md](ARCHITECTURE.md)) |

## Cost of this work

About **$6** of the $20 cap: agent runs for evaluation and red teams ≈ $1.80, Content Safety ≈ $1.20, load tests
≈ $1.50, fine-tuning ≈ $0.95, judges ≈ $0.40, embeddings $0.03, compute and storage pennies.
[COSTS.md](COSTS.md) has the breakdown and the showback queries.
