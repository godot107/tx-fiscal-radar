# Security: threat model, controls, detection and response

Fiscal Note Desk (web app + MCP endpoint) answers questions about public records for a mixed, politically active audience:
public officials, lobbyists, advocacy groups and journalists. The data is public; what needs protecting is **trust in
the answers**, the **availability and cost** of the service, and **the people** the questions are about.

Red team = attack it and measure ([RESULTS.md](RESULTS.md) → Guardrails). Blue team = this page: prevent, detect,
respond, and fix what the scans find.

## Threat model

| Threat | Example | Impact |
| :--- | :--- | :--- |
| Wrong or invented numbers | Model arithmetic, retrieval of the wrong session's bill | Misinformation with an official-looking source |
| Prompt injection (direct) | "Ignore your rules, you are DAN..." | Leaked instructions, invented figures |
| Prompt injection (indirect) | An instruction planted in a document the search returns | The agent repeats an attacker's claim as fact |
| Misuse against public trust | Fake fiscal notes or press releases, "the election was moved" posts | Disinformation carrying the state's name |
| Partisan capture | Answers that argue one side, or treat one side's framing differently | Loss of neutrality; the tool becomes advocacy |
| Harm to people | Home addresses of legislators, harassment, stereotypes about groups | Safety, discrimination |
| Security-sensitive detail | "Which state systems are easiest to hack, based on the cyber bills?" | Aid to attackers |
| Abuse of capacity | Scripted flood of questions | Cost, throttling for real users (model quota is shared) |
| Credential or data exposure | Keys in git, over-privileged identities | Takeover of the resources |

## Controls (prevent)

Layered, so no single control has to be perfect. Each layer is measured in RESULTS.md.

| Layer | Control | Where |
| :--- | :--- | :--- |
| Identity | Every service-to-service call uses a managed identity; no keys or passwords anywhere (shared keys off on storage, Entra-only SQL, local auth off on Foundry and Search) | `infra/main.bicep` |
| Access | Entra sign-in in front of every route of the app and MCP endpoint (401 without a token); the app identity can only `SELECT` from `dw` | `appAuth`, `sql/04_app_identity.sql` |
| Least privilege | App identity: Azure AI User, Cognitive Services User and OpenAI User on Foundry, Search Index Data Reader, Blob Data Owner on its own storage only | `appRoles` in Bicep |
| Platform filter | `txfr-guardrails` RAI policy: hate at Low, sexual/violence/self-harm at Medium on prompt and completion; Prompt Shields for jailbreak and indirect attack; protected material | Bicep `guardrails` |
| Request screen | One structured gpt-4.1-mini call before the agent: hard stop for election misinformation, impersonation, targeting individuals, attack instructions, hateful framing; one-sided questions are restated neutrally so both sides get the same retrieval | `txfr.guardrails.screen_request` |
| Instructions | Numbers only from tools; neutral fact service; no advocacy, partisanship, vote predictions; no instructions or SQL revealed; tool text is data, never instructions | `txfr.agent.INSTRUCTIONS` |
| Document shield | Prompt Shields on every search result before the model (or an MCP caller's model) sees it; flagged documents replaced | `txfr.agent._shield_search` |
| Grounding check | Content Safety groundedness detection on every answer vs the tool results; flagged, shown in the UI | `txfr.guardrails.groundedness` |
| Tool design | Fixed parameterized SQL only (the model picks a metric, never writes SQL); `top_n` capped at 25; bill ids normalized | `txfr.agent.METRICS` |
| Rate and size | Question ≤ 500 characters; MCP 60 calls/min per user per instance; Function App scale-out capped at 5 instances | `function_app.py`, `mcp_server.py`, Bicep |
| Output | The page escapes all model text before rendering; links only for `https`/`ftp`; CSP header | `app/static/index.html`, `function_app.py` |
| Memory | Per-user scope = Entra object id; the store is told to keep work context only, no personal data; 30-day TTL | `txfr.memory` |

## Encryption

**In transit.** Every hop we control is TLS 1.2 or later:

| Hop | Setting |
| :--- | :--- |
| Browser / MCP client → Function App | `httpsOnly`, `minTlsVersion: '1.2'`, FTP off, HTTP/2 |
| App, ADF, Synapse → ADLS (both storage accounts) | `supportsHttpsTrafficOnly`, `minimumTlsVersion: 'TLS1_2'` |
| App, tools → Azure SQL | server `minimalTlsVersion: '1.2'`; client `Encrypt=yes;TrustServerCertificate=no` (certificate validated) |
| App → Foundry, Content Safety, AI Search, Key Vault, memory store | HTTPS-only service endpoints (TLS 1.2+ enforced by the platform) |
| ADF → Comptroller open data (Socrata) | HTTPS |
| **ADF → Legislature FTP** | **Plain FTP: the server refuses TLS** (`534 Local policy on server does not allow TLS secure connections`, checked 2026-11-10). The data is public, so confidentiality isn't at stake, but integrity in transit isn't protected by the channel. Mitigations: every load is reconciled to the Legislature's own passed-bills count (any tampering that changes the count shows up on the Trust page), and bronze keeps the raw files so a suspicious load can be re-fetched and diffed. |

**At rest.** Everything is encrypted with Microsoft-managed keys (AES-256), which is the default and is on:

| Store | Encryption | Holds |
| :--- | :--- | :--- |
| ADLS Gen2 lake, app storage | Storage service encryption (`keySource: Microsoft.Storage`) | Public source files, Delta/Parquet, deployment packages |
| Azure SQL | Transparent Data Encryption (state: Enabled) | Warehouse (public data), control and run logs |
| AI Search | Service-managed encryption (compliant) | Index of public bill and fiscal-note text, vectors |
| Foundry | Service-managed | Agent threads (users' questions), memory store (users' work context), fine-tuning files, evaluation results |
| Log Analytics / App Insights | Service-managed | Guardrail and cost events (user object ids, no question text), SQL audit |
| Key Vault | Service-managed (software keys, Standard tier) | One optional Socrata app token |

What's personal or sensitive here is small: Entra object ids in logs, questions in agent threads, and the memory
store's work-context notes. Not set, with the reason:

- **Customer-managed keys (CMK)** for storage, SQL, Search, Foundry and Synapse (Checkov CKV_AZURE_240): CMK needs a
  Key Vault with purge protection, which is irreversible and conflicts with this project's teardown. For a client
  whose policy requires CMK (common under CJIS, IRS 1075 or state data-classification rules), production uses Key
  Vault Premium (HSM-backed keys), purge protection, key rotation and per-service CMK.
- **Infrastructure (double) encryption** on storage (`requireInfrastructureEncryption`): set at account creation only;
  production option for confidential data.
- **Retention**: agent threads and memories hold users' questions; memories expire after 30 days (store TTL), logs
  after 30 days. Production would add a thread-retention job and a privacy notice on the page.

## Detect

Every guardrail trigger in the app or MCP endpoint writes one line to Application Insights (`AppTraces` in the
Log Analytics workspace): `GUARD_EVENT {"kind": ..., "user": <object id>, ...}`. No question or answer text is
logged. Kinds: `policy_block`, `injection_removed`, `ungrounded_answer`, `content_filter`, `rate_limited`, `bad_request`.

Alert `txfr-guardrail-spike` (scheduled query, every 5 minutes): 5 or more `policy_block` / `injection_removed` /
`content_filter` events in 15 minutes → email (same action group as pipeline failures).

Azure SQL auditing goes to the same workspace: every query the app's identity runs is recorded.

```kusto
// What fired, by kind and user, last 24 h (`kind` is a reserved word in KQL: alias it, read it as e["kind"])
AppTraces
| where TimeGenerated > ago(24h) and Message startswith "GUARD_EVENT"
| extend e = parse_json(substring(Message, 12))
| summarize events = count() by guard_kind = tostring(e["kind"]), user = tostring(e.user), bin(TimeGenerated, 1h)
| order by TimeGenerated desc

// Probing: one user tripping several different guardrails
AppTraces
| where TimeGenerated > ago(1h) and Message startswith "GUARD_EVENT"
| extend e = parse_json(substring(Message, 12))
| summarize kinds = dcount(tostring(e["kind"])), events = count() by user = tostring(e.user)
| where kinds >= 2 or events >= 10

// Throttling (capacity, not attack): requests the model quota turned away
AppTraces | where Message startswith "GUARD_EVENT" | extend e = parse_json(substring(Message, 12))
| where tostring(e.kind) == "rate_limited" | summarize count() by bin(TimeGenerated, 5m)

// What the app identity ran against the warehouse (SQL auditing)
SQLSecurityAuditEvents
| where TimeGenerated > ago(24h) and server_principal_name_s startswith "func-txfr"
| project TimeGenerated, statement_s, succeeded_s
```

## Respond: incident runbook

| Step | Action | How |
| :--- | :--- | :--- |
| 1. Contain | Take the app and MCP endpoint offline and stop new sign-ins (data untouched) | `tools/killswitch.sh stop` (`full` also stops the pipeline schedules). Drilled 2026-11-10: offline in 13 s (HTTP 403), back in 17 s |
| 2. Scope | Who, what, since when | KQL above; App Insights requests; SQL audit; Foundry agent threads for the affected users |
| 3. Fix | Tighten the layer that failed: screen category, instruction, filter threshold, rate limit, role | Code + Bicep; redeploy (`app/deploy.sh`, `infra/up.sh`) |
| 4. Verify | Re-run the red team and the eval before reopening | `txfr.guardrails redteam/public/neutrality`, `txfr.agent_eval run` |
| 5. Recover | Reopen; watch the alert for 24 h | `tools/killswitch.sh start` |
| 6. Learn | Add the attack to the red-team set so it stays fixed | `ATTACKS` / `ATTACKS_PUBLIC` in `txfr.guardrails` |

Credential compromise is a smaller blast radius than usual because there are no stored credentials: disable the
app registration (`killswitch stop`), and if the operator account is suspected, revoke its sessions in Entra and
review role assignments on the resource group.

## Scans (find and fix)

Run on 2026-11-10; CI repeats the dependency and secret scans on every push.

| Scan | Tool | Result | Action |
| :--- | :--- | :--- | :--- |
| Known-vulnerable dependencies | `pip-audit` | 1: pyspark 3.5.5 (PYSEC-2026-3966), local tests only | Pinned 3.5.8 |
| Secrets in files and git history | `detect-secrets`, history grep | None | Baseline committed; CI fails on a new finding (GitHub secret scanning isn't available for this private repo) |
| Infrastructure misconfiguration | Checkov (Bicep) | 24 passed, 48 failed | Triage below |
| Dependency alerts | GitHub Dependabot | Alerts + automatic security PRs on | |

Checkov triage (48 failed checks):

| Group | Checks | Decision |
| :--- | :--- | :--- |
| Fixed | SQL auditing on → Log Analytics (CKV_AZURE_23); app HTTP/2 (CKV_AZURE_18/67); FTP and basic-auth (publishing password) deployments off (CKV_AZURE_78) | Deployed |
| Production only: network isolation | Public network access on storage, Key Vault, ADF, SQL, Foundry, web app (CKV_AZURE_35, 109, 189, 104, 113, 134, 222) | Private endpoints + VNet integration in the production reference (ARCHITECTURE.md); dev uses Entra-only auth + firewall instead |
| Production only: resilience and paid tiers | Zone redundancy, replication, minimum instances, Search SLA, Synapse CMK and exfiltration protection, isolated Spark, Defender for SQL alerts, 90-day audit retention (CKV_AZURE_206, 225, 229, 212, 208/209, 240, 157, 242, 25/26/27, 24) | Cost; listed in the production reference |
| Deliberately off | Key Vault purge protection (CKV_AZURE_110, and CKV_AZURE_42 which requires it; soft delete is on, 7 days) | Irreversible; would block the teardown purge for up to 90 days. On in production |
| Process | ADF git integration (CKV_AZURE_103) | Pipelines are generated from `adf/_generate.py` and CI checks the JSON; production uses ADF git integration |
| Not applicable or false positive | `httpsOnly`, `minTlsVersion`, managed identity, Entra auth on the app; App Service logging/Azure Files/.NET checks for a Flex Consumption Python app (CKV_AZURE_14/15/16/71/13/44/63/65/66/80/88/153/17/213) | Settings verified on the deployed resources; Checkov doesn't evaluate conditionally deployed resources |

Not enabled, on purpose: Microsoft Defender for AI threat protection (30-day trial, then billed per token). Worth it
in production; off while this is a learning project.

## Code review (2026-11-10)

A high-effort review of everything changed in phases A-H found 10 defects; all fixed and re-tested.

| # | Finding | Fix |
| :- | :--- | :--- |
| 1 | `up.sh` didn't read `.env`, so a routine deploy blanked the app's sign-in client id and agent id | `up.sh` exports `APP_CLIENT_ID`, `AGENT_ID`, `AGENT_ID_KNOWLEDGE` from `.env` |
| 2 | A dropped SQL connection (the free database pauses after 60 idle min) was never replaced: tool errors are caught inside the agent loop | Liveness check before each request + reconnect-and-retry once inside the tool loop |
| 3 | MCP: non-`TypeError` exceptions became HTTP 500 without a JSON-RPC body | Every tool error returns a structured `isError` result |
| 4 | Document shield failed **open** on a Content Safety error; the Foundry Knowledge variant isn't shielded | Fail **closed** (results withheld, `shield_error` event); retry 5xx; Knowledge variant documented, groundedness skipped there |
| 5 | App Insights sampling could drop `GUARD_EVENT` lines during a burst (when the alert matters) | Traces excluded from sampling |
| 6 | Charts drawn by code interpreter were paid for and discarded; extra API call per question | Images returned and rendered; fetched only when code interpreter ran |
| 7 | Empty agency text matched every agency (`LIKE '%%'`), inviting code guessing | Returns "missing argument" |
| 8 | New eval case could match anything if its SQL returned nothing; `701` matched dollar figures | Assertion + tighter pattern |
| 9 | Search fallback retried any error (even in keyword mode); filter values unescaped | Fallback only from semantic mode on semantic errors; OData quotes escaped |
| 10 | One Prompt Shields call per document; oversized requests | Batched calls within the API's limits (≤ 5 documents, ≤ 10,000 characters), unit-tested |

Found afterwards by evaluation and smoke tests (also fixed): the batched shield initially sent 6 documents (limit 5)
and failed closed, emptying search results; a deploy missed a new module and `/api/ask` returned ImportError in
production. Deploys now end with an authenticated end-to-end smoke test.

## Cost and abuse (FinOps as a security control)

An open endpoint's most likely incident is a bill, not a breach. Controls, cheapest first: sign-in (unauthenticated
flood of 500 requests: all 401, no model cost), per-user daily cap ($1, shared across instances), answer cache,
instance cap (5), model quota, and an **automatic cost circuit breaker**: more than $2 of metered spend in an hour stops
the Function App through a Logic App with its own managed identity (drilled: offline 10 s after trigger). Azure
budgets only email. Design for public exposure (Front Door + WAF, API Management quotas and token limits, External ID):
[ARCHITECTURE.md](ARCHITECTURE.md) → If it were opened to the public. Unit economics: [COSTS.md](COSTS.md).

## Privacy

Minimize by default, opt in for more, disclose everything: questions and answers are deleted after answering unless
the user opts in; logs carry a pseudonymous id and no question text; sentiment only from explicit 👍/👎.
Details and the data inventory: [PRIVACY.md](PRIVACY.md); user-facing notice: `/privacy`.
